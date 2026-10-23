# -*- coding: utf-8 -*-
"""Pass2 窗口调度与 whisper 修正的行为测试。

三块:
  - PlanWindowsTest:决策表纯函数,毫秒级;
  - Pass2RunnerTest:切片→whisper→段落/事件(whisper-cli 用假脚本替身,真跑 GPU 的用例只有 1 个);
  - GPU 锁:离线管线(run_whisper)与 live pass2 的互斥语义,以及持锁时 Pass1 不被阻塞。

音频取 anchors.json 的 zh 条目 wav(与 Task 1/3/4 同源)。
"""
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402  复用工作台的常量与 RIFF 解析
import live_pass2  # noqa: E402
from live_pass2 import Pass2Cfg, plan_windows  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
MODEL_DIR = str(BASE / 'models' / 'streaming-zipformer-zh-en')
ZH_WAV = json.load(open(BASE / 'models' / 'bench' / 'anchors.json'))['zh']['wav']

CFG = Pass2Cfg(min_s=20, force_s=35, quiet_s=1.5, pad_ms=200)


def fin(t0_ms, t1_ms, text='x'):
    return {'text': text, 't0_ms': t0_ms, 't1_ms': t1_ms}


class PlanWindowsTest(unittest.TestCase):
    """决策表:①≥min_s+静音成窗 ②≥force_s 强制 ③<min_s 等待 ④纯静音进 skip ⑤force_tail 补尾窗。"""

    def test_case1_min_span_and_quiet(self):
        """①finals 累计 25s 且尾静音 2s → 1 窗 [cursor-200, t_end+200],游标推进到 t_end。"""
        finals = [fin(10000, 22000), fin(22000, 35000)]
        plan = plan_windows(finals, 10000, 37000, 2000, CFG)
        self.assertEqual(plan.windows, [(9800, 35200)])
        self.assertEqual(plan.skip, [])
        self.assertEqual(plan.new_cursor_ms, 35000)

    def test_case2_force_span(self):
        """②累计 38s 静音 0 → 强制成 1 窗;窗尾不得超出已有音频(截到 now)。"""
        finals = [fin(0, 38000)]
        plan = plan_windows(finals, 0, 38000, 0, CFG)
        self.assertEqual(len(plan.windows), 1)
        self.assertEqual(plan.windows[0][0], 0)          # cursor-200 钳到 0
        self.assertEqual(plan.windows[0][1], 38000)      # min(t_end+200, now)=now
        self.assertEqual(plan.new_cursor_ms, 38000)

    def test_case3_too_short_waits(self):
        """③累计 12s 静音 10s → 不成窗、不推进游星(等待更多内容或 force_tail)。"""
        plan = plan_windows([fin(5000, 17000)], 5000, 27000, 10000, CFG)
        self.assertEqual(plan.windows, [])
        self.assertEqual(plan.skip, [])
        self.assertEqual(plan.new_cursor_ms, 5000)

    def test_case4_pure_silence_skip(self):
        """④finals 空但音频在推进:≥min_s 的纯静音区间进 skip 并推进游标;不足 min_s 不动。"""
        plan = plan_windows([], 0, 30000, 30000, CFG)
        self.assertEqual(plan.windows, [])
        self.assertEqual(plan.skip, [(0, 30000)])
        self.assertEqual(plan.new_cursor_ms, 30000)
        plan2 = plan_windows([], 0, 10000, 10000, CFG)
        self.assertEqual((plan2.windows, plan2.skip, plan2.new_cursor_ms), ([], [], 0))
        # 会话收尾(force_tail)时把残余静音也结算掉
        plan3 = plan_windows([], 0, 8000, 8000, CFG, force_tail=True)
        self.assertEqual(plan3.skip, [(0, 8000)])
        self.assertEqual(plan3.new_cursor_ms, 8000)

    def test_case5_force_tail(self):
        """⑤尾窗内容 8s(<min_s):默认不成窗;调用方传 force_tail=True 补窗。"""
        finals = [fin(20000, 28000)]
        self.assertEqual(plan_windows(finals, 20000, 30000, 2000, CFG).windows, [])
        plan = plan_windows(finals, 20000, 30000, 2000, CFG, force_tail=True)
        self.assertEqual(plan.windows, [(19800, 28200)])
        self.assertEqual(plan.new_cursor_ms, 28000)

    def test_long_span_splits(self):
        """超长积压(≥force_s+min_s)按 force_s 切多窗,余量 <min_s 留给下一轮。"""
        finals = [fin(0, 90000)]
        plan = plan_windows(finals, 0, 90000, 0, CFG)
        self.assertEqual(plan.windows, [(0, 35000), (34800, 70000)])
        self.assertEqual(plan.skip, [])
        self.assertEqual(plan.new_cursor_ms, 70000)      # 余量 20s 待下一轮
        # 切一刀后余量 ≥min_s 且尾静音够 → 余量成窗收尾
        plan2 = plan_windows([fin(0, 60000)], 0, 65000, 5000, CFG)
        self.assertEqual(plan2.windows, [(0, 35000), (34800, 60200)])
        self.assertEqual(plan2.new_cursor_ms, 60000)

    def test_pending_finals_filtered_by_cursor(self):
        """游标之前已结算的 finals 不再参与计划(t1 ≤ cursor 视为已覆盖)。"""
        plan = plan_windows([fin(0, 10000), fin(10000, 12000)], 10000, 13000, 1000, CFG)
        self.assertEqual((plan.windows, plan.skip), ([], []))
        self.assertEqual(plan.new_cursor_ms, 10000)


