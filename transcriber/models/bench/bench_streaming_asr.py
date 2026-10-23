#!/usr/bin/env python3
"""Pass1 流式识别回放基线(spec §6.1 配置 / §9 闸门)。

用 sherpa-onnx 流式 zipformer 双语模型回放 job 的 audio16k.wav:
  - 按 200ms 块 accept_waveform,endpoint(rule2 尾静音 0.6s / rule3 句长 1.0s)切 final;
  - partial 延迟 = 每 token 首现时「音频时间轴滞后(喂到的音频尾 - 该 token 时间戳)
    + 本块解码墙钟耗时」,p50 记为 partial_lag_ms_p50;
  - CER = finals 拼接 vs transcript.json 中与 finals 时间重叠(重叠占比 ≥50% 整段计入)
    的 whisper 段落拼接,字符级编辑距离 / 参照字符数;
  - cer_ratio = CER / 0.15。0.15 是 whisper large-v3-turbo 在本目录中文会议相对真值的
    经验 CER 基线常数(真值无法离线重放,以此作分母衡量流式模型相对 whisper 的劣化倍数)。

用法:
  python3 bench_streaming_asr.py --job 20261022-130447_9bef --out stream_zh.json
  python3 bench_streaming_asr.py --job <id> --out <json> --theta      # 附 θ 标定
  python3 bench_streaming_asr.py --job <id> --out <json> --limit-sec 60  # 冒烟
  python3 bench_streaming_asr.py --self-test                          # 纯函数自校验

闸门: 中文 cer_ratio ≤ 3.0 且 partial_lag_ms_p50 ≤ 1500。
"""
import argparse
import itertools
import json
import sys
import time

import numpy as np
import sherpa_onnx
from sherpa_onnx.online_recognizer import OnlineRecognizer

sys.path.insert(0, '/Users/rendy/财务知识/transcriber')
from app import read_wav_float  # noqa: E402  (afconvert 的 extensible WAV,标准库 wave 读不了)

BASE = '/Users/rendy/财务知识/transcriber'
MODEL_DIR = BASE + '/models/streaming-zipformer-zh-en'
TITANET = BASE + '/models/speaker_embedding_titanet.onnx'
CHUNK = 3200            # 200ms @16k
SR = 16000
# endpoint 参数 = spec §6.1,Task 3 消费同一模型目录与这组值(rule1 为库默认,显式写出)
RULE1 = 2.4
RULE2 = 0.6
RULE3 = 1.0
NUM_THREADS = 2
WHISPER_CER = 0.15      # 分母常数,见文件头注释


# ---------- 纯函数(自校验覆盖) ----------

def norm_text(s):
    """CER 归一化: 去空白与标点(仅保留字母/数字/CJK),英文小写。"""
    return ''.join(ch.lower() for ch in s if not ch.isspace() and ch.isalnum())


def levenshtein(a, b):
    """字符级编辑距离。a/b 为字符串;O(len(a)*len(b)),numpy 行向量 DP。"""
    n, m = len(a), len(b)
    if n == 0:
        return m
    idx = np.arange(n + 1, dtype=np.int64)
    prev = idx.copy()
    ra = np.frombuffer(a.encode('utf-32-le'), dtype=np.uint32)
    for h in b:
        hcode = np.uint32(ord(h))
        base = np.empty(n + 1, dtype=np.int64)
        base[0] = prev[0] + 1
        np.minimum(prev[1:] + 1, prev[:-1] + (ra != hcode), out=base[1:])
        prev = idx + np.minimum.accumulate(base - idx)  # 含插入路径的前缀扫
    return int(prev[n])


