# -*- coding: utf-8 -*-
"""Pass1Engine 行为测试:真实流式模型 + zh 锚点 job 前 60s 音频(bench 口径回放)。

慢测(约 6-15s/用例):模型加载数秒,60s 音频按 RTF≈0.04 解码约 2-3s。
音频取 anchors.json 的 zh 条目 wav,只喂前 60s,与 Task 1 bench 同源。
"""
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402  复用工作台的 RIFF 解析(勿改 app.py)
from live_pass1 import Pass1Engine  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
MODEL_DIR = str(BASE / 'models' / 'streaming-zipformer-zh-en')
CHUNK = 3200          # 200ms @16k,与 bench 同粒度
FEED_SEC = 60
FEED_SAMPLES = FEED_SEC * 16000


def load_feed_audio():
    """zh 锚点 wav 的前 60s float32 样本(Task 1 实测同源音频)。"""
    with open(BASE / 'models' / 'bench' / 'anchors.json') as fp:
        anchors = json.load(fp)
    audio = app.read_wav_float(anchors['zh']['wav'])
    assert len(audio) >= FEED_SAMPLES, '锚点音频不足 60s'
    return audio[:FEED_SAMPLES]


class ModelFileResolutionTest(unittest.TestCase):
    """_model_file 目录解析(spec 2026-10-24 §4.2):优先非 int8,仅 int8 时取 int8,
    无匹配回落硬编码名;既有中英目录解析结果与原硬编码路径一致(零行为变化)。"""

    def test_resolution(self):
        import os
        import tempfile
        import live_pass1
        with tempfile.TemporaryDirectory() as d:
            for name in ('encoder-epoch-75-avg-11-chunk-16-left-128.int8.onnx',
                         'decoder-epoch-99-avg-1.onnx',
                         'decoder-epoch-99-avg-1.int8.onnx'):
                open(os.path.join(d, name), 'w').close()
            self.assertTrue(live_pass1._model_file(d, 'encoder', 'fb.onnx').endswith(
                'encoder-epoch-75-avg-11-chunk-16-left-128.int8.onnx'),
                '目录只有 int8 时应取 int8')
            self.assertTrue(live_pass1._model_file(d, 'decoder', 'fb.onnx').endswith(
                'decoder-epoch-99-avg-1.onnx'), '有全量版本时不取 int8')
            self.assertTrue(live_pass1._model_file(d, 'joiner', 'fb.onnx').endswith(
                'fb.onnx'), '无匹配回落硬编码名')
        self.assertEqual(
            live_pass1._model_file(MODEL_DIR, 'encoder', 'encoder-epoch-99-avg-1.onnx'),
            os.path.join(MODEL_DIR, 'encoder-epoch-99-avg-1.onnx'),
            '中英目录解析结果与原硬编码路径一致')


class EndpointRulesTest(unittest.TestCase):
    """endpoint 三规则常量钉子(spec 2026-10-24 §4.1/§5.1):RULE3 曾被按 icefall
    旧语义误读成「最小句长门槛」钉在 1.0,致每秒强切(15.7s 连续语音 13 条碎片);
    现取 sherpa-onnx 官方默认(rule1=尾静音断句 / rule2=说过话后尾静音断句 /
    rule3=句长上限强制断句)。要改值须先推翻 spec §7 决策 3(按真实会议微调)。"""

    def test_rule_constants(self):
        import live_pass1
        self.assertEqual(live_pass1.RULE1, 2.4)
        self.assertEqual(live_pass1.RULE2, 1.2)
        self.assertEqual(live_pass1.RULE3, 20.0)


class Pass1EngineTest(unittest.TestCase):
    def test_partial_then_final(self):
        """喂完后 5s 内收到 ≥1 次 partial 与 ≥1 次 final;final 满足 t1_ms > t0_ms ≥ 0,且回调在工作线程触发。"""
        audio = load_feed_audio()
        events = {'partial': [], 'final': []}
        threads = set()
        ev_lock = threading.Lock()

        def on_partial(text):
            with ev_lock:
                events['partial'].append(text)
                threads.add(threading.current_thread().name)

        def on_final(text, t0_ms, t1_ms):
            with ev_lock:
                events['final'].append((text, t0_ms, t1_ms))
                threads.add(threading.current_thread().name)

        eng = Pass1Engine(MODEL_DIR, on_partial, on_final)
        self.addCleanup(eng.stop)
        # feed 非阻塞:300 块全部入队应远快于 60s 实时
        t0 = time.perf_counter()
        for i in range(0, len(audio), CHUNK):
            eng.feed(audio[i:i + CHUNK])
        self.assertLess(time.perf_counter() - t0, 5.0, 'feed 应为非阻塞追加')
        # 喂完后 5s 内必须有事件
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            with ev_lock:
                if events['partial'] and events['final']:
                    break
            time.sleep(0.05)
        with ev_lock:
            self.assertTrue(events['partial'], '5s 内未收到 partial')
            self.assertTrue(events['final'], '5s 内未收到 final')
            text, t0_ms, t1_ms = events['final'][0]
            cb_threads = set(threads)
        self.assertTrue(text.strip())
        self.assertGreaterEqual(t0_ms, 0)
        self.assertGreater(t1_ms, t0_ms)
        self.assertNotIn(threading.current_thread().name, cb_threads, '回调须在工作线程触发')

    def test_finals_grow_monotonic(self):
        """stop 冲刷后 finals() 线程安全快照:键齐全、t1>t0≥0,且 t0_ms 单调不减。"""
        audio = load_feed_audio()
        eng = Pass1Engine(MODEL_DIR, lambda text: None, lambda *a: None)
        self.addCleanup(eng.stop)
        for i in range(0, len(audio), CHUNK):
            eng.feed(audio[i:i + CHUNK])
        eng.stop()  # 幂等:冲刷尾部残留文本后工作线程退出
        eng.stop()
        finals = eng.finals()
        self.assertGreaterEqual(len(finals), 2, '60s 会议音频应产出多个 final')
        t0_list = []
        for f in finals:
            self.assertEqual(set(f), {'text', 't0_ms', 't1_ms'})
            self.assertGreaterEqual(f['t0_ms'], 0)
            self.assertGreater(f['t1_ms'], f['t0_ms'])
            t0_list.append(f['t0_ms'])
        self.assertEqual(t0_list, sorted(t0_list), 'finals 的 t0_ms 须单调不减')


if __name__ == '__main__':
    unittest.main()
