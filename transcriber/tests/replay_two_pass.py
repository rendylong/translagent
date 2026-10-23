#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端小样:回放 bench 中文 job(zh 锚点)前 90s,Pass1+Pass2 两遍串联。

流程(模拟 Task 9 驱动线程的调度口径,音频时间轴推进):
  1. Pass1Engine 200ms 块流式回放,收集 finals;
  2. 每推进 1s 音频时间调一次 plan_windows:待修正 finals → 窗口(真跑 whisper 修正)、
     纯静音区间 → pass2_skip(驱动侧事件,不进 whisper);喂完 stop 冲刷尾部 final 后
     force_tail=True 收尾补窗;
  3. 闸门:收到 pass2 事件,且 pass2 文本合并 vs transcript.json 对应区间(与各 pass2 窗口
     时间重叠 ≥50% 的段整段计入,bench 同口径)的 CER < Pass1 finals 合并的 CER。

CER 口径复用 models/bench/bench_streaming_asr.py(norm_text/levenshtein/build_ref),
即 Task 1 基线同一把尺子。

用法: python3 tests/replay_two_pass.py [--limit-sec 90] [--tick-s 1.0] [--pace 1.0]
退出码 0 = 闸门通过,1 = 未通过。

pace:音频秒/墙钟秒,默认 1.0(实时节奏,与真实 live 会话一致)。回放若快于实时
(Pass1 RTF≈0.04),引擎解码落后于喂料位置,调度器会把未解码区间误判为纯静音,
因此默认必须按实时节奏喂;whisper 窗口期间的阻塞照常让喂料停摆(与 live 相同)。
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'models' / 'bench'))

import numpy as np  # noqa: E402

import app  # noqa: E402
from bench_streaming_asr import build_ref, levenshtein, norm_text  # noqa: E402
from live_pass1 import Pass1Engine  # noqa: E402
from live_pass2 import Pass2Cfg, Pass2Runner, plan_windows  # noqa: E402

SR = 16000
CHUNK = 3200            # 200ms,与 bench/单测同粒度


def cer(segments, spans_ms, hyp_text, range_ms):
    """参照=transcript 段中与 spans 重叠 ≥50% 的整段;返回 (cer, ref_chars, hyp_chars)。"""
    ref = build_ref(segments, spans_ms, range_ms)[0]
    hyp = norm_text(hyp_text)
    if not ref:
        return None, 0, len(hyp)
    return levenshtein(ref, hyp) / len(ref), len(ref), len(hyp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit-sec', type=float, default=90.0, help='回放前 N 秒(默认 90)')
    ap.add_argument('--tick-s', type=float, default=1.0, help='驱动节奏:每推进 N 秒音频做一次规划')
    ap.add_argument('--pace', type=float, default=1.0, help='音频秒/墙钟秒(默认 1.0 实时)')
    args = ap.parse_args()

    anchors = json.load(open(BASE / 'models' / 'bench' / 'anchors.json'))
    wav_path = anchors['zh']['wav']
    job_dir = Path(wav_path).parent
    segments = json.load(open(job_dir / 'transcript.json'))['segments']
    audio = app.read_wav_float(wav_path)[:int(args.limit_sec * SR)]
    now_ms = round(len(audio) / SR * 1000)
    range_ms = (0, now_ms)

    # ---- Pass1:流式回放,边喂边驱动 Pass2 窗口调度(驱动线程口径的简化版) ----
    finals, lock = [], threading.Lock()

    def on_final(text, t0_ms, t1_ms):
        with lock:
            finals.append({'text': text, 't0_ms': t0_ms, 't1_ms': t1_ms})

    eng = Pass1Engine(str(BASE / 'models' / 'streaming-zipformer-zh-en'),
                      lambda t: None, on_final)
    events = []
    runner = Pass2Runner(wav_path, events.append, language='zh')
    cursor = 0

    def drive(cur_ms, force_tail=False):
        """一次调度:plan → 跑窗(whisper)/发 skip 事件 → 推进游标。返回本轮窗口数。"""
        nonlocal cursor
        with lock:
            pending = [f for f in finals if f['t1_ms'] > cursor]
        last_t1 = max((f['t1_ms'] for f in pending), default=cursor)
        silence = max(0, cur_ms - last_t1)
        plan = plan_windows(pending, cursor, cur_ms, silence, Pass2Cfg(), force_tail)
        for t0, t1 in plan.windows:
            runner.run_window(t0, t1)
        for t0, t1 in plan.skip:   # 纯静音区间:驱动侧发 pass2_skip,不进 whisper
            events.append({'type': 'pass2_skip', 't0': t0, 't1': t1})
        cursor = plan.new_cursor_ms
        return len(plan.windows) + len(plan.skip)

    t0_wall = time.perf_counter()
    n_windows = 0
    next_tick = args.tick_s * 1000
    for i in range(0, len(audio), CHUNK):
        eng.feed(audio[i:i + CHUNK])
        fed_ms = round((i + min(CHUNK, len(audio) - i)) / SR * 1000)
        if fed_ms >= next_tick:
            n_windows += drive(fed_ms)
            next_tick += args.tick_s * 1000
        # 实时节奏:喂料位置不得超前墙钟(pace=1 即真实会话速率;whisper 阻塞期间自然落后)
        target_s = fed_ms / 1000 / args.pace
        while time.perf_counter() - t0_wall < target_s:
            time.sleep(min(0.02, target_s - (time.perf_counter() - t0_wall)))
    eng.stop()                      # 冲刷尾部残留(final 经回调入 finals)
    n_windows += drive(now_ms, force_tail=True)
    wall = time.perf_counter() - t0_wall

    # ---- 评估:同一把尺子(bench 口径) ----
    with lock:
        p1_finals = list(finals)
    p2_events = [e for e in events if e['type'] == 'pass2']
    skip_events = [e for e in events if e['type'] == 'pass2_skip']

    cer1, ref1, hyp1 = cer(segments, [(f['t0_ms'], f['t1_ms']) for f in p1_finals],
                           ''.join(f['text'] for f in p1_finals), range_ms)
    cer2, ref2, hyp2 = cer(segments, [(e['t0'], e['t1']) for e in p2_events],
                           ''.join(e['text'] for e in p2_events), range_ms)

    print(json.dumps({
        'job': job_dir.name, 'limit_s': args.limit_sec, 'wall_s': round(wall, 1),
        'n_pass1_finals': len(p1_finals),
        'n_pass2_windows': len(p2_events), 'n_pass2_skip': len(skip_events),
        'windows': [[e['t0'], e['t1']] for e in p2_events],
        'cursor_final_ms': cursor,
        'pass1': {'cer': round(cer1, 4), 'ref_chars': ref1, 'hyp_chars': hyp1},
        'pass2': {'cer': round(cer2, 4), 'ref_chars': ref2, 'hyp_chars': hyp2},
        'pass2_text_head': ''.join(e['text'] for e in p2_events)[:60],
    }, ensure_ascii=False, indent=1))

    ok = bool(p2_events) and cer1 is not None and cer2 is not None and cer2 < cer1
    print('GATE %s: pass2 窗口 %d 个,CER %.4f vs Pass1 %.4f(修正%s)'
          % ('PASS' if ok else 'FAIL', len(p2_events), cer2 or -1, cer1 or -1,
             '有效' if ok else '未生效'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