def merge_spans(spans):
    """[(a,b)] → 不相交并集,升序。"""
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def build_ref(segments, spans_ms, range_ms):
    """参照文本: 与 finals 并集时间重叠占段长 ≥50% 的 whisper 段整段计入。
    segments: transcript.json['segments'];spans_ms: finals 的 (t0,t1) 毫秒;
    range_ms: 本次评估的音频范围 (0, limit_ms),段与之相交才算候选。
    返回 (ref_text, n_matched_segs, n_candidate_segs, matched_chars, candidate_chars)。"""
    union = merge_spans(spans_ms)
    lo_r, hi_r = range_ms
    parts, n_m, n_c, ch_m, ch_c = [], 0, 0, 0, 0
    for seg in segments:
        s0, s1 = seg['start_ms'], seg['end_ms']
        if s1 <= s0 or s0 >= hi_r or s1 <= lo_r:
            continue
        chars = norm_text(seg.get('text', ''))
        n_c += 1
        ch_c += len(chars)
        ov = sum(min(b, s1) - max(a, s0) for a, b in union if min(b, s1) > max(a, s0))
        if ov >= 0.5 * (s1 - s0):
            parts.append(chars)
            n_m += 1
            ch_m += len(chars)
    return ''.join(parts), n_m, n_c, ch_m, ch_c


# ---------- 流式回放 ----------

def make_recognizer():
    # sherpa-onnx 1.13.8: OnlineRecognizer() 无参,流式 transducer 走
    # from_transducer 工厂(字段名以 help() 实测为准,非 OnlineRecognizerConfig 直构)
    return OnlineRecognizer.from_transducer(
        tokens=f'{MODEL_DIR}/tokens.txt',
        encoder=f'{MODEL_DIR}/encoder-epoch-99-avg-1.onnx',
        decoder=f'{MODEL_DIR}/decoder-epoch-99-avg-1.onnx',
        joiner=f'{MODEL_DIR}/joiner-epoch-99-avg-1.onnx',
        num_threads=NUM_THREADS,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=RULE1,
        rule2_min_trailing_silence=RULE2,
        rule3_min_utterance_length=RULE3,
        decoding_method='greedy_search')


def replay(rec, audio, n_samples, verbose=True):
    """200ms 块回放。返回 (finals, lags_ms, compute_s)。"""
    stream = rec.create_stream()
    finals, lags = [], []
    compute = 0.0
    prev_ntok = 0
    utt_start = 0.0            # 当前 utterance 起点的音频时间(reset 时推进)
    last_log = time.perf_counter()
    for i in range(0, n_samples, CHUNK):
        seg = np.ascontiguousarray(audio[i:i + CHUNK], dtype=np.float32)
        t0 = time.perf_counter()
        stream.accept_waveform(SR, seg)
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        t_c = time.perf_counter() - t0
        compute += t_c
        a_end = (i + len(seg)) / SR
        r = json.loads(rec.get_result_as_json_string(stream))
        toks, tss = r.get('tokens') or [], r.get('timestamps') or []
        if len(toks) > prev_ntok:                       # 新 token 首现 → 记延迟
            for k in range(prev_ntok, len(toks)):
                # timestamps 相对 utterance 起点(reset 后归零,冒烟已验)
                spoken_at = utt_start + (tss[k] if k < len(tss) else a_end - utt_start)
                lags.append((a_end - spoken_at) * 1000 + t_c * 1000)
            prev_ntok = len(toks)
        elif len(toks) < prev_ntok:                     # partial 收缩(罕见),基线后移
            prev_ntok = len(toks)
        if rec.is_endpoint(stream):
            text = r.get('text', '')
            if text.strip():
                finals.append({'t0_ms': round(utt_start * 1000),
                               't1_ms': round(a_end * 1000), 'text': text})
            rec.reset(stream)
            utt_start = a_end
            prev_ntok = 0
        if verbose and time.perf_counter() - last_log > 30:
            print('  [%6.1f/%5.1f min] finals=%d lag_n=%d rtf=%.2f'
                  % (a_end / 60, n_samples / SR / 60, len(finals), len(lags),
                     compute / a_end), flush=True)
            last_log = time.perf_counter()
    # 尾部冲刷
    t0 = time.perf_counter()
    stream.input_finished()
    while rec.is_ready(stream):
        rec.decode_stream(stream)
    compute += time.perf_counter() - t0
    r = json.loads(rec.get_result_as_json_string(stream))
    if (r.get('text') or '').strip():
        finals.append({'t0_ms': round(utt_start * 1000),
                       't1_ms': round(n_samples / SR * 1000), 'text': r['text']})
    return finals, lags, compute


