# -*- coding: utf-8 -*-
"""LiveSession 行为测试:真实流式模型 + zh 锚点音频(与 Task 1/3 bench 同源)。

慢测(约 5-20s/用例,主要是模型加载):覆盖字节锚定时间轴、事件 seq 单调、
live_meta.json 状态机三块。音频取 anchors.json 的 zh 条目 wav。
"""
import json
import random
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402  复用工作台的 RIFF 解析(勿改 app.py)
from live_session import LiveSession  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
MODEL_DIR = str(BASE / 'models' / 'streaming-zipformer-zh-en')
CHUNK = 3200          # 200ms @16k 单声道 16bit
BYTES_PER_SEC = 32000


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


class LiveSessionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.sess = LiveSession(self.root, MODEL_DIR)
        self.addCleanup(self.sess.handle_stop)   # 幂等,防模型线程泄漏
        self.sid = self.sess.start('单测会话', 2)
        self.dir = str(Path(self.root) / self.sid)

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
        for e in collected:
            if e['type'] == 'pass1_final':
                self.assertEqual({'type', 'seq', 'text', 't0_ms', 't1_ms'}, set(e))
                self.assertGreaterEqual(e['t0_ms'], 0)
                self.assertGreater(e['t1_ms'], e['t0_ms'])
        # final 同时落了 jsonl
        with open(Path(self.dir) / 'live_pass1.jsonl', encoding='utf-8') as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([{'text': x['text'], 't0_ms': x['t0_ms'], 't1_ms': x['t1_ms']} for x in lines],
                         [{'text': e['text'], 't0_ms': e['t0_ms'], 't1_ms': e['t1_ms']}
                          for e in collected if e['type'] == 'pass1_final'])

    def test_meta_lifecycle(self):
        """start→live_meta.json status='listening';handle_stop→'stopping'(幂等);wav finalize 后头部长度回填。"""
        self.assertTrue(re.fullmatch(r'\d{8}-\d{6}_[0-9a-f]{4}', self.sid), self.sid)
        with open(Path(self.dir) / 'live_meta.json', encoding='utf-8') as f:
            meta = json.load(f)
        self.assertEqual(meta['status'], 'listening')
        self.assertEqual(meta['title'], '单测会话')
        self.assertEqual(meta['speakers'], 2)
        self.assertEqual(meta['id'], self.sid)
        self.sess.handle_stop()
        self.sess.handle_stop()   # 幂等
        with open(Path(self.dir) / 'live_meta.json', encoding='utf-8') as f:
            self.assertEqual(json.load(f)['status'], 'stopping')
        import struct
        with open(Path(self.dir) / 'live_audio.wav', 'rb') as f:
            raw = f.read()
        self.assertEqual(struct.unpack('<I', raw[40:44])[0], 0, '空会话 finalize 后 data 长度=0')


if __name__ == '__main__':
    unittest.main()
