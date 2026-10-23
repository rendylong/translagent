#!/usr/bin/env python3
"""诊断 20261023-124821_1374 说话人分离:重跑 diarization,落盘 dsegs,统计分布。"""
import json
import sys
import time

sys.path.insert(0, '/Users/rendy/财务知识/transcriber')
from app import run_diarization, read_wav_float, assign_speakers  # noqa: E402

WAV = '/Users/rendy/财务知识/transcriber/jobs/20261023-124821_1374/audio16k.wav'
OUT = '/Users/rendy/财务知识/transcriber/jobs/20261023-124821_1374/diag_dsegs.json'

t0 = time.time()
dsegs = run_diarization(WAV, 2, None)
print('耗时 %.1fs, 共 %d 段' % (time.time() - t0, len(dsegs)))
with open(OUT, 'w') as f:
    json.dump(dsegs, f)

from collections import defaultdict
stat = defaultdict(lambda: [0, 0.0, None, None])  # 段数, 总秒, 最早, 最晚
for s, e, sp in dsegs:
    st = stat[sp]
    st[0] += 1
    st[1] += e - s
    st[2] = s if st[2] is None else min(st[2], s)
    st[3] = e if st[3] is None else max(st[3], e)
for sp in sorted(stat):
    n, tot, lo, hi = stat[sp]
    print('说话人%d: %d段 共%.1fs 范围[%.1f, %.1f]' % (sp, n, tot, lo, hi))
print('时间戳最大值: %.3f (音频 2502.9s, 若为采样点会是千万级)' % max(e for _, e, _ in dsegs))

# 用 whisper 段模拟 assign_speakers,看是否复现全 0
wsegs = json.load(open('/Users/rendy/财务知识/transcriber/jobs/20261023-124821_1374/whisper.json'))
wsegs = [{'start_ms': t['offsets']['from'], 'end_ms': t['offsets']['to'], 'text': t['text'].strip()}
         for t in wsegs.get('transcription', []) if t.get('text', '').strip()]
wsegs = assign_speakers(wsegs, dsegs)
from collections import Counter
print('assign_speakers 复现结果:', dict(Counter(s['speaker'] for s in wsegs)))