# ---------- θ 标定 ----------

def theta_calib(audio, finals, segments):
    """对 finals 音频段提 titanet 声纹,按 whisper 重叠 majority 说话人标注,
    给同/异人余弦分布分位与建议 θ(Task 6 消费)。"""
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=TITANET, num_threads=4))
    by_label = {}
    n_skip = 0
    for f in finals:
        if f['t1_ms'] - f['t0_ms'] < 600:          # 过短段声纹不稳
            n_skip += 1
            continue
        best_lab, best_ov = None, 0
        for seg in segments:
            ov = min(f['t1_ms'], seg['end_ms']) - max(f['t0_ms'], seg['start_ms'])
            if ov > best_ov:
                best_ov, best_lab = ov, seg.get('speaker')
        if best_lab is None or best_ov < 300:      # 无 whisper 覆盖,标签不可信
            n_skip += 1
            continue
        s = ext.create_stream()
        s.accept_waveform(SR, np.ascontiguousarray(
            audio[f['t0_ms'] * SR // 1000:f['t1_ms'] * SR // 1000], dtype=np.float32))
        s.input_finished()
        e = np.array(ext.compute(s))
        e = e / np.linalg.norm(e)
        by_label.setdefault(best_lab, []).append(e)
    labs = sorted(by_label)
    intra, cross = [], []
    for lab in labs:
        A = np.stack(by_label[lab])
        sims = A @ A.T
        intra.extend(sims[np.triu_indices_from(sims, k=1)].tolist())
    for a, b in itertools.combinations(labs, 2):
        cross.extend((np.stack(by_label[a]) @ np.stack(by_label[b]).T).ravel().tolist())

    def quant(xs):
        xs = np.asarray(xs)
        return {q: round(float(np.percentile(xs, q)), 4) for q in (5, 25, 50, 75, 95)}

    qi, qc = quant(intra), quant(cross)
    separated = qi[5] > qc[95]
    theta = round((qi[5] + qc[95]) / 2, 4) if separated else round((qi[50] + qc[50]) / 2, 4)
    return {
        'n_finals': len(finals), 'n_used': sum(len(v) for v in by_label.values()),
        'n_skipped': n_skip,
        'labels': {str(l): len(by_label[l]) for l in labs},
        'intra_cos': qi, 'cross_cos': qc, 'n_pairs': [len(intra), len(cross)],
        'separated': bool(separated),
        'suggested_theta': theta,
        'note': 'separated=intra_p5 > cross_p95;不分离时 suggested 为两中点均值,仅供参考',
    }


# ---------- 自校验 ----------

def self_test():
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print('%-40s %s (got=%r want=%r)' % (name, 'PASS' if good else 'FAIL', got, want))

    check('levenshtein kitten/sitting', levenshtein('kitten', 'sitting'), 3)
    check('levenshtein empty/abc', levenshtein('', 'abc'), 3)
    check('levenshtein identical', levenshtein('会计准则', '会计准则'), 0)
    check('levenshtein 中英混合', levenshtein('ai cfo', 'ai cfo!'), 1)
    check('norm_text 标点/空白', norm_text('你好,世界! Hello, WORLD. 123'),
          '你好世界helloworld123')
    check('merge_spans 相邻合并', merge_spans([(0, 100), (100, 200), (500, 600)]),
          [(0, 200), (500, 600)])
    segs = [
        {'start_ms': 0, 'end_ms': 1000, 'text': '会计准则', 'speaker': 0},
        {'start_ms': 1000, 'end_ms': 2000, 'text': '复式记账', 'speaker': 1},
        {'start_ms': 3000, 'end_ms': 4000, 'text': '不该入选', 'speaker': 0},
    ]
    ref, n_m, n_c, ch_m, ch_c = build_ref(segs, [(0, 900), (1100, 1900)], (0, 4000))
    check('build_ref 多数重叠计入', (ref, n_m, n_c), ('会计准则复式记账', 2, 3))
    check('build_ref 字符计数', (ch_m, ch_c), (8, 12))
    ref2, *_ = build_ref(segs, [(950, 1050)], (0, 4000))  # 两段各半,不足 50% 不计
    check('build_ref 不足半段不计', ref2, '')
    print('SELF-TEST', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', help='job id,如 20261022-130447_9bef')
    ap.add_argument('--out', help='结果 json 路径')
    ap.add_argument('--limit-sec', type=float, default=0, help='只回放前 N 秒(0=全量)')
    ap.add_argument('--theta', action='store_true', help='附说话人聚类 θ 标定')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if not args.job or not args.out:
        ap.error('--job 与 --out 必填(或用 --self-test)')
    job_dir = f'{BASE}/jobs/{args.job}'
    audio = read_wav_float(job_dir + '/audio16k.wav')
    segments = json.load(open(job_dir + '/transcript.json'))['segments']
    n_samples = int(args.limit_sec * SR) if args.limit_sec else len(audio)
    dur = n_samples / SR

    print('job=%s audio=%.1f min' % (args.job, dur / 60), flush=True)
    rec = make_recognizer()
    t0 = time.perf_counter()
    finals, lags, compute = replay(rec, audio, n_samples)
    wall = time.perf_counter() - t0

    hyp = ''.join(norm_text(f['text']) for f in finals)
    ref, n_m, n_c, ch_m, ch_c = build_ref(
        segments, [(f['t0_ms'], f['t1_ms']) for f in finals], (0, n_samples / SR * 1000))
    cer = (levenshtein(ref, hyp) / len(ref)) if ref else None
    cer_ratio = round(cer / WHISPER_CER, 3) if cer is not None else None
    lag_arr = np.asarray(lags)
    lag_stat = {q: round(float(np.percentile(lag_arr, q)), 1) for q in (10, 50, 90)} if len(lags) else None
    rtf = compute / dur

    out = {
        'job': args.job,
        'model': 'streaming-zipformer-zh-en (sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20)',
        'endpoint': {'rule1_min_trailing_silence': RULE1, 'rule2_min_trailing_silence': RULE2,
                     'rule3_min_utterance_length': RULE3, 'enable_endpoint': True},
        'chunk_ms': CHUNK // 16, 'num_threads': NUM_THREADS,
        'limit_sec': args.limit_sec or None,
        'audio_sec': round(dur, 1),
        'partial_lag_ms_p50': lag_stat[50] if lag_stat else None,
        'partial_lag_ms': lag_stat,
        'lag_n': len(lags),
        'cer': round(cer, 4) if cer is not None else None,
        'cer_ratio': cer_ratio,
        'ref_chars': len(ref), 'hyp_chars': len(hyp),
        'ref_segs_matched': [n_m, n_c],
        'coverage_chars': round(ch_m / ch_c, 4) if ch_c else None,
        'rtf_compute': round(rtf, 3),
        'wall_sec': round(wall, 1),
        'n_finals': len(finals),
        'finals': finals,
    }
    if args.theta:
        print('theta 标定中...', flush=True)
        out['theta'] = theta_calib(audio, finals, segments)

    with open(args.out, 'w') as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)

    print(json.dumps({k: v for k, v in out.items() if k != 'finals'},
                     ensure_ascii=False, indent=1), flush=True)
    if args.theta:
        th = out['theta']
        print('θ: intra p5=%.4f cross p95=%.4f separated=%s suggested=%.4f'
              % (th['intra_cos'][5], th['cross_cos'][95], th['separated'],
                 th['suggested_theta']))


if __name__ == '__main__':
    main()
