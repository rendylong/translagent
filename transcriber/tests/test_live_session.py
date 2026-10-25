# -*- coding: utf-8 -*-
"""LiveSession 行为测试:真实流式模型 + zh 锚点音频(与 Task 1/3 bench 同源)。

慢测(约 5-20s/用例,主要是模型加载):覆盖字节锚定时间轴、事件 seq 单调、
live_meta.json 状态机三块。音频取 anchors.json 的 zh 条目 wav(本机路径失效时
相关用例 error,与基线一致)。两层管线口径(2026-10-24 spec 摘 Pass2):
_on_final 分人/落盘域、有 key 的 LLM 接线与停止排空用替身在快测覆盖。
"""
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402  复用工作台的 RIFF 解析(勿改 app.py)
import live_session  # noqa: E402
from live_session import LiveSession  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
MODEL_DIR = str(BASE / 'models' / 'streaming-zipformer-zh-en')
CHUNK = 3200          # 200ms @16k 单声道 16bit
BYTES_PER_SEC = 32000


class _StubCluster:
    """声纹替身:不加载 titanet;记录每次喂入的切片长度,恒归簇 0。"""

    def __init__(self):
        self.seen = []
        self.n_speakers = 1

    def assign(self, pcm):
        self.seen.append(len(pcm))
        return 0


class _FlakyCluster:
    """前 fail_first 次抛异常的替身:测 _on_final 分人失败的降级。"""

    def __init__(self, fail_first=1):
        self.calls = 0
        self.fail_first = fail_first

    def assign(self, pcm):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError('titanet 崩了')
        return 3


class _RecordingRefiner:
    """LLM 假件:记录入参,corrected 原样回显(带标签进带标签出)。"""
    fused = False

    def __init__(self, key, lang_pair=('zh', 'en'), **kw):
        self.calls = []

    def refine(self, text, prev_refined=''):
        self.calls.append((text, prev_refined))
        return {'corrected': text, 'translation': 'TRANS'}


def load_zh_samples(start_s, dur_s):
    """zh 锚点 wav 的 [start_s, start_s+dur_s) 窗口 float32 样本。"""
    with open(BASE / 'models' / 'bench' / 'anchors.json') as fp:
        anchors = json.load(fp)
    audio = app.read_wav_float(anchors['zh']['wav'])
    seg = audio[int(start_s * 16000):int((start_s + dur_s) * 16000)]
    assert len(seg) == int(dur_s * 16000), '锚点音频长度不足'
    return seg


def to_pcm_bytes(samples):
    """float32 样本 → 16bit 小端 PCM 字节(与 recorder/live_wav 同口径)。"""
    q = np.clip(samples * 32768.0, -32768.0, 32767.0)
    return q.astype('<i2').tobytes()


def drain(q, out):
    while True:
        try:
            out.append(q.get_nowait())
        except Exception:  # noqa: BLE001 queue.Empty
            return out


class ParseLangTest(unittest.TestCase):
    """语言对解析:src/tgt 支持 ja 全组合;非法输入回落 ('zh','en')。spec 2026-10-24 §4.3。"""

    def test_ja_combinations_and_fallbacks(self):
        from live_session import _parse_lang
        self.assertEqual(_parse_lang('ja>zh'), ('ja', 'zh'))
        self.assertEqual(_parse_lang('zh>ja'), ('zh', 'ja'))
        self.assertEqual(_parse_lang('ja>ja'), ('ja', 'ja'))     # 不翻译
        self.assertEqual(_parse_lang('en>ja'), ('en', 'ja'))
        self.assertEqual(_parse_lang('ja>en'), ('ja', 'en'))
        self.assertEqual(_parse_lang('fr>de'), ('zh', 'en'))     # 非法语种回落
        self.assertEqual(_parse_lang('zh'), ('zh', 'en'))        # 无 '>' 结构
        self.assertEqual(_parse_lang(None), ('zh', 'en'))
        self.assertEqual(_parse_lang(' zh > ja '), ('zh', 'ja')) # 容忍空白


class LiveSessionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        # 两层管线(2026-10-24):停止异步转正(finalizer 用替身防污染真实 jobs/)、
        # 声纹用替身(不加载 titanet)、LLM 无 key 禁用(有 key 场景见 LiveSessionLLMTest)。
        kp = unittest.mock.patch.object(live_session, 'parse_key', lambda: None)
        kp.start()
        self.addCleanup(kp.stop)
        self.sess = LiveSession(self.root, MODEL_DIR, speaker_cluster=_StubCluster(),
                                finalizer=lambda d, speakers_hint=None: 'unit-test-job')
        self.addCleanup(self.sess.handle_stop)   # 幂等,防模型线程泄漏
        self.sid = self.sess.start('单测会话', 2)
        self.dir = str(Path(self.root) / self.sid)

    def _feed_silence(self, seconds):
        """喂数字静音并等 writer 落盘到位(_on_final 分人切片依赖 wav 已在盘)。"""
        self.sess.feed_bytes(b'\x00\x00' * int(seconds * 16000))
        wav = Path(self.dir) / 'live_audio.wav'
        want = 44 + int(seconds * 16000) * 2
        deadline = time.perf_counter() + 10
        while time.perf_counter() < deadline and wav.stat().st_size < want:
            time.sleep(0.05)
        self.assertGreaterEqual(wav.stat().st_size, want, 'writer 应已把静音落盘')

    def test_byte_anchor(self):
        """32000×2 字节锚定 duration_s≈2.0;批次乱序到达时累计时间轴单调不减(不去重,靠累计语义)。"""
        batches = [bytes([i]) * CHUNK for i in range(20)]   # 20×3200B = 2.0s
        order = list(range(20))
        random.Random(42).shuffle(order)
        prev = 0.0
        for i in order:
            self.sess.feed_bytes(batches[i])
            d = self.sess.duration_s()
            self.assertGreaterEqual(d, prev, '累计时间轴不得回退')
            self.assertAlmostEqual(d - prev, CHUNK / BYTES_PER_SEC, places=6)
            prev = d
        self.assertAlmostEqual(self.sess.duration_s(), 2.0, delta=0.001)
        # 写线程最终把全部字节落进 live_audio.wav(44 头 + 64000)
        wav = Path(self.dir) / 'live_audio.wav'
        deadline = time.perf_counter() + 10
        while time.perf_counter() < deadline and wav.stat().st_size < 44 + 64000:
            time.sleep(0.05)
        self.assertEqual(wav.stat().st_size, 44 + 64000)

    def test_events_seq_monotonic(self):
        """喂含语音的 10s bench 音频(8s-18s,覆盖 sp0 锚点 9.8-15.8):事件 seq 严格递增,含 partial 与完整生命周期。"""
        pcm = to_pcm_bytes(load_zh_samples(8.0, 10.0))
        for i in range(0, len(pcm), CHUNK):
            self.sess.feed_bytes(pcm[i:i + CHUNK])
        collected = []
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            drain(self.sess.events, collected)
            if any(e['type'] == 'partial' for e in collected):
                break
            time.sleep(0.1)
        self.sess.handle_stop()
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            drain(self.sess.events, collected)
            if any(e['type'] == 'stopped' for e in collected):
                break
            time.sleep(0.1)
        seqs = [e['seq'] for e in collected]
        self.assertTrue(seqs, '没有收到任何下行事件')
        self.assertEqual(seqs, sorted(seqs), '事件 seq 必须按到达顺序单调')
        self.assertEqual(len(set(seqs)), len(seqs), '事件 seq 不得重复')
        self.assertTrue(any(e['type'] == 'partial' and e.get('text', '').strip() for e in collected),
                        '10s 含语音音频应产出 partial')
        self.assertTrue(any(e['type'] == 'stop_ack' for e in collected))
        self.assertTrue(any(e['type'] == 'stopped' for e in collected))
        # 两层口径:全事件流绝无 pass2/pass2_skip(驱动已摘)
        self.assertFalse([e for e in collected if e['type'] in ('pass2', 'pass2_skip')],
                         'Pass2 已摘除,事件流不得再出现:%s' % [e['type'] for e in collected])
        for e in collected:
            if e['type'] == 'pass1_final':
                self.assertLessEqual({'type', 'seq', 'text', 't0_ms', 't1_ms', 'speaker'}, set(e))
                self.assertGreaterEqual(e['t0_ms'], 0)
                self.assertGreater(e['t1_ms'], e['t0_ms'])
                if 'speaker' in e:      # 分人成功才带 speaker(失败回落通用色,不带键)
                    self.assertIsInstance(e['speaker'], int)
        # final 同时落了 jsonl
        with open(Path(self.dir) / 'live_pass1.jsonl', encoding='utf-8') as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([{'text': x['text'], 't0_ms': x['t0_ms'], 't1_ms': x['t1_ms']} for x in lines],
                         [{'text': e['text'], 't0_ms': e['t0_ms'], 't1_ms': e['t1_ms']}
                          for e in collected if e['type'] == 'pass1_final'])

    def test_meta_lifecycle(self):
        """start→live_meta.json status='listening';handle_stop→'stopping'(幂等);wav finalize 后头部长度回填。
        喂 0.5s 数字静音:空会话(0 字节)在 Task 9 起不转正、直接删目录,走不到
        finalize 断言。"""
        self.assertTrue(re.fullmatch(r'\d{8}-\d{6}_[0-9a-f]{4}', self.sid), self.sid)
        with open(Path(self.dir) / 'live_meta.json', encoding='utf-8') as f:
            meta = json.load(f)
        self.assertEqual(meta['status'], 'listening')
        self.assertEqual(meta['title'], '单测会话')
        self.assertEqual(meta['speakers'], 2)
        self.assertEqual(meta['id'], self.sid)
        self.sess.feed_bytes(b'\x00\x00' * 4000)     # 0.5s 静音,保住转正路径
        self.sess.handle_stop()
        self.sess.handle_stop()   # 幂等
        # Task 9 起转正异步:等 stopped 事件(wav finalize 在其后)再读头部长度
        ended = []
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            drain(self.sess.events, ended)
            if any(e['type'] == 'stopped' for e in ended):
                break
            time.sleep(0.1)
        self.assertTrue(any(e['type'] == 'stopped' for e in ended), '转正线程应发出 stopped')
        import struct
        with open(Path(self.dir) / 'live_audio.wav', 'rb') as f:
            raw = f.read()
        self.assertEqual(struct.unpack('<I', raw[40:44])[0], 8000, 'finalize 后 data 长度回填 0.5s')

    def test_on_final_speaker_assign(self):
        """_on_final 四步契约(直接调,engine 在静音下不产 final 属预期):
        jsonl 落盘 → 分人切片(t1-t0=2000ms → 32000 样本)→ 事件带 speaker=簇号。"""
        self._feed_silence(4.0)                   # ≥3s:wav 必已覆盖 [1000,3000]ms
        self.sess._on_final('你好', 1000, 3000)
        collected = []
        drain(self.sess.events, collected)
        finals = [e for e in collected if e['type'] == 'pass1_final']
        self.assertEqual(len(finals), 1)
        self.assertEqual((finals[0]['text'], finals[0]['t0_ms'], finals[0]['t1_ms'],
                          finals[0]['speaker']), ('你好', 1000, 3000, 0))
        self.assertEqual(self.sess._cluster.seen, [32000],
                         '声纹收到的切片长度应为 2000ms × 16 样本/ms')
        with open(Path(self.dir) / 'live_pass1.jsonl', encoding='utf-8') as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(lines, [{'text': '你好', 't0_ms': 1000, 't1_ms': 3000}])
        self.assertTrue(self.sess._llm_q.empty(), '无 key(refiner None)不投 llm 队列')
        self.assertFalse([e for e in collected if e['type'] == 'error'], '成功路径不发 error')

    def test_on_final_speaker_failure(self):
        """cluster.assign 抛异常:本条事件无 speaker 键、不崩、不 emit error;
        后续事件恢复正常分人。"""
        self._feed_silence(6.0)
        self.sess._cluster = _FlakyCluster(fail_first=1)   # 第一次 assign 就炸
        self.sess._on_final('第一条', 1000, 3000)
        self.sess._on_final('第二条', 3000, 5000)
        collected = []
        drain(self.sess.events, collected)
        finals = [e for e in collected if e['type'] == 'pass1_final']
        self.assertEqual(len(finals), 2, '分人失败不得阻断识别流:%s'
                        % [e['type'] for e in collected])
        self.assertNotIn('speaker', finals[0], '分人失败的事件不得带 speaker 键')
        self.assertEqual(finals[1]['speaker'], 3, '后续事件恢复分人(替身簇 3)')
        self.assertFalse([e for e in collected if e['type'] == 'error'],
                         '分人失败静默降级,不 emit error')

    def test_stop_no_pass2_tail(self):
        """停止全流程(stop_ack → stopped)不再有补跑尾窗的中间事件:
        两层管线下 stop_ack 的下一个事件就是 stopped,且全事件流绝无 pass2/pass2_skip。"""
        self._feed_silence(1.0)
        self.sess.handle_stop()
        collected = []
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            drain(self.sess.events, collected)
            if any(e['type'] == 'stopped' for e in collected):
                break
            time.sleep(0.1)
        types = [e['type'] for e in collected]
        self.assertIn('stop_ack', types)
        self.assertIn('stopped', types)
        self.assertLess(types.index('stop_ack'), types.index('stopped'),
                        'stop_ack 必须先于 stopped')
        self.assertEqual(types[types.index('stop_ack') + 1], 'stopped',
                         '两层管线无尾窗补跑,stop_ack 与 stopped 之间不应有中间事件:%s' % types)
        self.assertFalse([t for t in types if t in ('pass2', 'pass2_skip')],
                         'Pass2 已摘除,事件流不得再出现:%s' % types)


