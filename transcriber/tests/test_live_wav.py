# -*- coding: utf-8 -*-
"""live_wav 追加式 WAV 写入器的行为测试:写盘后用 app.read_wav_float 读回验证。"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402  复用工作台的 RIFF 解析(勿改 app.py)
from live_wav import AppendWav, write_slice  # noqa: E402


class AppendWavTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / 'live.wav')

    def test_roundtrip(self):
        """3 秒已知正弦数据:追加 → finalize → read_wav_float 读回 3.0s 且样本逐点一致。"""
        vals = (np.sin(np.arange(48000) * 0.01) * 30000).astype('<i2')
        pcm = vals.tobytes()
        self.assertEqual(len(pcm), 32000 * 3)
        w = AppendWav(self.path)
        self.assertEqual(w.append(pcm), 32000 * 3)
        w.finalize()
        got = app.read_wav_float(self.path)
        self.assertEqual(len(got), 48000)
        self.assertAlmostEqual(len(got) / 16000.0, 3.0)
        np.testing.assert_array_equal(got, vals.astype(np.float32) / 32768.0)

    def test_odd_bytes_truncated(self):
        """奇数长度只落偶数字节,累计值与时长均按截断后计。"""
        w = AppendWav(self.path)
        self.assertEqual(w.append(b'\x01\x02\x03'), 2)
        self.assertAlmostEqual(w.duration_s(), 2 / 32000.0)
        w.finalize()
        with open(self.path, 'rb') as f:
            self.assertEqual(len(f.read()), 44 + 2)

    def test_header_before_finalize(self):
        """未 finalize:文件已是 44+数据,RIFF 字段=36+0、data 字段=0(未回填),duration_s 由累计字节给出。"""
        w = AppendWav(self.path)
        w.append(b'\x05' * 3200)
        with open(self.path, 'rb') as f:
            raw = f.read()
        self.assertEqual(len(raw), 44 + 3200)
        self.assertEqual(struct.unpack('<I', raw[4:8])[0], 36)
        self.assertEqual(struct.unpack('<I', raw[40:44])[0], 0)
        self.assertAlmostEqual(w.duration_s(), 0.1)

    def test_two_hour_header(self):
        """2 小时量级(32000 B/s × 7200 s = 230,400,000 字节)后 finalize,头两处长度精确回填。"""
        w = AppendWav(self.path)
        block = b'\x11\x22' * 16000  # 32000 字节 = 1 秒,重复块避免在内存构造整个文件
        total = 0
        for _ in range(7200):
            total = w.append(block)
        self.assertEqual(total, 32000 * 7200)
        w.finalize()
        with open(self.path, 'rb') as f:
            f.seek(4)
            riff_len = struct.unpack('<I', f.read(4))[0]
            f.seek(40)
            data_len = struct.unpack('<I', f.read(4))[0]
        self.assertEqual(riff_len, 36 + 32000 * 7200)
        self.assertEqual(data_len, 32000 * 7200)

    def test_write_slice(self):
        """write_slice 一次成型:标准头 + 数据,read_wav_float 读回样本一致。"""
        samples = (np.sin(np.arange(1600) * 0.05) * 30000).astype(np.float32) / 32768.0
        write_slice(self.path, samples)
        got = app.read_wav_float(self.path)
        self.assertEqual(len(got), 1600)
        np.testing.assert_allclose(got, samples, atol=1 / 32768.0)
        with open(self.path, 'rb') as f:
            f.seek(40)
            self.assertEqual(struct.unpack('<I', f.read(4))[0], 1600 * 2)


if __name__ == '__main__':
    unittest.main()
