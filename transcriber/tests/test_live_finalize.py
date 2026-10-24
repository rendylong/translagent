# -*- coding: utf-8 -*-
"""Task 9:停止转正 / 断电恢复 / 异步停止 / LLM 接线 / pump 竞态(EventTap)。

快测:FinalizeSessionTest / RecoverTest / EventTapTest / WatchdogTest(毫秒级,不碰模型)。
慢测:StopAsyncTest / Pass2WiringTest(真实流式 Pass1,约 10-25s)。
2026-10-24 spec 摘 Pass2:Pass2 驱动单测整类退役,事件流口径改两层(绝无 pass2/pass2_skip)。
"""
import json
import os
import queue
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import live_finalize  # noqa: E402
import live_pass2  # noqa: E402
import live_session  # noqa: E402
from live_session import EventTap, LiveSession  # noqa: E402
from live_wav import AppendWav  # noqa: E402

try:
    from test_live_pass2 import FAKE_WHISPER  # noqa: E402  discover 从 tests 起跑时
except ImportError:
    from tests.test_live_pass2 import FAKE_WHISPER  # noqa: E402

from live_session import pump_events  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
MODEL_DIR = str(BASE / 'models' / 'streaming-zipformer-zh-en')
CHUNK = 3200          # 200ms @16k mono s16le


def zh_pcm_bytes(start_s, dur_s):
    """zh 锚点 wav 的 [start_s, +dur_s) 窗口转 16bit PCM 字节(与 Task 4 同源)。"""
    with open(BASE / 'models' / 'bench' / 'anchors.json') as fp:
        anchors = json.load(fp)
    x = app.read_wav_float(anchors['zh']['wav'])
    seg = x[int(start_s * 16000):int((start_s + dur_s) * 16000)]
    q = np.clip(seg * 32768.0, -32768.0, 32767.0)
    return q.astype('<i2').tobytes()


