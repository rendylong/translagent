# -*- coding: utf-8 -*-
"""Pass2 窗口调度与 whisper 修正(live 实时字幕第二遍)。

Pass1 流式识别追低延迟、文本欠佳;Pass2 把流式定稿句(finals)累积成窗口,
从已落盘的 live WAV(live_wav.AppendWav 产物,read_wav_float 可读)切音频
片段重跑 whisper-cli 修正。本模块提供:

  - plan_windows:纯决策函数(决策表见 tests/test_live_pass2.py):
      内容跨度(t_end-cursor)≥ min_s 且尾静音 ≥ quiet_s → 成窗;
      跨度 ≥ force_s 无视静音强制成窗(防无限拖延);
      跨度 < min_s 一律等待(调用方 stop 时传 force_tail=True 补尾窗);
      finals 为空的纯静音区间 ≥ min_s → 进 skip(由调用方发 pass2_skip,不进 whisper),
      force_tail 时不足 min_s 的残余静音也一并结算,保证收尾后游标 == now。
    窗口取 [cursor-pad, min(t_end+pad, now)]:±pad 给 whisper 双向语境,窗尾不越出已录音频;
    超长积压(≥ force_s+min_s)按 force_s 切多窗,余量留给下一轮。
    finals 只是调度提示(哪些音频段落已有定稿内容),窗口内容真值来自音频本身,
    因此跨游标的 final 或跳过的静音都不会造成内容丢失。

  - Pass2Runner.run_window:read_wav_slice 只读窗口区间(Task 9 改,长跑内存平稳;
    与全量读+切片逐位等价)→ write_slice 落临时切片 → GPU_LOCK
    互斥 → whisper-cli → 段落 dict(时间戳折回整场会议时间轴),并经 session_events 回调
    emit 下行事件:{type:'pass2', t0, t1, text} 或 {type:'pass2_skip', t0, t1}
    (接 LiveSession._emit 时 seq 自动补;pass2 事件只带窗口全文,逐段时间戳在返回值里)。
    窗口无可切片音频或 whisper 无文本 → pass2_skip。成功后 prev_tail 自动更新为
    本窗口结尾文本,作下一窗口 --prompt(首窗口用 app.PROMPTS 的语言提示,与离线管线一致)。
    传入 speaker_cluster(live_speaker.OnlineSpeakerCluster 等 assign(pcm)->int 对象,
    Task 6)时:每个 whisper 段落按其时间戳从窗口音频切片喂 cluster.assign,段落 dict
    增加 'speaker' 簇号,pass2 事件增加 'speaker'=窗口内时长最长段落(主导说话人)的
    簇号;不传则段落/事件与 Task 5 完全一致(无 speaker 键)。
"""
import json
import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field

import numpy as np

from app import (GPU_LOCK, PROMPTS, WHISPER_BIN, WHISPER_DIR,  # noqa: F401  GPU_LOCK 在 run_window 使用
                 WHISPER_MODEL)
from live_wav import write_slice

SAMPLE_RATE = 16000
TAIL_CHARS = 200      # prev_tail 保留的上一窗口结尾字符数(whisper --prompt 语境续接)


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


# ---------- 决策表纯函数 ----------

@dataclass
class Pass2Cfg:
    min_s: float = 20.0    # 成窗最小内容跨度(秒)
    force_s: float = 35.0  # 无视静音的强制成窗跨度(秒),也是长积压切窗步长
    quiet_s: float = 1.5   # 触发成窗所需尾静音(秒)
    pad_ms: int = 200      # 窗口两侧语境冗余(毫秒)


@dataclass
class WindowPlan:
    windows: list = field(default_factory=list)   # [(t0_ms, t1_ms)] 待 whisper 修正的窗口
    skip: list = field(default_factory=list)      # [(t0_ms, t1_ms)] 纯静音区间(调用方发 pass2_skip)
    new_cursor_ms: int = 0                        # 计划执行后的新游标


def plan_windows(finals, cursor_ms, now_ms, silence_ms, cfg=Pass2Cfg(), force_tail=False):
    """把未结算的 Pass1 finals 规划成 whisper 窗口/静音跳过。纯函数,不碰音频。

    finals: [{text, t0_ms, t1_ms}] 传入全部累积定稿句即可,t1 ≤ cursor 的自动视为已覆盖;
    silence_ms: 调用方口径的当前尾静音时长(now - 最后一条 final 的 t1,无 pending 时为
    now - cursor),只影响余量成窗判断;纯静音 skip 的判定用 now - cursor。
    """
    pending = [f for f in finals if f.get('t1_ms', 0) > cursor_ms]
    plan = WindowPlan(new_cursor_ms=cursor_ms)
    if not pending:
        if now_ms > cursor_ms and (force_tail or now_ms - cursor_ms >= cfg.min_s * 1000):
            plan.skip.append((cursor_ms, now_ms))
            plan.new_cursor_ms = now_ms
        return plan
    t_end = max(f['t1_ms'] for f in pending)
    t = cursor_ms
    while t_end - t >= (cfg.force_s + cfg.min_s) * 1000:   # 长积压:按 force_s 切窗
        plan.windows.append((max(0, t - cfg.pad_ms), t + int(cfg.force_s * 1000)))
        t += int(cfg.force_s * 1000)
    rest = t_end - t
    quiet_ok = rest >= cfg.min_s * 1000 and silence_ms >= cfg.quiet_s * 1000
    force_ok = rest >= cfg.force_s * 1000
    if quiet_ok or force_ok or (force_tail and rest > 0):
        plan.windows.append((max(0, t - cfg.pad_ms), min(t_end + cfg.pad_ms, now_ms)))
        plan.new_cursor_ms = t_end
    elif t > cursor_ms:
        plan.new_cursor_ms = t   # 已切出前几窗,余量 <min_s 等下一轮
    return plan


