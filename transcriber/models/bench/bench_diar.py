#!/usr/bin/env python3
"""全管线 diarization 实测: 逐模型跑英文面试全音频,输出时间占比/锚点准确率/whisper分布。"""
import json
import sys
import time
from collections import Counter

sys.path.insert(0, '/Users/rendy/财务知识/transcriber')
from app import run_diarization, assign_speakers, read_wav_float  # noqa: E402
import sherpa_onnx  # noqa: E402

BASE = '/Users/rendy/财务知识/transcriber'
JOB = BASE + '/jobs/20261023-124821_1374'
WAV = JOB + '/audio16k.wav'

RUNS = [
    ('wespeaker-resnet34-LM', BASE + '/models/bench/wespeaker_en_voxceleb_resnet34_LM.onnx'),
    ('3dspeaker-campplus-en', BASE + '/models/bench/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx'),
    ('nemo-titanet-large', BASE + '/models/speaker_embedding_titanet.onnx'),
    ('3dspeaker-campplus-zh-en', BASE + '/models/bench/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx'),
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

def anchor_acc(dsegs, anchors):
    """锚点窗口按重叠最大取簇;返回允许整体翻转的最优准确率。"""
    pred = []  # (真值组, 预测簇)
    for gi, lab in enumerate(('interviewer', 'candidate')):
        for a, b in anchors[lab]:
            best_sp, best_ov = None, -1.0
            for s, e, sp in dsegs:
                ov = min(b, e) - max(a, s)
                if ov > best_ov:
                    best_sp, best_ov = sp, ov
            if best_sp is not None:
                pred.append((gi, best_sp))
    if not pred:
        return 0.0, []
    sps = sorted({sp for _, sp in pred})
    if len(sps) < 2:
        return 0.0, pred
    best = 0.0
    for m0, m1 in ((sps[0], sps[1]), (sps[1], sps[0])):
        acc = sum((p == (m0 if g == 0 else m1)) for g, p in pred) / len(pred)
        best = max(best, acc)
    return best, pred

def main():
    anchors = json.load(open(BASE + '/models/bench/anchors.json'))['en']
    w = json.load(open(JOB + '/whisper.json'))
    wsegs = [{'start_ms': t['offsets']['from'], 'end_ms': t['offsets']['to'], 'text': t['text'].strip()}
             for t in w.get('transcription', []) if t.get('text', '').strip()]
    results = {}
    for mname, mpath in RUNS:
        print('\n===== %s =====' % mname, flush=True)
        t0 = time.time()
        try:
            dsegs = diarize_with(mpath)
        except Exception as e:
            print('  失败: %s' % str(e)[:200], flush=True)
            continue
        dt = time.time() - t0
        json.dump(dsegs, open(BASE + '/models/bench/dsegs_%s.json' % mname, 'w'))
        tot = {}
        for s, e, sp in dsegs:
            tot[sp] = tot.get(sp, 0) + (e - s)
        dist = ' / '.join('%d:%.0fs(%.0f%%)' % (sp, v, v / 25.03) for sp, v in sorted(tot.items()))
        acc, pred = anchor_acc(dsegs, anchors)
        out = assign_speakers([dict(s) for s in wsegs], dsegs)
        wdist = dict(Counter(s['speaker'] for s in out))
        print('  耗时 %.0fs | %d 段 | 占比 %s' % (dt, len(dsegs), dist), flush=True)
        print('  锚点准确率(允许翻转) %.0f%% | whisper 段分布 %s' % (acc * 100, wdist), flush=True)
        # 面试官锚点在 whisper 段上的说话人归属(应全为同一人)
        iv = set()
        for a, b in anchors['interviewer']:
            for s in out:
                if a - 1 <= s['start_ms'] / 1000 <= b:
                    iv.add(s['speaker']); break
        print('  面试官锚点落在的 whisper 说话人: %s' % (sorted(iv) or '未覆盖'), flush=True)
        results[mname] = {'dur_s': round(dt), 'n_dsegs': len(dsegs), 'balance': dist,
                          'anchor_acc': round(acc, 3), 'whisper_dist': wdist,
                          'iv_spk': sorted(iv)}
        json.dump(results, open(BASE + '/models/bench/diar_results.json', 'w'), ensure_ascii=False, indent=1)
    print('\n全部完成', flush=True)

if __name__ == '__main__':
    main()