class LiveSessionLLMTest(unittest.TestCase):
    """有 key 的 LLM 接线集成:pass1_final → llm 队列 → refine worker(合并批)→
    llm 事件;停止排空(_drain_llm 等「队列空且不在飞」)保证尾句 llm 在 stopped 前。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        kp = unittest.mock.patch.object(live_session, 'parse_key', lambda: 'sk-test-key-1234567890')
        kp.start()
        self.addCleanup(kp.stop)
        rp = unittest.mock.patch.object(live_session, 'LLMRefiner', _RecordingRefiner)
        rp.start()
        self.addCleanup(rp.stop)
        self.sess = LiveSession(self.root, MODEL_DIR, speaker_cluster=_StubCluster(),
                                finalizer=lambda d, speakers_hint=None: 'unit-test-job')
        self.addCleanup(self.sess.handle_stop)
        self.sid = self.sess.start('LLM 接线', 2)
        self.dir = str(Path(self.root) / self.sid)

    def _feed_silence(self, seconds):
        self.sess.feed_bytes(b'\x00\x00' * int(seconds * 16000))
        wav = Path(self.dir) / 'live_audio.wav'
        want = 44 + int(seconds * 16000) * 2
        deadline = time.perf_counter() + 10
        while time.perf_counter() < deadline and wav.stat().st_size < want:
            time.sleep(0.05)

    def test_final_reaches_llm_and_drain_keeps_tail(self):
        """静音 + 手动 _on_final → llm 事件到达(t0/t1 对应 final,seq 单调);
        紧接 handle_stop:worker 仍在攒批(等待窗 1.5s)时 drain 必须等它,
        最后一条 llm 仍在 stopped 之前(在飞排空修复的回归钉)。"""
        self._feed_silence(4.0)
        self.sess._on_final('尾句', 1000, 3000)
        self.sess.handle_stop()                  # 立即停止,不等待 worker 出批
        collected = []
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            drain(self.sess.events, collected)
            if any(e['type'] == 'stopped' for e in collected):
                break
            time.sleep(0.1)
        types = [e['type'] for e in collected]
        seqs = [e['seq'] for e in collected]
        self.assertEqual(seqs, sorted(seqs), '事件 seq 按到达顺序单调')
        self.assertEqual(len(set(seqs)), len(seqs), '事件 seq 不得重复')
        llms = [e for e in collected if e['type'] == 'llm']
        self.assertEqual(len(llms), 1, '应产出 1 条 llm 事件:%s' % types)
        self.assertEqual((llms[0]['t0'], llms[0]['t1']), (1000, 3000), 't0/t1 取自 final')
        self.assertEqual(llms[0]['speaker'], 0, '单 speaker 批:事件 speaker=簇号')
        self.assertEqual(llms[0]['text'], '尾句', '单 speaker 批:行首标签已剥离')
        self.assertEqual(llms[0]['translation'], 'TRANS')
        self.assertLess(types.index('llm'), types.index('stopped'),
                        'drain 生效:尾句 llm 必须在 stopped 之前(在飞排空)')
        self.assertEqual(self.sess._refiner.calls, [('说话人0:尾句', '')],
                         'refine 输入带说话人编号标签,首批上下文为空')
        self.assertFalse([t for t in types if t in ('pass2', 'pass2_skip')])


class GpuLockTest(unittest.TestCase):
    """GPU 锁语义(2026-10-24 自原 test_live_pass2.py 迁入,pass2 驱动两例随 Pass2
    退役;live 两层管线不碰 GPU_LOCK,此处只钉离线管线纪律与 Pass1 不受锁影响)。"""

    def test_pass1_not_blocked(self):
        """持 GPU 锁时 Pass1Engine.feed 立即返回(<50ms):Pass1 流式识别不依赖 whisper 的 GPU 锁。"""
        from live_pass1 import Pass1Engine
        eng = Pass1Engine(MODEL_DIR, lambda t: None, lambda *a: None)
        self.addCleanup(eng.stop)
        self.assertTrue(app.GPU_LOCK.acquire(blocking=False))
        try:
            noise = np.zeros(3200, dtype=np.float32)
            t0 = time.perf_counter()
            for _ in range(10):
                eng.feed(noise)
            dt = time.perf_counter() - t0
        finally:
            app.GPU_LOCK.release()
        self.assertLess(dt, 0.05, 'feed 必须非阻塞(持锁不影响 Pass1)')

    def test_app_run_whisper_holds_gpu_lock(self):
        """app.run_whisper(离线管线)在子进程存续期间持 GPU_LOCK,返回后释放(替身 Popen 验证接线)。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        jobs = Path(tmp.name) / 'jobs' / 'j1'
        jobs.mkdir(parents=True)
        (jobs / 'meta.json').write_text('{"id": "j1"}', encoding='utf-8')
        wav = jobs / 'audio16k.wav'
        wav.write_bytes(b'RIFF--placeholder--')
        seen = {}

        class FakeProc:
            def __init__(self, cmd, **kw):
                seen['at_popen'] = app.GPU_LOCK.locked()
                seen['cwd'] = kw.get('cwd')
                self.pid = 4242
                self.stderr = ['progress = 50\n']
                prefix = cmd[cmd.index('-of') + 1]
                with open(prefix + '.json', 'w', encoding='utf-8') as f:
                    json.dump({'transcription': [
                        {'offsets': {'from': 0, 'to': 900}, 'text': '离线模拟'}]}, f)

            def wait(self):
                seen['at_wait'] = app.GPU_LOCK.locked()
                return 0

        with unittest.mock.patch.object(app, 'JOBS_DIR', str(jobs.parent)), \
                unittest.mock.patch.object(app.subprocess, 'Popen', FakeProc):
            duration, segs = app.run_whisper('j1', str(wav), 'zh')
        self.assertTrue(seen['at_popen'], 'Popen 时必须已持锁')
        self.assertTrue(seen['at_wait'], 'wait 时必须仍持锁')
        self.assertEqual(seen['cwd'], app.WHISPER_DIR)
        self.assertFalse(app.GPU_LOCK.locked(), '返回后必须已释放')
        self.assertEqual(len(segs), 1)
        self.assertGreater(duration, 0)


