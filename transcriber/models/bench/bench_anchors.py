#!/usr/bin/env python3
"""锚点分离度初筛: 对每个声纹模型, 在锚点窗口上提 embedding, 衡量同/异人可分性。"""
import json
import itertools
import numpy as np
import sherpa_onnx

BASE = '/Users/rendy/财务知识/transcriber'
MODELS = {
    '当前-eres2netv2-zh(基线)': BASE + '/models/speaker_embedding.onnx',
    'wespeaker-resnet34-LM': BASE + '/models/bench/wespeaker_en_voxceleb_resnet34_LM.onnx',
    'wespeaker-resnet152-LM': BASE + '/models/bench/wespeaker_en_voxceleb_resnet152_LM.onnx',
    'wespeaker-CAM++-LM': BASE + '/models/bench/wespeaker_en_voxceleb_CAM++_LM.onnx',
    '3dspeaker-campplus-en': BASE + '/models/bench/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx',
    '3dspeaker-campplus-zh-en': BASE + '/models/bench/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx',
    '3dspeaker-eres2net-en': BASE + '/models/bench/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx',
    'nemo-titanet-large': BASE + '/models/bench/nemo_en_titanet_large.onnx',
}
DATASETS = {
    '英文面试(失败案例)': ('en', ['interviewer', 'candidate']),
    '中文会议(回归参照)': ('zh', ['sp0', 'sp1']),
}

def read_wav_float(path):
    import struct
    data = open(path, 'rb').read()
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos+4], struct.unpack('<I', data[pos+4:pos+8])[0]
        if cid == b'data':
            n = size - size % 2
            return np.frombuffer(data[pos+8:pos+8+n], dtype='<i2').astype(np.float32) / 32768.0
        pos += 8 + size + (size % 2)
    raise RuntimeError('no data')

def auc(intra, cross):
    """AUC = P(intra_sim > cross_sim)"""
    wins = ties = 0
    for a in intra:
        for b in cross:
            wins += a > b
            ties += a == b
    return (wins + 0.5 * ties) / (len(intra) * len(cross))

def main():
    anchors = json.load(open(BASE + '/models/bench/anchors.json'))
    audio = {}
    for ds, (key, _) in DATASETS.items():
        audio[key] = read_wav_float(anchors[key]['wav'])

    print('%-28s %-22s %6s %6s %7s %6s %6s' % ('模型', '数据集', 'intra', 'cross', 'Δ=c-i', 'AUC', 'acc'))
    print('-' * 84)
    for mname, mpath in MODELS.items():
        ext = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=mpath, num_threads=4))
        def emb(x, a, b):
            s = ext.create_stream()
            s.accept_waveform(16000, x[int(a*16000):int(b*16000)].astype(np.float32))
            s.input_finished()
            return np.array(ext.compute(s))
        for ds, (key, labels) in DATASETS.items():
            embs = {lab: [emb(audio[key], a, b) for a, b in anchors[key][lab]] for lab in labels}
            for lab in labels:
                embs[lab] = [e / np.linalg.norm(e) for e in embs[lab]]
            intra = [float(a @ b) for lab in labels for a, b in itertools.combinations(embs[lab], 2)]
            cross = [float(a @ b) for a, b in itertools.product(embs[labels[0]], embs[labels[1]])]
            sims = np.array(intra + cross)
            best_acc = 0
            for th in np.unique(sims):
                best_acc = max(best_acc, ((sims[:len(intra)] >= th).mean() * len(intra) +
                                          (sims[len(intra):] < th).mean() * len(cross)) / (len(intra) + len(cross)))
            print('%-28s %-22s %6.3f %6.3f %+7.3f %6.3f %5.1f%%' % (
                mname, ds, np.mean(intra), np.mean(cross), np.mean(cross) - np.mean(intra),
                auc(intra, cross), best_acc * 100))
        print()

if __name__ == '__main__':
    main()