# ---------- whisper-cli 替身:记录 argv/cwd/时序,按环境变量切换行为 ----------

FAKE_WHISPER = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, os, sys, time
argv = sys.argv[1:]
prefix = argv[argv.index('-of') + 1]
env = os.environ
if env.get('FAKE_WHISPER_ARGV_OUT'):
    with open(env['FAKE_WHISPER_ARGV_OUT'], 'w') as f:
        json.dump({'argv': argv, 'cwd': os.getcwd()}, f)
slow = float(env.get('FAKE_WHISPER_SLOW') or 0)
if slow and env.get('FAKE_WHISPER_TIMES_DIR'):
    p = os.path.join(env['FAKE_WHISPER_TIMES_DIR'], '%d.times' % os.getpid())
    with open(p, 'a') as f:
        f.write('start %.6f\\n' % time.time())
    time.sleep(slow)
    with open(p, 'a') as f:
        f.write('end %.6f\\n' % time.time())
segs = [] if env.get('FAKE_WHISPER_EMPTY') else [
    {'offsets': {'from': 0, 'to': 800}, 'text': '替身窗口文本'}]
with open(prefix + '.json', 'w') as f:
    json.dump({'transcription': segs}, f)
"""

HAVE_WHISPER = os.path.isfile(app.WHISPER_BIN)


def sine_wav(path, seconds):
    """正弦假音频(内容对 whisper 测试无关紧要,占住时间轴即可)。"""
    from live_wav import write_slice
    x = (np.sin(np.arange(int(seconds * 16000)) * 0.05) * 0.3).astype(np.float32)
    write_slice(path, x)
    return path


class Pass2RunnerTest(unittest.TestCase):
    """run_window:切片→whisper→段落 dict + emit。除标明的 1 例外均用替身,不占 GPU。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fake_bin = self.tmp / 'fake-whisper.py'
        self.fake_bin.write_text(FAKE_WHISPER)
        self.fake_bin.chmod(self.fake_bin.stat().st_mode | stat.S_IEXEC)
        self.wav = sine_wav(str(self.tmp / 'live.wav'), 3.0)
        self.events = []

    def _patch_fake(self):
        """把 live_pass2.WHISPER_BIN 换成替身(真跑 GPU 的用例不调用,保持真二进制)。"""
        p = unittest.mock.patch.object(live_pass2, 'WHISPER_BIN', str(self.fake_bin))
        p.start()
        self.addCleanup(p.stop)

    def _patch_fake(self):
        p = unittest.mock.patch.object(live_pass2, 'WHISPER_BIN', str(self.fake_bin))
        p.start()
        self.addCleanup(p.stop)

    def _setenv(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.pop(key, None) if old is None
                        else os.environ.__setitem__(key, old))

    def _runner(self, **kw):
        from live_pass2 import Pass2Runner
        return Pass2Runner(self.wav, self.events.append, **kw)

    def test_cmd_matches_run_whisper(self):
        """cmd 参数与 app.run_whisper 逐项一致:二进制后依次 -m/-f/-oj/-otxt/-olrc/-of/-pp/-l/--prompt,
        且 cwd=WHISPER_DIR;prev_tail 非空时 --prompt 让位于 prev_tail。"""
        self._patch_fake()
        argv_out = self.tmp / 'argv.json'
        self._setenv('FAKE_WHISPER_ARGV_OUT', str(argv_out))
        self._runner().run_window(0, 2000)
        rec = json.loads(argv_out.read_text())
        argv, cwd = rec['argv'], rec['cwd']   # 替身记录 sys.argv[1:],即二进制之后的全部参数
        self.assertEqual(cwd, app.WHISPER_DIR)
        wav_arg = argv[argv.index('-f') + 1]
        of_arg = argv[argv.index('-of') + 1]
        self.assertTrue(wav_arg.endswith('.wav'))
        self.assertTrue(of_arg.endswith('whisper'))
        self.assertEqual(argv, ['-m', app.WHISPER_MODEL, '-f', wav_arg,
                                '-oj', '-otxt', '-olrc', '-of', of_arg, '-pp',
                                '-l', 'zh', '--prompt', app.PROMPTS['zh']])
        # prev_tail 覆盖语言提示
        self._setenv('FAKE_WHISPER_ARGV_OUT', str(self.tmp / 'argv2.json'))
        self._runner(prev_tail='上次窗口结尾,会计准则').run_window(0, 2000)
        argv2 = json.loads((self.tmp / 'argv2.json').read_text())['argv']
        self.assertEqual(argv2[argv2.index('--prompt') + 1], '上次窗口结尾,会计准则')

    def test_run_window_pass2_event_and_segments(self):
        """有文本:返回段落 dict(时间戳折回整场时间轴并钳在窗内),emit 恰一个 pass2 事件,prev_tail 更新。"""
        self._patch_fake()
        r = self._runner()
        segs = r.run_window(1000, 2000)
        self.assertEqual(len(segs), 1)
        self.assertEqual(set(segs[0]), {'start_ms', 'end_ms', 'text'})
        self.assertEqual(segs[0]['start_ms'], 1000)     # 切片起点 + offsets.from(0)
        self.assertEqual(segs[0]['end_ms'], 1800)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['type'], 'pass2')
        self.assertEqual(set(self.events[0]), {'type', 't0', 't1', 'text'})
        self.assertEqual((self.events[0]['t0'], self.events[0]['t1']), (1000, 2000))
        self.assertEqual(self.events[0]['text'], '替身窗口文本')
        self.assertEqual(r.prev_tail, '替身窗口文本')

    def test_run_window_skip_when_no_text(self):
        """whisper 无文本(替身 FAKE_WHISPER_EMPTY)→ emit pass2_skip(无 text 键),返回 [],prev_tail 不动。"""
        self._patch_fake()
        self._setenv('FAKE_WHISPER_EMPTY', '1')
        r = self._runner(prev_tail='旧尾巴')
        self.assertEqual(r.run_window(0, 2000), [])
        self.assertEqual(self.events, [{'type': 'pass2_skip', 't0': 0, 't1': 2000}])
        self.assertEqual(r.prev_tail, '旧尾巴')

    def test_run_window_skip_when_no_audio(self):
        """窗口完全越出已录音频(无可切片样本)→ 不起子进程,直接 pass2_skip。"""
        self._patch_fake()
        r = self._runner()
        self.assertEqual(r.run_window(5000, 9000), [])
        self.assertEqual(self.events, [{'type': 'pass2_skip', 't0': 5000, 't1': 9000}])

    @unittest.skipUnless(HAVE_WHISPER, 'whisper-cli 不存在')
    def test_run_window_real_whisper(self):
        """真跑 whisper(GPU,~30s 窗):段落非空、时间戳钳在窗内,事件带非空 text。

        窗口 (4000, 34000) 覆盖 zh 锚点 job 前 30s 连续对话(bench finals 从 4s 起)。
        这是本文件唯一真占 GPU 的单测用例。"""
        from live_pass2 import Pass2Runner
        events = []
        r = Pass2Runner(ZH_WAV, events.append, language='zh')
        t0 = time.perf_counter()
        segs = r.run_window(4000, 34000)
        wall = time.perf_counter() - t0
        self.assertGreater(len(segs), 0, '30s 连续语音应有段落')
        for s in segs:
            self.assertEqual(set(s), {'start_ms', 'end_ms', 'text'})
            self.assertGreaterEqual(s['start_ms'], 4000)
            self.assertGreaterEqual(s['end_ms'], s['start_ms'])
            self.assertLessEqual(s['end_ms'], 34000)
            self.assertTrue(s['text'].strip())
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev['type'], 'pass2')
        self.assertEqual((ev['t0'], ev['t1']), (4000, 34000))
        self.assertGreater(len(ev['text'].strip()), 20)
        self.assertTrue(r.prev_tail)
        print('\n  [real whisper] %d 段, wall=%.1fs, text=%s...' % (len(segs), wall, ev['text'][:30]))