def make_live_dir(root, sid='20261023-000000_ab12', status='stopping', seconds=3.0,
                  closed=True, title='测试会话', speakers=2):
    """造假 live 会话目录:AppendWav 写 seconds 秒音频(closed=False 模拟崩溃残留 0 长度头)。"""
    d = Path(root) / sid
    d.mkdir(parents=True)
    w = AppendWav(str(d / 'live_audio.wav'))
    w.append(b'\x01\x00' * int(seconds * 16000))
    if closed:
        w.finalize()
    else:
        w._f.close()                       # 模拟崩溃:头部长度 0,句柄关闭
    (d / 'live_pass1.jsonl').touch()
    meta = {'id': sid, 'title': title, 'speakers': speakers,
            'created_at': '2026-10-23 00:00', 'status': status,
            'sample_rate': 16000, 'language': 'zh'}
    (d / 'live_meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return d


class FinalizeSessionTest(unittest.TestCase):
    """finalize_session 六步:stopping → force_tail → 头回填 → 硬链接 → meta 入队 → 删目录。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        self.q = queue.Queue()

    def test_finalize_creates_job(self):
        d = make_live_dir(self.root, seconds=3.0)
        before = (d / 'live_audio.wav').read_bytes()
        jid = live_finalize.finalize_session(str(d), speakers_hint=2,
                                             jobs_dir=str(self.jobs), job_queue=self.q)
        jdir = self.jobs / jid
        self.assertTrue(jdir.is_dir(), 'jobs/ 下应出现任务目录')
        meta = json.loads((jdir / 'meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['status'], 'queued')
        self.assertEqual(meta['source_file'], 'source.wav')
        self.assertEqual(meta['num_speakers'], 2)
        self.assertEqual(meta['language'], 'zh')
        self.assertEqual(meta['title'], '实时 测试会话')
        self.assertEqual((jdir / 'source.wav').read_bytes(), before, 'source.wav 应与 live 音频字节相同')
        self.assertEqual(self.q.get_nowait(), jid, 'job_id 应入 job_queue')
        self.assertFalse(d.exists(), 'live 目录应删除(硬链接不占双空间)')

    def test_finalize_patches_crashed_header(self):
        """崩溃残留(头部长度 0)→ 转正后 source.wav 头部回填、样本数正确。"""
        d = make_live_dir(self.root, status='listening', seconds=3.0, closed=False)
        raw = (d / 'live_audio.wav').read_bytes()
        self.assertEqual(struct.unpack('<I', raw[40:44])[0], 0, '前置:崩溃文件 data 长度=0')
        jid = live_finalize.finalize_session(str(d), speakers_hint=0,
                                             jobs_dir=str(self.jobs), job_queue=self.q)
        src = self.jobs / jid / 'source.wav'
        raw2 = src.read_bytes()
        self.assertEqual(struct.unpack('<I', raw2[40:44])[0], 3.0 * 32000)
        self.assertEqual(len(app.read_wav_float(str(src))), 3.0 * 16000)

    def test_finalize_calls_force_tail_then_links(self):
        """force_tail 在转正链路内被调用(补跑尾窗),且先于硬链接(读的还是未回填的 wav)。"""
        saw = {}

        def tail():
            saw['header_at_tail'] = struct.unpack('<I', (Path(self.root) /
                                                         '20261023-000000_ab12' /
                                                         'live_audio.wav').read_bytes()[40:44])[0]
        d = make_live_dir(self.root, closed=False, seconds=2.0)
        live_finalize.finalize_session(str(d), speakers_hint=1, force_tail=tail,
                                       jobs_dir=str(self.jobs), job_queue=self.q)
        self.assertEqual(saw.get('header_at_tail'), 0, 'force_tail 时头尚未回填')

    def test_finalize_title_no_double_prefix(self):
        d = make_live_dir(self.root, title='实时会议')
        jid = live_finalize.finalize_session(str(d), speakers_hint=1,
                                             jobs_dir=str(self.jobs), job_queue=self.q)
        meta = json.loads((self.jobs / jid / 'meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['title'], '实时会议')


class RecoverTest(unittest.TestCase):
    """live_recover(recover_all):非终态转正、终态残目录清理、空壳清理。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'live'
        self.root.mkdir(parents=True)
        self.jobs = self.root.parent / 'jobs'
        self.jobs.mkdir(exist_ok=True)
        patcher = unittest.mock.patch.object(app, 'JOBS_DIR', str(self.jobs))
        patcher.start()
        self.addCleanup(patcher.stop)
        fresh_q = queue.Queue()
        qp = unittest.mock.patch.object(app, 'job_queue', fresh_q)
        qp.start()
        self.addCleanup(qp.stop)

    def test_recover_auto_finalizes(self):
        d = make_live_dir(self.root, sid='20261023-010101_cras', status='listening',
                          seconds=3.0, closed=False)   # 断电现场:listening + 0 长度头
        out = live_finalize.recover_all(str(self.root))
        self.assertEqual(len(out), 1)
        sid, jid = out[0]
        self.assertEqual(sid, '20261023-010101_cras')
        src = self.jobs / jid / 'source.wav'
        self.assertTrue(src.is_file())
        self.assertEqual(struct.unpack('<I', src.read_bytes()[40:44])[0], 3.0 * 32000,
                         '恢复路径必须回填崩溃残留的 WAV 头')
        self.assertFalse(d.exists())
        self.assertEqual(json.loads((self.jobs / jid / 'meta.json').read_text(encoding='utf-8'))['status'],
                         'queued')

    def test_recover_cleans_finalized_leftover(self):
        """转正中途崩溃(已置 finalized 但目录残留)→ 只删目录,不重复建 job。"""
        make_live_dir(self.root, sid='20261023-020202_done', status='finalized')
        out = live_finalize.recover_all(str(self.root))
        self.assertEqual(out, [])
        self.assertFalse((self.root / '20261023-020202_done').exists())

    def test_recover_deletes_empty_shell(self):
        (self.root / '20261023-030303_dead').mkdir()
        (self.root / '20261023-030303_dead' / 'live_meta.json').write_text('{"status": "listening"}',
                                                                           encoding='utf-8')
        out = live_finalize.recover_all(str(self.root))
        self.assertEqual(out, [])
        self.assertFalse((self.root / '20261023-030303_dead').exists())

    def test_recover_ignores_underscore_and_missing_meta(self):
        (self.root / '_attic').mkdir()
        (self.root / 'no-meta-dir').mkdir()
        self.assertEqual(live_finalize.recover_all(str(self.root)), [])
        self.assertTrue((self.root / '_attic').exists())
        self.assertTrue((self.root / 'no-meta-dir').exists())


class EventTapTest(unittest.TestCase):
    """双 pump 竞态修复:断连 pump 的事件按序归还队首,新 pump 先取归还队列,不吞不乱序。"""

    def test_send_failure_redelivers_in_order(self):
        """pump1 取到 e1 后 send 失败(对端断):e1 归还队首;新 pump 必须先 e1 后 e2。"""
        q = queue.Queue()
        tap = EventTap(q)
        gone1 = threading.Event()
        q.put({'seq': 1})
        q.put({'seq': 2})
        sent = []
        self.assertIsNone(tap.deliver(lambda e: (_ for _ in ()).throw(OSError('broken')),
                                      gone1))
        gone2 = threading.Event()
        self.assertEqual(tap.deliver(sent.append, gone2)['seq'], 1)
        self.assertEqual(tap.deliver(sent.append, gone2)['seq'], 2)

    def test_gone_set_while_holding_event(self):
        """取到事件的瞬间连接断(gone 置位在锁内 get 之后):事件归还,新 pump 依序取。"""
        class TrapQueue(queue.Queue):
            def get(self, **kw):
                ev = super().get(**kw)
                trap()                    # 取到 e1 的同时旧连接判死
                return ev
        q = TrapQueue()
        tap = EventTap(q)
        gone1 = threading.Event()
        q.put({'seq': 1})
        q.put({'seq': 2})

        def trap():
            gone1.set()
        self.assertIsNone(tap.deliver(lambda e: None, gone1))
        gone2 = threading.Event()
        sent = []
        self.assertEqual(tap.deliver(sent.append, gone2)['seq'], 1)
        self.assertEqual(tap.deliver(sent.append, gone2)['seq'], 2)

    def test_gone_before_deliver_returns_none(self):
        q = queue.Queue()
        tap = EventTap(q)
        gone = threading.Event()
        gone.set()
        q.put({'seq': 1})
        self.assertIsNone(tap.deliver(lambda e: None, gone))

    def test_replayed_seq_not_resent(self):
        """seq ≤ last_seq 的事件(resume 已重放)被消费但不重发。"""
        q = queue.Queue()
        tap = EventTap(q)
        gone = threading.Event()
        q.put({'seq': 1})
        q.put({'seq': 2})
        sent = []
        self.assertEqual(tap.deliver(sent.append, gone, last_seq=1)['seq'], 1, 'e1 仍被消费')
        self.assertEqual(sent, [], 'e1(seq=last_seq)免发')
        self.assertEqual(tap.deliver(sent.append, gone, last_seq=1)['seq'], 2)
        self.assertEqual([e['seq'] for e in sent], [2])

    def test_deliver_timeout_none(self):
        tap = EventTap(queue.Queue())
        t0 = time.perf_counter()
        self.assertIsNone(tap.deliver(lambda e: None, threading.Event(), timeout=0.05))
        self.assertGreater(time.perf_counter() - t0, 0.04)


class PumpEventsTest(unittest.TestCase):
    """pump 主循环策略(app.ws_live 的 pump 真身,收口到 live_session 便于单测):

    - stopped 事件投递后退出;
    - gone 置位退出;
    - 连续 send 失败(对端已断)有界退出——修复:转正异步期间客户端断开时,
      接收线程阻塞在 sender.join(),gone 永远不会置位,若 pump 无限重试失败
      的 send 会空转烧 CPU 且 join 永不返回(活锁)。"""

    def test_pump_returns_after_stopped(self):
        q = queue.Queue()
        tap = EventTap(q)
        gone = threading.Event()
        q.put({'seq': 1, 'type': 'partial', 'text': 'x'})
        q.put({'seq': 2, 'type': 'stopped', 'job_id': 'j1'})
        sent = []
        last = pump_events(tap, sent.append, gone)
        self.assertEqual([e['type'] for e in sent], ['partial', 'stopped'])
        self.assertEqual(last, 2)

    def test_pump_exits_when_gone_set(self):
        q = queue.Queue()
        tap = EventTap(q)
        gone = threading.Event()
        gone.set()
        t0 = time.perf_counter()
        pump_events(tap, lambda e: None, gone, timeout=0.05)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_pump_exits_on_repeated_send_failures(self):
        """对端持续断开:有限次失败后 pump 必须退出(gone 永不置位也不能空转)。"""
        q = queue.Queue()
        tap = EventTap(q)
        gone = threading.Event()                 # 模拟接收线程卡在 join(),gone 不置位
        for i in range(1, 20):
            q.put({'seq': i, 'type': 'partial'})
        done = threading.Event()

        def run():
            def send(ev):
                raise OSError('broken pipe')
            pump_events(tap, send, gone, timeout=0.05)
            done.set()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(3.0)
        self.assertTrue(done.wait(0.1), 'pump 在对端断开后 3s 内未退出(活锁)')
        self.assertFalse(gone.is_set())

    def test_pump_survives_single_send_hiccup(self):
        """偶发单次失败(事件已归还 tap):不退出,下一轮重投成功。"""
        q = queue.Queue()
        tap = EventTap(q)
        gone = threading.Event()
        q.put({'seq': 1, 'type': 'partial'})
        q.put({'seq': 2, 'type': 'stopped'})
        sent = []
        state = {'n': 0}

        def flaky(ev):
            state['n'] += 1
            if state['n'] == 1:
                raise OSError('瞬断')
            sent.append(ev)
        last = pump_events(tap, flaky, gone)
        self.assertEqual(last, 2)
        self.assertEqual([e['seq'] for e in sent], [1, 2], '归还的事件应按序重投')


class _StubCluster:
    n_speakers = 1

    def assign(self, pcm):
        return 0


class FakeRefiner:
    """LLM 接线测试替身:记录调用,回固定修正/翻译。"""
    fused = False
    last_error = ''

    def __init__(self, key, lang_pair=('zh', 'en'), **kw):
        self.calls = []

    def refine(self, text, prev_refined=''):
        self.calls.append((text, prev_refined))
        return {'corrected': '修:' + text, 'translation': 'TRANS'}


def _session_case():
    """慢测公共装置:tmp root + stub finalizer。"""
    tmp = tempfile.TemporaryDirectory()
    box = {'tmp': tmp, 'finalized': [], 'job_id': 'job-fake-1'}

    def finalizer(d, speakers_hint=None):
        box['finalized'].append((d, speakers_hint))
        return box['job_id']
    return tmp, box, finalizer


class StopAsyncTest(unittest.TestCase):
    """handle_stop 立即返回;事件流 stop_ack → stopped(带 job_id)(转正异步)。"""

    def setUp(self):
        tmp, self.box, finalizer = _session_case()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        binpath = Path(tmp.name) / 'fake-whisper.py'
        binpath.write_text(FAKE_WHISPER)
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
        envp = unittest.mock.patch.dict(os.environ, {'FAKE_WHISPER_SLOW': '0.3'})
        envp.start()
        self.addCleanup(envp.stop)
        wp = unittest.mock.patch.object(live_pass2, 'WHISPER_BIN', str(binpath))
        wp.start()
        self.addCleanup(wp.stop)
        kp = unittest.mock.patch.object(live_session, 'parse_key', lambda: None)
        kp.start()
        self.addCleanup(kp.stop)
        self.sess = LiveSession(self.root, MODEL_DIR,
                                speaker_cluster=_StubCluster(), finalizer=finalizer)
        self.addCleanup(self.sess.handle_stop)
        self.sid = self.sess.start('停测', 2)

    def _collect_until(self, want, deadline_s=60):
        """收事件至 want 到达,返回 [(到达时刻, ev), ...] 与是否命中。"""
        out = []
        deadline = time.perf_counter() + deadline_s
        while time.perf_counter() < deadline:
            try:
                out.append((time.perf_counter(), self.sess.events.get(timeout=0.2)))
            except queue.Empty:
                pass
            if out and out[-1][1].get('type') == want:
                return out, True
        return out, False

    def test_stop_ack_then_stopped(self):
        pcm = zh_pcm_bytes(8.0, 4.0)
        for i in range(0, len(pcm), CHUNK):
            self.sess.feed_bytes(pcm[i:i + CHUNK])
        time.sleep(1.0)                   # 让 Pass1 消费完再停
        t0 = time.perf_counter()
        self.sess.handle_stop()
        handle_wall = time.perf_counter() - t0
        self.assertLess(handle_wall, 1.0, 'handle_stop 必须立即返回(转正异步)')
        stamped, ok = self._collect_until('stopped')
        self.assertTrue(ok, '未收到 stopped;事件:%s' % [e['type'] for _, e in stamped])
        types = [e['type'] for _, e in stamped]
        self.assertIn('stop_ack', types)
        self.assertLess(types.index('stop_ack'), types.index('stopped'),
                        'stop_ack 必须先于 stopped')
        ack_t = next(t for t, e in stamped if e['type'] == 'stop_ack')
        stopped_t, stopped = stamped[-1]
        # stop_ack 与 stopped 之间隔着驱动尾窗(替身 whisper 慢 0.3s)+ LLM 排空
        self.assertGreaterEqual(stopped_t - ack_t, 0.25,
                                'stopped 应晚于 stop_ack 至少一窗 whisper 时间')
        self.assertEqual(stopped.get('job_id'), 'job-fake-1')
        self.assertEqual(self.box['finalized'][0][1], 2, 'finalizer 收到 speakers 透传')
        self.assertIsNone(live_session.find_session(self.sid), '转正后会话应从注册表摘除')

    def test_stop_idempotent_and_double_call(self):
        self.sess.feed_bytes(b'\x00\x00' * 4000)   # 空会话不转正,喂 0.5s 走转正路径
        time.sleep(0.3)
        self.sess.handle_stop()
        self.sess.handle_stop()           # 幂等:不炸、不重复转正
        events, ok = self._collect_until('stopped')
        self.assertTrue(ok)
        self.assertEqual(len(self.box['finalized']), 1, '只转正一次')

    def test_stop_settles_cursor_to_end(self):
        """尾窗补跑后游标必须推满到总时长:语音后跟数字静音时,残余静音区间
        [t_end, now] 也要结算(驱动侧 pass2_skip),否则游标停在最后一个 final
        的 t1——force_tail 只结算到 t_end 是 plan_windows 契约,驱动负责第二轮。"""
        pcm = zh_pcm_bytes(8.0, 3.0) + b'\x00\x00' * int(2.0 * 16000)   # 3s 语音 + 2s 静音
        for i in range(0, len(pcm), CHUNK):
            self.sess.feed_bytes(pcm[i:i + CHUNK])
        time.sleep(1.0)
        self.sess.handle_stop()
        events, ok = self._collect_until('stopped')
        self.assertTrue(ok)
        types = [e['type'] for _, e in events]
        self.assertIn('pass2', types, '语音窗应经 whisper 修正')
        self.assertEqual(self.sess._cursor_ms, 5000,
                         '游标应推满到累计字节时长(5000ms),实际 %d;事件:%s'
                         % (self.sess._cursor_ms, types))

    def test_empty_session_stop_no_job(self):
        """start 后立即 stop(0 字节):无音频可转正——不建 job、清 live 目录、
        stopped 无 job_id(否则工作台出现必然失败的空音频任务)。"""
        self.sess.handle_stop()
        events, ok = self._collect_until('stopped')
        self.assertTrue(ok)
        stopped = events[-1][1]
        self.assertNotIn('job_id', stopped)
        self.assertEqual(self.box['finalized'], [], '空会话不应调 finalizer')
        self.assertFalse(Path(self.sess._dir).exists(), 'live 目录应删除')
        self.assertIsNone(live_session.find_session(self.sid))

    def test_finalize_failure_still_emits_stopped(self):
        """转正失败:error 事件可见,且仍发 stopped(无 job_id)——否则 WS pump
        永远等不到终止事件,接收线程卡死在 join,客户端也无从收尾。"""
        def boom(d, speakers_hint=None):
            raise RuntimeError('disk full')
        self.sess._finalizer = boom
        pcm = zh_pcm_bytes(8.0, 2.0)
        for i in range(0, len(pcm), CHUNK):
            self.sess.feed_bytes(pcm[i:i + CHUNK])
        time.sleep(1.0)
        self.sess.handle_stop()
        events, ok = self._collect_until('stopped')
        self.assertTrue(ok, '转正失败也必须发 stopped;事件:%s'
                        % [e['type'] for _, e in events])
        types = [e['type'] for _, e in events]
        stopped = events[-1][1]
        self.assertIn('error', types)
        self.assertNotIn('job_id', stopped)
        self.assertIsNone(live_session.find_session(self.sid), '失败路径同样要回收会话')


class Pass2WiringTest(unittest.TestCase):
    """接线口径(2026-10-24 spec 摘 Pass2 后为两层):会话事件流绝无 pass2/pass2_skip;
    pass1_final 直接喂 LLM 出 llm 事件(旧 pass2 断言所在用例的两层化改写)。"""

    def setUp(self):
        tmp, self.box, finalizer = _session_case()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        binpath = Path(tmp.name) / 'fake-whisper.py'
        binpath.write_text(FAKE_WHISPER)
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
        wp = unittest.mock.patch.object(live_pass2, 'WHISPER_BIN', str(binpath))
        wp.start()
        self.addCleanup(wp.stop)
        kp = unittest.mock.patch.object(live_session, 'parse_key', lambda: 'sk-test-key-1234567890')
        kp.start()
        self.addCleanup(kp.stop)
        rp = unittest.mock.patch.object(live_session, 'LLMRefiner', FakeRefiner)
        rp.start()
        self.addCleanup(rp.stop)
        self.sess = LiveSession(self.root, MODEL_DIR,
                                speaker_cluster=_StubCluster(), finalizer=finalizer)
        self.addCleanup(self.sess.handle_stop)
        self.sid = self.sess.start('接线', 2)

    def test_pass2_and_llm_events(self):
        pcm = zh_pcm_bytes(8.0, 6.0)
        for i in range(0, len(pcm), CHUNK):
            self.sess.feed_bytes(pcm[i:i + CHUNK])
        self.sess.handle_stop()           # 尾窗 force_tail → pass2 → llm
        got = {}
        deadline = time.perf_counter() + 60
        order = []
        while time.perf_counter() < deadline:
            try:
                ev = self.sess.events.get(timeout=0.3)
            except queue.Empty:
                if got.get('llm'):
                    break
                continue
            order.append(ev['type'])
            got.setdefault(ev['type'], ev)
            if got.get('llm') and got.get('pass2'):
                break
        self.assertIn('pass2', got, '应产出 pass2 事件;收到:%s' % order)
        self.assertEqual({'type', 't0', 't1', 'text', 'seq', 'speaker'}, set(got['pass2']))
        self.assertEqual(got['pass2']['text'], '替身窗口文本')
        self.assertEqual(got['pass2']['speaker'], 0)
        self.assertIn('llm', got, 'LLM worker 应在 pass2 后产出 llm 事件;收到:%s' % order)
        self.assertEqual(got['llm']['text'], '修:说话人0:替身窗口文本')
        self.assertEqual(got['llm']['translation'], 'TRANS')

    def test_silence_no_pass2_events(self):
        """两层口径(原 pass2_skip 驱动用例改写):纯静音会话停止,全事件流
        绝无 pass2/pass2_skip(驱动已摘,静音不再有「结算」事件)。"""
        silence = b'\x00\x00' * int(2.0 * 16000)   # 2s 数字静音
        for i in range(0, len(silence), CHUNK):
            self.sess.feed_bytes(silence[i:i + CHUNK])
        self.sess.handle_stop()
        events, ok = [], False
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            try:
                events.append(self.sess.events.get(timeout=0.3))
            except queue.Empty:
                if events and events[-1].get('type') == 'stopped':
                    ok = True
                    break
                continue
            if events[-1].get('type') == 'stopped':
                ok = True
                break
        self.assertTrue(ok, '静音会话停止应发出 stopped')
        types = [e['type'] for e in events]
        self.assertFalse([t for t in types if t in ('pass2', 'pass2_skip')],
                         'Pass2 已摘除,静音不得触发任何 pass2 族事件:%s' % types)


class WatchdogTest(unittest.TestCase):
    """孤儿会话看门狗(终审修复项 1):无字节喂入且无事件投递持续超阈值 →
    自动 handle_stop 走正常转正;会话进入 stopping 后看门狗退出。

    快测口径:不 start()(不加载 300MB 模型),阈值/周期经构造注入,并把
    _last_activity「拨回 11 分钟前」模拟孤儿,不真睡 10 分钟。"""

    def _mk_sess(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kw.setdefault('watchdog_period_s', 0.02)     # 巡检周期压到毫秒级
        return LiveSession(tmp.name, MODEL_DIR, speaker_cluster=_StubCluster(),
                           finalizer=lambda d, speakers_hint=None: 'j', **kw)

    def test_fires_handle_stop_after_idle(self):
        """最后活动拨回 661s 前(> 600s 阈值):首巡检即自动 handle_stop。"""
        sess = self._mk_sess()                       # 阈值用默认真值 600s
        calls = []
        with unittest.mock.patch.object(sess, 'handle_stop',
                                       side_effect=lambda: calls.append(1)):
            sess.status = 'listening'
            sess._last_activity = time.monotonic() - 661
            t0 = time.perf_counter()
            sess._run_watchdog()
        self.assertEqual(calls, [1], '空闲超阈值应自动 handle_stop')
        self.assertLess(time.perf_counter() - t0, 5.0)

    def test_quiet_below_threshold_keeps_waiting(self):
        """只闲 30s(< 600s):多个巡检周期内不得触发;会话正常停止(status 离开
        listening)后看门狗必须退出,不留常驻线程。"""
        sess = self._mk_sess()
        calls = []

        def fake_stop():
            calls.append(1)
            sess.status = 'stopping'
        with unittest.mock.patch.object(sess, 'handle_stop', side_effect=fake_stop):
            sess.status = 'listening'
            sess._last_activity = time.monotonic() - 30
            th = threading.Thread(target=sess._run_watchdog, daemon=True)
            th.start()
            time.sleep(0.2)                          # ≥ 数个巡检周期
            self.assertEqual(calls, [], '未超阈值不得自动停止')
            sess.status = 'stopping'                 # 模拟正常 handle_stop 置位
            th.join(2.0)
        self.assertFalse(th.is_alive(), '进入 stopping 后看门狗应退出')

    def test_exits_immediately_when_not_listening(self):
        sess = self._mk_sess(watchdog_period_s=30.0)
        sess.status = 'stopping'
        t0 = time.perf_counter()
        sess._run_watchdog()
        self.assertLess(time.perf_counter() - t0, 1.0, '非 listening 应立即退出(不睡周期)')

    def test_feed_and_tap_delivery_mark_activity(self):
        """判活的两个来源:feed_bytes(采集推进,静音也有字节)与 tap.deliver
        (pump 挂接在消费事件)都必须刷新 _last_activity。"""
        sess = self._mk_sess()
        sess.status = 'listening'
        base = sess._last_activity
        time.sleep(0.01)
        sess.feed_bytes(b'\x00\x01' * 10)
        self.assertGreater(sess._last_activity, base, 'feed_bytes 应刷新活动时间')
        base = sess._last_activity
        time.sleep(0.01)
        sess.tap._q.put({'seq': 1, 'type': 'partial'})
        got = sess.tap.deliver(lambda e: None, threading.Event())
        self.assertEqual(got['seq'], 1)
        self.assertGreater(sess._last_activity, base, 'tap 投递应刷新活动时间(pump 活跃)')


if __name__ == '__main__':
    unittest.main()
