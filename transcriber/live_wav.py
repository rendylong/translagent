# -*- coding: utf-8 -*-
"""live 会话的追加式 WAV 写入器:16kHz/16bit/单声道,边录边落盘成标准 44 字节头 PCM WAV。

打开即写长度为 0 的标准头,音频字节流实时 append(偶数截断保护),finalize 时
seek 回填 RIFF/data 两处长度,产出与 afconvert 中间产物同构、read_wav_float 可读的文件。
纯 struct + open,无第三方依赖。
"""
import struct

import numpy as np

# RIFF(4)+size(4)+WAVE(4)+fmt (4)+16(4)+audio_fmt/ch(4)+rate/byterate(8)+align/bits(4)+data(4)+size(4)
_HEADER = '<4sI4s4sIHHIIHH4sI'
_FMT_SIZE, _PCM, _MONO, _BITS16 = 16, 1, 1, 16


def _header(data_len, sample_rate):
    """标准 44 字节 PCM 头;byte_rate = sample_rate × 2 字节(单声道 16bit)。"""
    return struct.pack(
        _HEADER,
        b'RIFF', 36 + data_len, b'WAVE',
        b'fmt ', _FMT_SIZE, _PCM, _MONO,
        sample_rate, sample_rate * 2, 2, _BITS16,
        b'data', data_len,
    )


class AppendWav:
    """追加式 PCM WAV:__init__ 建头 → append 持续写入 → finalize 回填长度。"""

    def __init__(self, path, sample_rate=16000):
        self.path = path
        self.sample_rate = sample_rate
        self._total = 0
        self._f = open(path, 'wb')
        self._f.write(_header(0, sample_rate))

    def append(self, pcm_bytes):
        """写入一段 16bit 小端 PCM 字节,返回累计数据字节数;奇数长度截断到偶数。"""
        keep = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if keep:
            self._f.write(pcm_bytes[:keep])
            self._total += keep
            self._f.flush()  # 实时字幕场景:落盘即时可见,进程意外退出不丢已录音频
        return self._total

    def finalize(self):
        """回填 RIFF 长度(offset 4)与 data 长度(offset 40),关闭文件。"""
        self._f.seek(4)
        self._f.write(struct.pack('<I', 36 + self._total))
        self._f.seek(40)
        self._f.write(struct.pack('<I', self._total))
        self._f.close()

    def duration_s(self):
        """已录时长 = 累计字节 / (sample_rate × 2)。"""
        return self._total / (self.sample_rate * 2)


def write_slice(path, samples, sample_rate=16000):
    """把 float32 样本一次写成带完整头的 WAV 切片文件(窗口切片落盘用)。"""
    x = np.asarray(samples, dtype=np.float32)
    q = np.clip(x * 32768.0, -32768.0, 32767.0)
    data = q.astype('<i2').tobytes()
    with open(path, 'wb') as f:
        f.write(_header(len(data), sample_rate))
        f.write(data)
