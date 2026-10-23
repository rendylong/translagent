#!/usr/bin/env python3
"""中文会议回归: 用两个最佳模型跑 10-22 的 86 分钟中文会议,对比基线分配结果。"""
import json
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/rendy/财务知识/transcriber')
from app import assign_speakers, read_wav_float  # noqa: E402
import sherpa_onnx  # noqa: E402

BASE = '/Users/rendy/财务知识/transcriber'
JOB2 = BASE + '/jobs/20261022-130447_9bef'
WAV = JOB2 + '/audio16k.wav'

RUNS = [
    ('nemo-titanet-large', BASE + '/models/speaker_embedding_titanet.onnx'),
    ('3dspeaker-eres2net-en', BASE + '/models/bench/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx'),
]

def diarize_with(mpath):
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=BASE + '/models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx')),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=mpath, num_threads=4),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=2),
        min_duration_on=0.3, min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    res = sd.process(read_wav_float(WAV))
    return [(s.start, s.end, s.speaker) for s in res.sort_by_start_time()]

# 基线在中文会议上的分配结果(视为参照)
ref = json.load(open(JOB2 + '/transcript.json'))['segments']
ref_dist = Counter(s['speaker'] for s in ref)
print('基线(当前模型)结果: %s 段' % dict(ref_dist), flush=True)

w = json.load(open(JOB2 + '/whisper.json'))
wsegs = [{'start_ms': t['offsets']['from'], 'end_ms': t['offsets']['to'], 'text': t['text'].strip()}
         for t in w.get('transcription', []) if t.get('text', '').strip()]

for mname, mpath in RUNS:
    print('\n===== %s =====' % mname, flush=True)
    t0 = time.time()
    dsegs = diarize_with(mpath)
    dt = time.time() - t0
    json.dump(dsegs, open(BASE + '/models/bench/zh_dsegs_%s.json' % mname, 'w'))
    tot = {}
    for s, e, sp in dsegs:
        tot[sp] = tot.get(sp, 0) + (e - s)
    dist = ' / '.join('%d:%.0fs(%.0f%%)' % (sp, v, v / 51.84) for sp, v in sorted(tot.items()))
    out = assign_speakers([dict(s) for s in wsegs], dsegs)
    wdist = dict(Counter(s['speaker'] for s in out))
    print('  耗时 %.0fs | %d 段 | 占比 %s | whisper 分布 %s' % (dt, len(dsegs), dist, wdist), flush=True)
    # 与基线分配的一致率(允许翻转): 按 whisper 段逐段对比
    ref_map = {0: 0, 1: 1}
    seq_ref = [s['speaker'] for s in ref]
    seq_new = [s['speaker'] for s in out]
    n = min(len(seq_ref), len(seq_new))
    for m in ({0: 0, 1: 1}, {0: 1, 1: 0}):
        agree = sum(m[seq_new[i]] == seq_ref[i] for i in range(n)) / n
        print('  与基线一致率(映射%s): %.1f%%' % (m, agree * 100), flush=True)
print('\n完成', flush=True)