# ---------- GPU 锁:离线管线与 live pass2 的互斥(不真跑 whisper,用替身计时) ----------

class GpuLockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fake_bin = self.tmp / 'fake-whisper.py'
        self.fake_bin.write_text(FAKE_WHISPER)
        self.fake_bin.chmod(self.fake_bin.stat().st_mode | stat.S_IEXEC)
        self.wav = sine_wav(str(self.tmp / 'live.wav'), 4.0)
        self.times_dir = self.tmp / 'times'
        self.times_dir.mkdir()
        os.environ['FAKE_WHISPER_SLOW'] = '0.4'
        os.environ['FAKE_WHISPER_TIMES_DIR'] = str(self.times_dir)
        self.addCleanup(os.environ.pop, 'FAKE_WHISPER_SLOW', None)
        self.addCleanup(os.environ.pop, 'FAKE_WHISPER_TIMES_DIR', None)
        p = unittest.mock.patch.object(live_pass2, 'WHISPER_BIN', str(self.fake_bin))
        p.start()
        self.addCleanup(p.stop)

    def _runner(self):
        from live_pass2 import Pass2Runner
        return Pass2Runner(self.wav, lambda ev: None)

    def _read_pairs(self):
        pairs = []
        for f in sorted(self.times_dir.glob('*.times')):
            vals = {}
            for line in f.read_text().splitlines():
                k, v = line.split()
                vals[k] = float(v)
            pairs.append((vals['start'], vals['end']))
        return sorted(pairs)

    def test_gpu_lock_serializes(self):
        """两线程并发 run_window:第二次的子进程启动时刻 ≥ 第一次的结束时刻(锁内才允许起 whisper)。"""
        done = []

        def work(i):
            self._runner().run_window(i * 1000, i * 1000 + 2000)
            done.append(i)

        ths = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=15)
        self.assertEqual(sorted(done), [0, 1])
        pairs = self._read_pairs()
        self.assertEqual(len(pairs), 2)
        self.assertGreaterEqual(pairs[1][0], pairs[0][1],
                                '两次 whisper 子进程时间上必须串行(第二个 start ≥ 第一个 end)')

    def test_gpu_lock_held_blocks_run_window(self):
        """外部持锁时 run_window 阻塞在切窗后、whisper 前;释放后完成。"""
        self.assertTrue(app.GPU_LOCK.acquire(blocking=False))
        try:
            box = {}
            th = threading.Thread(
                target=lambda: box.update(done=self._runner().run_window(0, 2000) or True))
            th.start()
            time.sleep(1.5)                     # 替身慢 0.4s + 启动开销 <1s,若没被锁挡住早该完成
            self.assertTrue(th.is_alive(), '持锁期间 run_window 不得完成')
        finally:
            app.GPU_LOCK.release()
        th.join(timeout=10)
        self.assertFalse(th.is_alive())
        self.assertTrue(box.get('done'))

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
        jobs = self.tmp / 'jobs' / 'j1'
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

        with unittest.mock.patch.object(app, 'JOBS_DIR', str(self.tmp / 'jobs')), \
                unittest.mock.patch.object(app.subprocess, 'Popen', FakeProc):
            duration, segs = app.run_whisper('j1', str(wav), 'zh')
        self.assertTrue(seen['at_popen'], 'Popen 时必须已持锁')
        self.assertTrue(seen['at_wait'], 'wait 时必须仍持锁')
        self.assertEqual(seen['cwd'], app.WHISPER_DIR)
        self.assertFalse(app.GPU_LOCK.locked(), '返回后必须已释放')
        self.assertEqual(len(segs), 1)
        self.assertGreater(duration, 0)


if __name__ == '__main__':
    unittest.main()