# ---------- 窗口修正 Runner ----------

class Pass2Runner:
    """对 live WAV 逐窗跑 whisper 修正。无内部线程:run_window 同步执行(由 Task 9 的
    驱动线程调用),窗口串行即 whisper 调用串行;GPU 互斥靠 app.GPU_LOCK。

    wav_path: live WAV 路径(AppendWav 产物或任何 read_wav_float 可读的 16k 单声道 wav);
    session_events: 下行事件回调 dict -> None(LiveSession 侧接 _emit,seq 自动补;
    测试可直接 list.append);
    prev_tail: 上一窗口结尾文本,初始作本会话第一窗口之后的 --prompt 语境;
    speaker_cluster: 可选,live_speaker.OnlineSpeakerCluster 等 assign(pcm)->int
    对象(Task 6)。传入则段落与 pass2 事件带 speaker 簇号(见模块 docstring)。
    """

    def __init__(self, wav_path, session_events, language='zh', prev_tail='',
                 speaker_cluster=None):
        self.wav_path = wav_path
        self._emit = session_events
        self.language = language
        self.prev_tail = prev_tail
        self.cluster = speaker_cluster

    def run_window(self, t0_ms, t1_ms):
        """切 [t0_ms, t1_ms) 音频跑 whisper,返回段落 list[{start_ms,end_ms,text}]
        (时间轴为整场会议),并 emit pass2/pass2_skip 事件。窗口无音频或无文本 →
        pass2_skip 且返回 []。"""
        t0_ms, t1_ms = int(t0_ms), int(t1_ms)
        total = _wav_data_span(self.wav_path)[1]      # 窗口化读(Task 9),内存与窗长成正比
        a = max(0, t0_ms * SAMPLE_RATE // 1000)
        b = min(total, t1_ms * SAMPLE_RATE // 1000)
        if b <= a:
            self._emit({'type': 'pass2_skip', 't0': t0_ms, 't1': t1_ms})
            return []
        samples = read_wav_slice(self.wav_path, a, b)
        with tempfile.TemporaryDirectory(prefix='pass2-') as td:
            slice_path = os.path.join(td, 'window.wav')
            write_slice(slice_path, samples)
            prefix = os.path.join(td, 'whisper')
            # ↓ 参数与 app.run_whisper 保持同步,勿单边改(二进制/-m/-oj/-otxt/-olrc/-of/-pp/
            #    -l/--prompt/cwd 一致;唯一差异:--prompt 优先取 prev_tail 续接上一窗口语境,
            #    prev_tail 为空时回落 app.PROMPTS 的语言提示,与离线管线相同)
            cmd = [WHISPER_BIN, '-m', WHISPER_MODEL, '-f', slice_path,
                   '-oj', '-otxt', '-olrc', '-of', prefix, '-pp']
            if self.language != 'auto':
                cmd += ['-l', self.language]
            prompt = (self.prev_tail or '').strip() or PROMPTS.get(self.language)
            if prompt:
                cmd += ['--prompt', prompt]
            with GPU_LOCK:
                proc = subprocess.Popen(cmd, cwd=WHISPER_DIR, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True)
                _, err = proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError('pass2 whisper 转录失败:' + (err or '')[-1500:])
            with open(prefix + '.json', encoding='utf-8') as f:
                data = json.load(f)
        base_ms = a * 1000 // SAMPLE_RATE           # 切片在整场时间轴上的真实起点
        end_cap = b * 1000 // SAMPLE_RATE
        segs = []
        for t in data.get('transcription', []):
            text = t.get('text', '').strip()
            if not text:
                continue
            s = base_ms + int(t['offsets']['from'])
            e = min(base_ms + int(t['offsets']['to']), end_cap)   # whisper 尾时戳可能漂出切片
            segs.append({'start_ms': s, 'end_ms': max(e, s), 'text': text})
        if not segs:
            self._emit({'type': 'pass2_skip', 't0': t0_ms, 't1': t1_ms})
            return []
        ev = None
        if self.cluster is not None:
            for s in segs:                          # 段落切片喂声纹,簇号写回段落
                sa = max(a, s['start_ms'] * SAMPLE_RATE // 1000)
                sb = min(b, s['end_ms'] * SAMPLE_RATE // 1000)
                s['speaker'] = self.cluster.assign(
                    np.ascontiguousarray(samples[sa - a:sb - a], dtype=np.float32))
            # 事件 speaker = 窗口内时长最长段落(主导说话人)的簇号
            dom = max(segs, key=lambda s: s['end_ms'] - s['start_ms'])
            ev = {'speaker': dom['speaker']}
        text = ''.join(s['text'] for s in segs)
        self.prev_tail = text[-TAIL_CHARS:]         # 下一窗口的 --prompt 语境
        self._emit(dict({'type': 'pass2', 't0': t0_ms, 't1': t1_ms, 'text': text}, **(ev or {})))
        return segs
