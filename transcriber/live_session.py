# -*- coding: utf-8 -*-
"""live 实时会话域:Pass1 流式识别 + Pass2 窗口修正 + Pass3 LLM 的完整接线(Task 9)。

会话目录 sessions_root/<session_id>/:
  live_meta.json    会话状态机:id/title/speakers/language/created_at/status(
                    listening → stopping → 目录删除;恢复由 live_finalize 负责)
  live_audio.wav    AppendWav 边录边落(未 finalize 时头部长度 0,崩溃读回 0 长度是安全态)
  live_pass1.jsonl  final 逐行落盘 {text, t0_ms, t1_ms},start 时建空文件

线程模型(谁写什么,单一写者):
  - 调用方(WS 接收线程,单调用者)调 start / feed_bytes / handle_stop;
  - live-writer:字节 → AppendWav.append(落盘)→ float32 → Pass1Engine.feed;
  - Pass1Engine 工作线程触发 on_partial/on_final 回调(final 落 jsonl,单一写者);
  - live-pass2 驱动线程:周期 tick(500ms)取 finals 快照 → plan_windows 规划 →
    skip 由本侧 emit、窗口交 Pass2Runner.run_window(whisper 串行,GPU_LOCK 互斥);
    now_ms 一律用累计字节口径(duration_s×1000),回放与实时统一,不用墙钟;
  - live-llm refine worker 线程:消费内部 pass2 事件队列 → LLMRefiner → emit llm
    (key 从 env.rtf 解析,失败整层禁用;熔断/兜底在 live_llm);
  - live-finalize 转正线程(handle_stop 拉起,异步):排空音频 → 停引擎 → 驱动线程
    force_tail 补跑尾窗(游标推满 now)→ LLM 有界排空 → wav finalize →
    live_finalize.finalize_session 建 job → emit stopped(带 job_id)→ 从 _SESSIONS
    摘除会话;转正失败发 error + stopped(无 job_id,音频留 live/ 由重启恢复转正),
    同样摘除——pumpEvents 三种退出路径都因此有界。
  - live-watchdog 看门狗线程:周期巡检,无字节喂入(feed_bytes)且无事件投递
    (tap.deliver 有 pump 在消费)持续超过 WATCHDOG_IDLE_S → 自动 handle_stop
    走正常转正(桌面端退出而未按「停止」的孤儿会话,否则要等服务重启的
    live_recover);静音不算触发——采集持续时静音也有字节流,字节口径安全;
    会话离开 listening(stopping/转正完成)即退出;
  - 硬约束:绝不在 on_partial/on_final 回调内调 engine.stop()(join 自身线程抛
    RuntimeError),stop 一律由会话调用方线程(handle_stop)发起。

下行事件 {'type': 'partial'|'pass1_final'|'pass2'|'llm'|'pass2_skip'|'stop_ack'|
'stopped'|'error', 'seq': int, ...} 由 _emit 统一分配 seq(单调递增、无重复),写
events 队列 + 环形缓存(BACKLOG_MAX,resume 按 last_seq 重放)。WS 层 pump 经
EventTap 取事件:断连后快速重连时旧 pump 手里的事件锁内放回队首,不吞、不乱序
(Task 7 实测的 stop_ack 丢失竞态在此修);pump 主循环 pump_events 同在本模块,
stopped / gone / 连续 send 失败任一即退出(转正异步期间对端断开不活锁)。
"""
import json
import os
import queue
import threading
import time
import uuid
from collections import deque

import numpy as np

from live_llm import LLMRefiner, parse_key, refine_worker
from live_pass1 import Pass1Engine
from live_pass2 import Pass2Cfg, Pass2Runner, plan_windows
from live_speaker import OnlineSpeakerCluster
from live_wav import AppendWav

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2          # 16bit 单声道
BACKLOG_MAX = 1000                        # resume 重放的环形缓存上限
PASS2_TICK_S = 0.5                        # Pass2 驱动线程周期(秒)
LLM_DRAIN_S = 30                          # 停止时 LLM 队列有界排空上限(秒)
WATCHDOG_IDLE_S = 10 * 60                 # 孤儿会话阈值:无字节且无投递持续超过即自动停止
WATCHDOG_PERIOD_S = 60                    # 看门狗巡检周期(秒;测试注入小值)
META = 'live_meta.json'
AUDIO = 'live_audio.wav'
JSONL = 'live_pass1.jsonl'

_STOP = object()   # 音频写线程停止哨兵(区别于任何字节串)

