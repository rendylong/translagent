# -*- coding: utf-8 -*-
"""静音幻觉防护(2026-10-25):trim_wav_tail 尾部静音裁剪 + collapse_repeat_loops 循环折叠。

全合成数据快测(毫秒级,不碰 whisper/sherpa)。
"""
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

SR = 16000


def write_wav(path, x):
    """写标准 PCM16 单声道 wav(trim 只按 RIFF 块走,格式标签 1 或 65534 均可)。"""
    raw = (np.asarray(x, dtype=np.float32) * 32767).astype('<i2').tobytes()
    hdr = b'RIFF' + struct.pack('<I', 36 + len(raw)) + b'WAVE'
    hdr += b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, SR, SR * 2, 2, 16)
    hdr += b'data' + struct.pack('<I', len(raw))
    with open(path, 'wb') as f:
        f.write(hdr + raw)


def seg(t, text):
    return {'start_ms': int(t * 1000), 'end_ms': int((t + 1) * 1000), 'text': text}


class TrimWavTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'a.wav')

    def tearDown(self):
        self.dir.cleanup()

    def read_size(self):
        with open(self.path, 'rb') as f:
            data = f.read()
        sz = struct.unpack('<I', data[4:8])[0]
        pos = 12
        while pos + 8 <= len(data):
            cid, dsz = data[pos:pos + 4], struct.unpack('<I', data[pos + 4:pos + 8])[0]
            if cid == b'data':
                return len(data), sz, data[pos:pos + 8], dsz, pos + 8
            pos += 8 + dsz + (dsz % 1)
        raise AssertionError('no data chunk')

    def test_trims_trailing_silence(self):
        rng = np.random.RandomState(0)
        speech = rng.uniform(-0.1, 0.1, 3 * SR)          # ≈-25dB,窗窗过阈
        silence = np.zeros(5 * SR)
        write_wav(self.path, np.concatenate([speech, silence]))
        trimmed = app.trim_wav_tail(self.path)
        # 5s 静音 - 0.75s 余量 = 4.25s
        self.assertAlmostEqual(trimmed, 4.25, places=3)
        kept = app.read_wav_float(self.path)
        self.assertAlmostEqual(len(kept) / SR, 3.75, places=3)
        flen, riff_sz, data_hdr, dsz, _ = self.read_size()
        self.assertEqual(flen, riff_sz + 8)                # RIFF 尺寸 = 文件长-8
        self.assertEqual(dsz, len(kept) * 2)
        self.assertTrue(np.all(np.abs(kept) < 0.11))       # 只剩语音区

    def test_no_trim_when_tight(self):
        rng = np.random.RandomState(1)
        x = np.concatenate([rng.uniform(-0.1, 0.1, 3 * SR), np.zeros(int(0.3 * SR))])
        write_wav(self.path, x)
        self.assertIsNone(app.trim_wav_tail(self.path))    # 可裁量 <1s 不裁
        with open(self.path, 'rb') as f:
            after = f.read()
        self.assertEqual(len(after), 44 + len(x) * 2)      # 文件原样未动

    def test_no_trim_all_silent(self):
        write_wav(self.path, np.zeros(4 * SR) + 1e-6)
        self.assertIsNone(app.trim_wav_tail(self.path))    # 全程无声不裁(交给循环折叠兜底)

    def test_no_trim_speech_to_end(self):
        rng = np.random.RandomState(2)
        write_wav(self.path, rng.uniform(-0.1, 0.1, 3 * SR))
        self.assertIsNone(app.trim_wav_tail(self.path))

    def test_quiet_recording_left_alone(self):
        # 整体偏安静的录音(语音 -50dB):自适应阈下限 -42 兜住,宁可不裁也不误切
        # 真实语音,残余幻觉交由循环折叠兜底。
        rng = np.random.RandomState(3)
        x = np.concatenate([rng.uniform(-3e-3, 3e-3, 3 * SR), np.zeros(5 * SR)])
        write_wav(self.path, x)
        self.assertIsNone(app.trim_wav_tail(self.path))


class CollapseRepeatLoopsTest(unittest.TestCase):
    REAL = ['Now I am testing the transcription function.',
            "That's right, if I speak Japanese now.",
            'But it seems that you cannot recognize Japanese.',
            'Okay, I see.']
    LOOP = 'You can be able to distinguish between Chinese and English.'
    LOOP_AND = 'And you can be able to distinguish between Chinese and English.'

    def collapse(self, texts):
        return [s['text'] for s in app.collapse_repeat_loops([seg(i, t) for i, t in enumerate(texts)])]

    def test_collapses_tail_period1(self):
        texts = self.REAL + [self.LOOP] * 26
        out = self.collapse(texts)
        self.assertEqual(out, self.REAL + [self.LOOP])     # 26 段循环只留首个

    def test_collapses_alternating_variants(self):
        out = self.collapse([self.LOOP, self.LOOP_AND] * 4)  # ABAB×4,A/B 各出现 4 次
        self.assertEqual(out, [self.LOOP, self.LOOP_AND])

    def test_collapses_mixed_run_like_real_case(self):
        # 2026-10-25 实测形态:两个变体混杂的长循环
        texts = self.REAL + [self.LOOP, self.LOOP, self.LOOP_AND, self.LOOP,
                             self.LOOP_AND] + [self.LOOP_AND] * 20
        out = self.collapse(texts)
        self.assertEqual(out, self.REAL + [self.LOOP, self.LOOP_AND])

    def test_keeps_normal_variation(self):
        texts = self.REAL + ['Let me check the numbers.', 'The report is ready.']
        self.assertEqual(self.collapse(texts), texts)

    def test_keeps_short_backchannels(self):
        texts = self.REAL + ['对。'] * 6                   # 归一化后 1 字符,不折叠
        self.assertEqual(self.collapse(texts), texts)

    def test_keeps_three_repeats(self):
        texts = self.REAL + ['这个数字要再核一遍。'] * 3 + ['好,那先这样。']
        self.assertEqual(self.collapse(texts), texts)      # 3 次重复不折叠

    def test_norm_text_strips_punctuation_and_case(self):
        self.assertEqual(app._norm_text('You can, Distinguish!'), app._norm_text('you can distinguish'))
        self.assertEqual(app._norm_text('谢谢观看。'), '谢谢观看')

    def test_loop_after_loop_keeps_both_firsts(self):
        # 两段不同循环相邻(AAAA + BBBB):各留首个,中间真实句不丢
        texts = self.REAL + [self.LOOP] * 5 + ['请不吝点赞订阅。'] * 5 + self.REAL[:1]
        out = self.collapse(texts)
        self.assertEqual(out, self.REAL + [self.LOOP, '请不吝点赞订阅。', self.REAL[0]])


if __name__ == '__main__':
    unittest.main()