class StoppedReasonTest(unittest.TestCase):
    """stopped 事件带 reason(spec WP6-1)三分支:empty / finalize_failed / 成功无 reason。

    不经 start()(模型只在 start 加载,worktree 无模型):LiveSession 壳 + _finish
    触碰的边缘件(writer/engine/wav)全部换最小桩,finalizer 注入,直调 _finish 同步收尾。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.sess = LiveSession(self.root, 'unused-model-dir',
                                speaker_cluster=_StubCluster())
        self.sess.id = 'reason-test'
        self.sess._dir = str(Path(self.root) / self.sess.id)
        os.makedirs(self.sess._dir)
        self.sess._meta = {'id': self.sess.id, 'title': 't', 'speakers': 0}
        self.sess.status = 'stopping'
        # _finish 的边缘件:writer 用已退出的真线程(join 立即返回),engine/wav 用空桩
        done = threading.Thread(target=lambda: None)
        done.start()
        done.join()
        self.sess._writer = done

        class _FakeEngine:
            def stop(self):
                pass

        class _FakeWav:
            def finalize(self):
                pass

        self.sess._engine = _FakeEngine()
        self.sess._wav = _FakeWav()
        # pass2 已随 drop-pass2 线摘除(合并 2026-10-25):_finish 不再等 _pass2_done,
        # 无需也无法预置该事件

    def _drain(self):
        out = []
        drain(self.sess.events, out)
        return out

    def test_empty_session_reason_empty(self):
        """0 字节空会话:stopped 带 reason='empty',无 job_id,目录直接清掉。"""
        self.sess._bytes_fed = 0
        self.sess._finish()
        evs = self._drain()
        stopped = [e for e in evs if e['type'] == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['reason'], 'empty')
        self.assertNotIn('job_id', stopped[0])
        self.assertFalse(Path(self.sess._dir).exists(), '空会话目录应被清理')

    def test_finalize_failure_reason_with_brief(self):
        """转正失败:stopped 带 reason='finalize_failed' 与 error 简要(路径打码);
        error 事件先行且文案同源打码(先前的 %s 裸异常会带本机路径);无 job_id。"""
        def boom(session_dir, speakers_hint=None):
            raise RuntimeError('wav 头损坏: /Users/rendy/live/x/live_audio.wav')

        self.sess._finalizer = boom
        self.sess._bytes_fed = 32000       # 非空:走到 finalizer 才炸
        self.sess._finish()
        evs = self._drain()
        stopped = [e for e in evs if e['type'] == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['reason'], 'finalize_failed')
        self.assertNotIn('job_id', stopped[0])
        self.assertIn('RuntimeError', stopped[0]['error'])
        self.assertIn('[路径]', stopped[0]['error'], 'error 简要须打码本机路径')
        self.assertNotIn('/Users', stopped[0]['error'])
        errs = [e for e in evs if e['type'] == 'error']
        self.assertEqual(len(errs), 1)
        self.assertTrue(errs[0]['message'].startswith('停止转正失败:'), 'error 事件文案前缀不变')
        self.assertIn('[路径]', errs[0]['message'], 'error 事件 message 须与 stopped.error 同源打码')
        self.assertNotIn('/Users', errs[0]['message'], 'error 事件 message 不得泄露本机路径')
        self.assertIn('RuntimeError', errs[0]['message'])

    def test_success_stopped_has_job_id_no_reason(self):
        """成功分支不变:带 job_id、无 reason 键(前端按有无 job_id 判成功)。"""
        self.sess._finalizer = lambda d, speakers_hint=None: 'unit-test-job'
        self.sess._bytes_fed = 32000
        self.sess._finish()
        evs = self._drain()
        stopped = [e for e in evs if e['type'] == 'stopped']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['job_id'], 'unit-test-job')
        self.assertNotIn('reason', stopped[0], '成功分支不得带 reason')


class StartRegistrationOrderTest(unittest.TestCase):
    """TOCTOU 回归钉(2026-10-25 评审 Important):start() 里 _SESSIONS 登记必须
    先于 Pass1Engine 构造(即 meta 落盘后的下一个动作)。否则「meta 已写、未注册」
    窗口内 recover_now 会把即将录制的会话目录当死目录转正、或把 44 字节空壳 wav
    直接删——两条都是音频丢失。Pass1Engine 替换桩后 start() 可无模型跑通
    (pass2 已随 drop-pass2 线摘除,合并后无 Pass2Runner 可替换)。
    """

    def test_registered_before_engine_construction(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sess = LiveSession(tmp.name, 'unused-model-dir', speaker_cluster=_StubCluster())
        self.addCleanup(sess.handle_stop)   # 幂等收尾:空会话清目录 + 摘注册
        observed = {}

        class _SpyEngine:
            def __init__(self, model_dir, on_partial, on_final):
                observed['ok'] = live_session._SESSIONS.get(sess.id) is sess

            def feed(self, pcm):
                pass

            def finals(self):
                return []

            def stop(self):
                pass

        class _StubRunner:
            def __init__(self, *a, **kw):
                pass

        with unittest.mock.patch.object(live_session, 'Pass1Engine', _SpyEngine), \
             unittest.mock.patch.object(live_session, 'parse_key', lambda: None):
            sess.start('注册顺序窗口钉', 0)
        self.assertTrue(observed.get('ok', False),
                        'Pass1Engine 构造时(meta 已落盘)会话必须已在 _SESSIONS')


if __name__ == '__main__':
    unittest.main()