_SESSIONS = {}                            # session_id -> LiveSession(进程内注册表,供 resume 查找)
_SESSIONS_LOCK = threading.Lock()


def find_session(session_id):
    """按 id 找进程内会话;已转正回收或服务重启后的会话返回 None(恢复走 live_finalize)。"""
    with _SESSIONS_LOCK:
        return _SESSIONS.get(session_id)


def _parse_lang(spec):
    """前端语言对 'src>tgt'(如 'ja>zh')→ (whisper 语言码, LLM 目标语);非法回落 ('zh','en')。

    src/tgt 合法集均为 {zh, en, ja}(spec 2026-10-24);「不翻译」由前端序列化为
    tgt==src,服务端不特判——LLMRefiner 按 src==tgt 走不翻译分支。"""
    src = tgt = ''
    if isinstance(spec, str) and '>' in spec:
        parts = [p.strip() for p in spec.split('>', 1)]
        if len(parts) == 2:
            src, tgt = parts
    if src not in ('zh', 'en', 'ja'):
        src = 'zh'
    if tgt not in ('zh', 'en', 'ja'):
        tgt = 'en'
    return src, tgt


class EventTap:
    """events 队列的安全取用器(修 ws_live 断连后 ≤0.5s 重连的双 pump 竞态)。

    deliver 把「取事件 → 判 gone → send → 失败归还」整个周期放在一把锁内:并发
    pump 之间天然串行,断连 pump 手里的事件按原顺序归还 redelivery 队首,新 pump
    先取归还队列再取事件队列——不吞事件、不乱序。seq ≤ last_seq 的事件视为 resume
    已重放过,消费但不发送。"""

    def __init__(self, events, on_deliver=None):
        self._q = events
        self._front = deque()              # 断连 pump 归还的事件(队首优先)
        self._lock = threading.Lock()
        self._on_deliver = on_deliver      # 事件成功投递(pump 活跃)时回调,看门狗判活用

    def deliver(self, send, gone, last_seq=0, timeout=0.2):
        """取一个事件并投递。返回事件 dict(已发送,或 seq≤last_seq 已重放过而免发);
        超时 / gone 已置位 / send 失败(事件已归还队首)返回 None,调用方据此退出或重试。"""
        with self._lock:
            if gone is not None and gone.is_set():
                return None
            if self._front:
                ev = self._front.popleft()
            else:
                try:
                    ev = self._q.get(timeout=timeout)
                except queue.Empty:
                    return None
            if gone is not None and gone.is_set():
                self._front.appendleft(ev)   # 取到事件才发现连接已断:锁内按序归还
                return None
            if ev.get('seq', 0) > last_seq:
                try:
                    send(ev)
                except Exception:            # noqa: BLE001 对端已断:锁内归还,交给下一个 pump
                    self._front.appendleft(ev)
                    return None
            if self._on_deliver is not None:
                self._on_deliver()           # 已投递(或按 seq 判重免发但已被消费):pump 活跃
            return ev


def pump_events(tap, send, gone, last_seq=0, timeout=0.2):
    """WS 事件 pump 主循环(app.ws_live 的 pump 真身,收口在此便于单测)。

    经 tap.deliver 投递事件;三种退出:①投出 stopped(会话终结);②gone 置位
    (接收线程收尾);③连续 3 次 send 失败——对端已断。③是修复:转正异步期间
    接收线程阻塞在 sender.join(),客户端断开时 gone 永不置位,pump 若无限重试
    失败的 send 会空转烧 CPU 且 join 永不返回(活锁);失败事件已按序归还 tap,
    由断线重连后的新 pump 续投。返回最后投出的 seq。"""
    fails = 0

    def _send(ev):
        nonlocal fails
        fails += 1                       # 先计数:send 抛错时保留,成功才清零
        send(ev)
        fails = 0

    while True:
        ev = tap.deliver(_send, gone, last_seq, timeout=timeout)
        if ev is None:
            if gone is not None and gone.is_set():
                return last_seq
            if fails >= 3:
                return last_seq          # 对端持续断开:有界退出,不再空转
            continue
        last_seq = ev['seq']
        if ev.get('type') == 'stopped':
            return last_seq


