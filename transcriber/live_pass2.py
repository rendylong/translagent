# -*- coding: utf-8 -*-
"""live 会话音频的按样本区间切片读取(Pass1 final 时间戳 → 声纹聚类切片)。

live_session._on_final 用 read_wav_slice 按 final 的 [t0_ms, t1_ms) 从已落盘的
live WAV(live_wav.AppendWav 产物)切出该句音频喂声纹聚类。原 Pass2 窗口调度与
whisper 重转已于 2026-10-24 摘除,spec 见
docs/2026-10-24-live-captions-drop-pass2-and-segmentation-spec.md;
文件名保留 live_pass2 是减小 churn 的既定决策。
"""
import os
import struct

import numpy as np

SAMPLE_RATE = 16000      # live WAV 采样率(切片单位换算:毫秒 × 16 = 样本)


# ---------- 窗口化读音频(Task 9:2h 会话全量读一次 460MB,长跑内存不稳) ----------

def _wav_data_span(path):
    """RIFF 遍历定位 data 块,返回 (data 起始字节偏移, 实际样本数)。
    样本数按文件实际大小算(崩溃残留的 0 长度头文件也正确),奇数字节截断。"""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        if f.read(12)[:4] != b'RIFF':
            raise RuntimeError('不是有效的 WAV 文件:%s' % path)
        pos = 12
        while pos + 8 <= size:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, sz = struct.unpack('<4sI', hdr)
            if cid == b'data':
                n = size - (pos + 8)
                return pos + 8, n // 2
            pos += 8 + sz + (sz % 2)
    raise RuntimeError('WAV 中未找到 data 块:%s' % path)


def read_wav_slice(path, a, b):
    """只读样本区间 [a, b) 的 float32(app.read_wav_float 全量读后切片的逐位等价)。
    单窗最大 ~37s ≈ 2.4MB,长跑内存恒定。"""
    off, total = _wav_data_span(path)
    a, b = max(0, int(a)), min(int(b), total)
    if b <= a:
        return np.empty(0, dtype=np.float32)
    with open(path, 'rb') as f:
        f.seek(off + a * 2)
        raw = f.read((b - a) * 2)
    return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
