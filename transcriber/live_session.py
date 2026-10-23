# -*- coding: utf-8 -*-
"""live 实时会话域:把 Task 2 的 AppendWav 落盘与 Task 3 的 Pass1Engine 组装成一个会话。

会话目录 sessions_root/<session_id>/:
  live_meta.json    会话状态机:id/title/speakers/created_at/status(listening→stopping;Task 9 再向后转)
  live_audio.wav    AppendWav 边录边落(未 finalize 时头部长度 0,崩溃读回 0 长度是安全态)
  live_pass1.jsonl  final 逐行落盘 {text, t0_ms, t1_ms},start 时建空文件

线程模型(谁写什么,单一写者):
  - 调用方(WS 接收线程,单调用者)调 start / feed_bytes / handle_stop;
  - 每会话一个音频写线程(live-writer):字节 → AppendWav.append(落盘)→ float32 → Pass1Engine.feed;
  - Pass1Engine 自己的工作线程触发 on_partial/on_final 回调:partial 直接入事件队列,
    final 由回调落 jsonl 并入队——jsonl 的所有写都发生在引擎工作线程(单一写者),
    且事件顺序与语音时间轴一致(endpoint 先于下一句 partial)。
  - 硬约束:绝不在 on_partial/on_final 回调内调 engine.stop()(join 自身线程抛 RuntimeError),
    stop 一律由会话调用方线程(handle_stop)发起。

下行事件 {'type': 'partial'|'pass1_final'|'pass2'|'llm'|'pass2_skip'|'stop_ack'|'stopped'|'error',
'seq': int, ...} 由本类统一分配 seq(单调递增、无重复),写入 events 队列(WS 层直接消费);
同时进环形缓存(上限 BACKLOG_MAX)支持 resume 按 last_seq 重放。Task 5/8 往同一队列放
pass2/llm/pass2_skip 时沿用 _emit。feed_bytes 不做任何去重:协议保证客户端不重发已确认
字节,累计字节计数即时间轴锚点,乱序到达只影响落盘顺序不影响累计时长。
"""
import json
import os
import queue
import threading
import time
import uuid
from collections import deque

import numpy as np

from live_pass1 import Pass1Engine
from live_wav import AppendWav

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2          # 16bit 单声道
BACKLOG_MAX = 1000                        # resume 重放的环形缓存上限
META = 'live_meta.json'
AUDIO = 'live_audio.wav'
JSONL = 'live_pass1.jsonl'

_STOP = object()   # 音频写线程停止哨兵(区别于任何字节串)

_SESSIONS = {}                            # session_id -> LiveSession(进程内注册表,供 resume 查找)
_SESSIONS_LOCK = threading.Lock()


def find_session(session_id):
    """按 id 找进程内会话;重启后的恢复由 Task 9 的 live_recover 负责,现返回 None。"""
    with _SESSIONS_LOCK:
        return _SESSIONS.get(session_id)


class LiveSession:
    """一场实时会议:音频落盘 + Pass1 流式识别 + 事件下行。"""

    def __init__(self, sessions_root, model_dir):
        self.sessions_root = sessions_root
        self.model_dir = model_dir
        self.id = None
        self.status = 'idle'               # idle → listening → stopping
        self.events = queue.Queue()        # WS 层直接消费的下行事件队列
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

    # ---------- 生命周期(调用方线程) ----------

    def start(self, title, speakers):
        """建会话目录三件套并起引擎,返回 session_id(时间戳_uuid4[:4],与 jobs 同构)。"""
        assert self.status == 'idle', '会话只能 start 一次'
        self.id = time.strftime('%Y%m%d-%H%M%S') + '_' + uuid.uuid4().hex[:4]
        self._dir = os.path.join(self.sessions_root, self.id)
        os.makedirs(self._dir, exist_ok=True)
        self._wav = AppendWav(os.path.join(self._dir, AUDIO), SAMPLE_RATE)
        open(os.path.join(self._dir, JSONL), 'a', encoding='utf-8').close()   # 建空文件
        self._meta = {'id': self.id, 'title': (str(title).strip() or '实时会议')[:80],
                      'speakers': int(speakers or 0), 'created_at': time.strftime('%Y-%m-%d %H:%M'),
                      'status': 'listening', 'sample_rate': SAMPLE_RATE}
        self._save_meta()
        self._engine = Pass1Engine(self.model_dir, self._on_partial, self._on_final)
        self._writer = threading.Thread(target=self._run_writer, name='live-writer-' + self.id,
                                        daemon=True)
        self._writer.start()
        self.status = 'listening'
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
        self._q.put(b[:keep])

    def duration_s(self):
        """已录时长 = 累计字节 / 32000。"""
        return self._bytes_fed / BYTES_PER_SEC

    def handle_stop(self):
        """listening→stopping:确认停止 → 排空音频 → 停引擎(尾部 final 冲刷落盘)→ finalize 音频。
        实际"转正"(生成正式任务)由 Task 9 补,故停在 stopping。幂等。"""
        if self.status != 'listening':
            return
        self.status = 'stopping'
        self._save_meta(status='stopping')
        self._emit({'type': 'stop_ack'})
        self._q.put(_STOP)
        self._writer.join()                # 写线程排空剩余字节后退出
        self._engine.stop()                # 阻塞至队列排空+尾部冲刷(flush 的 final 经回调落盘入队)
        self._wav.finalize()
        self._emit({'type': 'stopped'})

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