class LiveSession:
    """一场实时会议:音频落盘 + Pass1 流式识别 + Pass2 窗口修正 + Pass3 LLM + 事件下行。

    构造注入点(测试/长跑用,生产默认全真实):
      pass2_cfg:窗口调度参数(默认 Pass2Cfg 20/35/1.5/200);
      speaker_cluster:传 None 则构造 OnlineSpeakerCluster;
      finalizer:(session_dir, speakers_hint) -> job_id,默认走 live_finalize.finalize_session;
      watchdog_idle_s / watchdog_period_s:孤儿看门狗的空闲阈值与巡检周期
      (默认 600s / 60s;测试注入小值,不真睡 10 分钟)。
    """

    def __init__(self, sessions_root, model_dir, pass2_cfg=None, speaker_cluster=None,
                 finalizer=None, watchdog_idle_s=WATCHDOG_IDLE_S,
                 watchdog_period_s=WATCHDOG_PERIOD_S):
        self.sessions_root = sessions_root
        self.model_dir = model_dir
        self.id = None
        self.status = 'idle'               # idle → listening → stopping(目录随后删除)
        self.events = queue.Queue()        # WS 层直接消费的下行事件队列
        self._last_activity = time.monotonic()   # 看门狗判活:最近一次字节喂入或事件投递
        self._watchdog_idle_s = watchdog_idle_s
        self._watchdog_period_s = watchdog_period_s
        self._watchdog = None
        self.tap = EventTap(self.events, on_deliver=self._touch)   # pump 安全取用(修双 pump 竞态)
        self._backlog = deque(maxlen=BACKLOG_MAX)
        self._seq = 0
        self._ev_lock = threading.Lock()   # 守 seq 分配 + backlog + 入队三步原子
        self._bytes_fed = 0                # 时间轴锚点:累计已喂字节(偶数截断后)
        self._dir = None
        self._meta = None
        self._wav = None
        self._engine = None
        self._q = queue.Queue()            # 音频写线程的字节队列
        self._writer = None
        # Pass2 驱动(Task 9 接线)
        self._cfg = pass2_cfg or Pass2Cfg()
        self._cluster = speaker_cluster if speaker_cluster is not None else OnlineSpeakerCluster()
        self._runner = None                # start 时建(需要 wav 路径)
        self._cursor_ms = 0                # Pass2 已结算游标(音频时间轴)
        self._pass2 = None
        self._pass2_stopping = threading.Event()
        self._pass2_done = threading.Event()
        # Pass3 LLM(Task 9 接线)
        self._llm_q = queue.Queue()        # 内部队列:pass2 事件 → refine worker
        self._llm = None
        self._refiner = None
        self._llm_stop = threading.Event()
        # 转正
        self._finalizer = finalizer or self._default_finalizer
        self._finalizing = None

    # ---------- 生命周期(调用方线程) ----------

    def start(self, title, speakers, language='zh>en'):
        """建会话目录三件套并起三层引擎线程,返回 session_id(时间戳_uuid4[:4],与 jobs 同构)。"""
        assert self.status == 'idle', '会话只能 start 一次'
        self.id = time.strftime('%Y%m%d-%H%M%S') + '_' + uuid.uuid4().hex[:4]
        self._dir = os.path.join(self.sessions_root, self.id)
        os.makedirs(self._dir, exist_ok=True)
        self._wav = AppendWav(os.path.join(self._dir, AUDIO), SAMPLE_RATE)
        open(os.path.join(self._dir, JSONL), 'a', encoding='utf-8').close()   # 建空文件
        whisper_lang, tgt = _parse_lang(language)
        self._meta = {'id': self.id, 'title': (str(title).strip() or '实时会议')[:80],
                      'speakers': int(speakers or 0), 'created_at': time.strftime('%Y-%m-%d %H:%M'),
                      'status': 'listening', 'sample_rate': SAMPLE_RATE, 'language': whisper_lang}
        self._save_meta()
        self._engine = Pass1Engine(self.model_dir, self._on_partial, self._on_final)
        self._runner = Pass2Runner(os.path.join(self._dir, AUDIO), self._on_pass2_event,
                                   language=whisper_lang, speaker_cluster=self._cluster)
        self._writer = threading.Thread(target=self._run_writer, name='live-writer-' + self.id,
                                        daemon=True)
        self._writer.start()
        self._pass2 = threading.Thread(target=self._run_pass2, name='live-pass2-' + self.id,
                                       daemon=True)
        self._pass2.start()
        key = parse_key()                  # env.rtf;无 key → Pass3 整层禁用
        self.llm_enabled = bool(key)       # ws_live 透传给前端:无 key 且选了翻译目标时提示
        if key:
            self._refiner = LLMRefiner(key, lang_pair=(whisper_lang, tgt))
            self._llm = threading.Thread(
                target=refine_worker,
                args=(lambda: self._llm_q.get(timeout=0.5), self._emit,
                      self._refiner, self._llm_stop),
                name='live-llm-' + self.id, daemon=True)
            self._llm.start()
        self.status = 'listening'
        self._watchdog = threading.Thread(target=self._run_watchdog,
                                          name='live-watchdog-' + self.id, daemon=True)
        self._watchdog.start()
        with _SESSIONS_LOCK:
            _SESSIONS[self.id] = self
        return self.id

    def feed_bytes(self, b):
        """喂一批 16bit PCM 字节(单调用者线程)。字节计数即时间轴锚点,不去重、不排序。"""
        if self.status != 'listening' or not b:
            return
        keep = len(b) - (len(b) % 2)       # 与 AppendWav 同口径的偶数截断
        if not keep:
            return
        self._bytes_fed += keep
        self._touch()                      # 采集仍在推进(静音也有字节流):看门狗不触发
        self._q.put(b[:keep])

    def duration_s(self):
        """已录时长 = 累计字节 / 32000(音频时间轴唯一口径,Pass2 驱动同样用它)。"""
        return self._bytes_fed / BYTES_PER_SEC

    def handle_stop(self):
        """listening→stopping:确认停止并立即返回(stop_ack 已入队)。转正全程异步:
        排空音频 → 停引擎 → 驱动线程补跑尾窗 → LLM 排空 → finalize 建 job →
        emit stopped(带 job_id)。幂等。"""
        if self.status != 'listening':
            return
        self.status = 'stopping'
        self._save_meta(status='stopping')
        self._emit({'type': 'stop_ack'})
        self._finalizing = threading.Thread(target=self._finish, name='live-finalize-' + self.id,
                                            daemon=True)
        self._finalizing.start()

    def _touch(self):
        """刷新最近活动时间(字节喂入或事件投递),孤儿看门狗的判活依据。"""
        self._last_activity = time.monotonic()

    def _run_watchdog(self):
        """孤儿会话看门狗:桌面端退出而未按「停止」时,会话永滞 listening,只能等
        服务重启的 live_recover 转正。本线程在「无字节喂入且无事件投递」持续超过
        watchdog_idle_s 后自动 handle_stop(走正常转正,与手动停止同路径)。
        静音不是触发条件:采集持续时静音也有字节流,字节口径安全。周期巡检,
        会话离开 listening(stopping/转正完成)即退出。handle_stop 幂等且只拉起
        转正线程,从本线程发起与从 WS 调用方线程发起等价。"""
        while self.status == 'listening':
            time.sleep(self._watchdog_period_s)
            if self.status != 'listening':
                return
            if time.monotonic() - self._last_activity > self._watchdog_idle_s:
                self.handle_stop()
                return

    # ---------- 转正线程 ----------

    @staticmethod
    def _default_finalizer(session_dir, speakers_hint=None):
        import live_finalize
        return live_finalize.finalize_session(session_dir, speakers_hint=speakers_hint)

    def _finish(self):
        try:
            self._q.put(_STOP)
            self._writer.join()                # 写线程排空剩余字节后退出
            self._engine.stop()                # 阻塞至队列排空+尾部冲刷(final 落盘入队)
            self._pass2_stopping.set()         # 驱动线程下一轮(或在飞窗口后)force_tail 补尾窗
            self._pass2_done.wait()
            self._drain_llm()
            self._wav.finalize()
            if self._bytes_fed == 0:           # 空会话:无音频可转正,不建 job 直接清目录
                import shutil
                shutil.rmtree(self._dir, ignore_errors=True)
                self._emit({'type': 'stopped'})
                return
            job_id = self._finalizer(self._dir, speakers_hint=self._meta.get('speakers'))
            self._emit({'type': 'stopped', 'job_id': job_id})
        except Exception as e:  # noqa: BLE001 转正失败也要让客户端收尾:error 可见 + stopped(无 job_id)
            self._emit({'type': 'error', 'message': '停止转正失败:%s' % e})
            self._emit({'type': 'stopped'})   # 无 job_id;音频留在 live/ 由重启恢复转正
        finally:
            self._reap()

    def _drain_llm(self):
        """停止时等待在飞 LLM 窗口完成(有界,超时硬停):终稿不受影响,仅丢软修正。"""
        if self._refiner is None:
            return
        deadline = time.perf_counter() + LLM_DRAIN_S
        while time.perf_counter() < deadline and not self._llm_q.empty():
            time.sleep(0.2)
        self._llm_stop.set()
        if self._llm is not None:
            self._llm.join(timeout=5)

    def _reap(self):
        """会话终结后从进程内注册表摘除(模型实例随对象回收)。"""
        with _SESSIONS_LOCK:
            if _SESSIONS.get(self.id) is self:
                del _SESSIONS[self.id]

    # ---------- Pass2 驱动线程 ----------

    def _silence_ms(self, finals, now_ms):
        """当前尾静音(毫秒):now - 最后一条未结算 final 的 t1;无 pending 时 now - cursor。"""
        pending = [f for f in finals if f.get('t1_ms', 0) > self._cursor_ms]
        if pending:
            return max(0, now_ms - max(f['t1_ms'] for f in pending))
        return max(0, now_ms - self._cursor_ms)

    def _run_pass2(self):
        """周期消费 Pass1 finals → plan_windows → skip 本侧 emit / 窗口交 Runner。
        stopping 置位后:在飞窗口跑完,做 force_tail 计划(尾窗 + 残余静音结算),
        直到游标推满 now(语音后跟静音时 last final 的 t1 < now,需要第二轮 skip
        补齐 [t_end, now])才退出;规划持续失败也不死循环,stopping 即退。单窗失败
        发 error 事件并推进游标——终稿由 job 全量重转,实时视图缺口可接受,换来
        管线不卡死。"""
        try:
            while True:
                stopping = self._pass2_stopping.is_set()
                now_ms = int(self.duration_s() * 1000)      # 累计字节口径,非墙钟
                try:
                    finals = self._engine.finals()
                    plan = plan_windows(finals, self._cursor_ms, now_ms,
                                        self._silence_ms(finals, now_ms), self._cfg,
                                        force_tail=stopping)
                except Exception:  # noqa: BLE001 引擎快照/规划失败不杀驱动:当作本轮无计划
                    plan = None
                if plan is not None:
                    for a, b in plan.skip:                   # 纯静音区间:驱动侧发 skip(Task 5 裁定)
                        self._emit({'type': 'pass2_skip', 't0': a, 't1': b})
                    for a, b in plan.windows:
                        try:
                            self._runner.run_window(a, b)    # emit pass2/pass2_skip 经回调
                        except Exception as e:  # noqa: BLE001
                            self._emit({'type': 'error',
                                        'message': 'pass2 窗口修正失败,已跳过(终稿不受影响):%s'
                                                   % type(e).__name__})
                    self._cursor_ms = plan.new_cursor_ms
                if stopping:
                    if plan is not None and plan.new_cursor_ms < now_ms:
                        continue          # 尾窗只推到 t_end:下一轮 force_tail 补齐残余静音
                    return
                time.sleep(PASS2_TICK_S)
        finally:
            self._pass2_done.set()

    def _on_pass2_event(self, ev):
        """Pass2Runner 的 emit 回调:进 WS 下行队列;pass2 事件同时喂 LLM 内部队列。"""
        self._emit(ev)
        if ev.get('type') == 'pass2' and self._refiner is not None:
            self._llm_q.put(ev)

    # ---------- 下行事件 ----------

    def _emit(self, ev):
        """分配 seq 并原子地进环形缓存与队列(保证 seq 顺序 == 队列顺序 == backlog 顺序)。"""
        with self._ev_lock:
            self._seq += 1
            ev['seq'] = self._seq
            self._backlog.append(ev)
            self.events.put(ev)
        return ev

    def backlog_after(self, last_seq):
        """resume 重放:环形缓存里 seq > last_seq 的事件(按 seq 升序)。"""
        with self._ev_lock:
            return [dict(e) for e in self._backlog if e['seq'] > last_seq]

    def _on_partial(self, text):
        self._emit({'type': 'partial', 'text': text})

    def _on_final(self, text, t0_ms, t1_ms):
        rec = {'text': text, 't0_ms': t0_ms, 't1_ms': t1_ms}
        try:
            with open(os.path.join(self._dir, JSONL), 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except OSError as e:   # 落盘失败不阻断识别流,发错误事件让客户端可见
            self._emit({'type': 'error', 'message': 'pass1 结果落盘失败:%s' % e})
        self._emit({'type': 'pass1_final', **rec})

    # ---------- 音频写线程 ----------

    def _run_writer(self):
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            self._wav.append(item)         # 先落盘再进识别流:识别可丢,录音不可丢
            pcm = np.frombuffer(item, dtype='<i2').astype(np.float32) / 32768.0
            try:
                self._engine.feed(pcm)
            except RuntimeError:
                pass                       # stop 之后迟到的字节:已落盘,不进识别流

    # ---------- 元数据 ----------

    def _save_meta(self, **updates):
        self._meta.update(updates)
        path = os.path.join(self._dir, META)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
