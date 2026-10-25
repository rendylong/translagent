#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地会议转录服务:上传音频 → whisper 转录 → 说话人分离 → 时间戳文稿。

数据形状(每个任务一个目录 jobs/<id>/):
  meta.json      任务状态机与参数,磁盘即真相
  source.<ext>   原始上传音频
  audio16k.wav   16kHz 单声道中间产物(播放与识别共用)
  whisper.json   whisper 原始分段
  transcript.json  合并结果 {segments: [{start_ms, end_ms, speaker, text}]};speaker 是簇号 int
  transcript.md  完成后落盘的文稿(改名/改说话人后重生成,Finder 定位用)
  dsegs.json     diarization 分段 [(start_s, end_s, speaker)](单人任务由语音区合并而来)
  embeddings.json  各簇声纹签名 {cluster: [192 floats]},跨会议匹配的原料

meta.json 关键字段:
  speaker_names  {"簇号": 本任务覆盖名}(局部,优先于全局)
  speaker_map    {"簇号": {gid, sim, status}};status ∈ auto(≥0.65)/suspect(0.50~0.65)/
                 confirmed(人工确认)/new(本场新登记);gid 指向全局说话人库
  voiceprint     是否启用声纹跨会议匹配(转录设置里可关)

全局说话人库 speakers/speakers.json:
  {model, dim, speakers: [{id, name, created_at, updated_at, centroid[192], n_anchors,
    anchors: [{job_id, cluster, sim, at, vec[192]}]}]}
  匹配阈值:cosine ≥0.65 自动合并,0.50~0.65 疑似待确认,<0.50 新建。宁新建勿误并。
"""
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import deque

import numpy as np
from flask import Flask, jsonify, request, send_file, send_from_directory, abort

BASE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE, 'jobs')
STATIC_DIR = os.path.join(BASE, 'static')
MODEL_DIR = os.path.join(BASE, 'models')
REPO_ROOT = os.path.dirname(BASE)
WHISPER_DIR = os.environ.get('WHISPER_DIR', os.path.join(REPO_ROOT, '.tools', 'whisper.cpp'))
WHISPER_BIN = os.path.join(WHISPER_DIR, 'build', 'bin', 'whisper-cli')
WHISPER_MODEL = os.path.join(WHISPER_DIR, 'models', 'ggml-large-v3-turbo.bin')
SEG_MODEL = os.path.join(MODEL_DIR, 'sherpa-onnx-pyannote-segmentation-3-0', 'model.onnx')
EMB_MODEL = os.path.join(MODEL_DIR, 'speaker_embedding_titanet.onnx')  # NeMo TitaNet-large;2026-10-23 实测英文双男声场景胜出,中文零退化,对比见 models/bench/

STAGES = ['queued', 'converting', 'transcribing', 'diarizing', 'merging', 'done']
TERMINAL = {'done', 'error'}
ALLOWED_EXT = {'.m4a', '.wav', '.mp3', '.aac', '.aif', '.aiff', '.flac'}
PROMPTS = {'zh': '以下是一场中文普通话会议的逐字记录,使用简体中文。'}

SPEAKERS_JSON = os.path.join(BASE, 'speakers', 'speakers.json')
EMB_DIM = 192
SIM_AUTO, SIM_SUSPECT = 0.65, 0.50   # bench 实测:同人跨会议 0.751,异人最高 0.557

app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024
job_queue = queue.Queue()
_speakers_lock = threading.Lock()
GPU_LOCK = threading.Lock()   # whisper GPU 全局互斥:离线管线 run_whisper 与 live pass2(live_pass2)串行占用


# ---------- 任务存储(meta.json 即状态机) ----------

def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def read_meta(job_id):
    with open(os.path.join(job_dir(job_id), 'meta.json'), encoding='utf-8') as f:
        return json.load(f)


def write_meta(job_id, **fields):
    meta = read_meta(job_id)
    meta.update(fields)
    tmp = os.path.join(job_dir(job_id), 'meta.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(job_dir(job_id), 'meta.json'))
    return meta


def job_public(meta):
    out = {k: meta.get(k) for k in (
        'id', 'title', 'created_at', 'language', 'num_speakers', 'num_speakers_detected',
        'status', 'stage', 'progress', 'error', 'duration_s', 'speaker_names',
        'speaker_map', 'voiceprint', 'note')}
    out['running'] = meta.get('status') not in TERMINAL
    out['is_live'] = bool(meta.get('live_session'))   # 实时字幕会话转正的任务(历史列表徽标用)
    return out


def write_md(job_id):
    """把已完成任务的文稿写成 transcript.md,返回路径;未完成返回 None。旧任务缺文件时也会补写。"""
    meta = read_meta(job_id)
    tpath = os.path.join(job_dir(job_id), 'transcript.json')
    if meta.get('status') != 'done' or not os.path.isfile(tpath):
        return None
    with open(tpath, encoding='utf-8') as f:
        segments = json.load(f)['segments']
    content, _ = render_transcript(meta, segments, 'md')
    path = os.path.join(job_dir(job_id), 'transcript.md')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(content)
    os.replace(tmp, path)
    return path


# ---------- 管线纯函数 ----------

def single_speaker_dsegs(wsegs):
    """单人任务没有 diarization,由 whisper 语音区合并出等价的 dsegs,供声纹提取用。
    段长上限 12s:TitaNet extractor 对超长输入会内部广播失败(实测 160s 段崩溃)。"""
    spans = []
    for s in wsegs:
        st, en = s['start_ms'] / 1000, s['end_ms'] / 1000
        if spans and st - spans[-1][1] < 0.5 and en - spans[-1][0] < 12:
            spans[-1][1] = max(spans[-1][1], en)
        else:
            spans.append([st, en])
    return [(a, b, 0) for a, b in spans]


def _split_long(segs, max_len=12.0):
    out = []
    for s, e in segs:
        while e - s > max_len:
            out.append((s, s + max_len))
            s += max_len
        if e - s > 0.3:
            out.append((s, e))
    return out


def extract_cluster_embeddings(wav, dsegs, min_seg=1.5, max_segs=40):
    """每簇取 ≥min_seg 秒的 diar 段(上限 max_segs,时间上尽量均布,超长段切成 ≤12s)
    提 TitaNet 声纹,归一化均值作簇签名。返回 {cluster: np.ndarray(192,)}。"""
    import sherpa_onnx
    by_cluster = {}
    for s, e, sp in dsegs:
        if e - s >= min_seg:
            by_cluster.setdefault(sp, []).append((s, e))
    if not by_cluster:
        return {}
    samples = read_wav_float(wav)
    ext = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=4))
    out = {}
    for sp, segs in by_cluster.items():
        segs = _split_long(sorted(segs))
        if len(segs) > max_segs:
            idx = sorted({round(i * (len(segs) - 1) / (max_segs - 1)) for i in range(max_segs)})
            segs = [segs[i] for i in idx]
        vecs = []
        for s, e in segs:
            chunk = samples[int(s * 16000):int(e * 16000)]
            # whisper 尾部时间戳可能漂移出音频实际时长,越界切片直接跳过
            if len(chunk) < 16000:
                continue
            stream = ext.create_stream()
            stream.accept_waveform(16000, chunk.astype(np.float32))
            stream.input_finished()
            if not ext.is_ready(stream):
                continue
            v = np.asarray(ext.compute(stream), dtype=np.float32)
            v = v / (np.linalg.norm(v) + 1e-9)
            vecs.append(v)
        if not vecs:
            continue
        sig = np.mean(vecs, axis=0)
        out[sp] = sig / (np.linalg.norm(sig) + 1e-9)
    return out


# ---------- 全局说话人库(跨会议声纹) ----------

def load_speakers():
    with _speakers_lock:
        if not os.path.isfile(SPEAKERS_JSON):
            return {'model': os.path.basename(EMB_MODEL), 'dim': EMB_DIM, 'speakers': []}
        with open(SPEAKERS_JSON, encoding='utf-8') as f:
            return json.load(f)


def save_speakers(lib):
    with _speakers_lock:
        os.makedirs(os.path.dirname(SPEAKERS_JSON), exist_ok=True)
        tmp = SPEAKERS_JSON + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(lib, f, ensure_ascii=False)
        os.replace(tmp, SPEAKERS_JSON)


def spk_add_anchor(spk, job_id, cluster, sim, vec):
    """追加一场会议的簇签名锚点并重算质心;同任务重跑先去重。幂等可重放。"""
    spk.setdefault('anchors', [])
    spk['anchors'] = [a for a in spk['anchors'] if not (a.get('job_id') == job_id and a.get('cluster') == cluster)]
    spk['anchors'].append({'job_id': job_id, 'cluster': int(cluster), 'sim': round(float(sim), 3),
                           'at': time.strftime('%Y-%m-%d %H:%M'),
                           'vec': [round(float(x), 6) for x in vec]})
    vecs = [np.asarray(a['vec'], dtype=np.float32) for a in spk['anchors']]
    c = np.mean(vecs, axis=0)
    c = c / (np.linalg.norm(c) + 1e-9)
    spk['centroid'] = [round(float(x), 6) for x in c]
    spk['n_anchors'] = len(spk['anchors'])
    spk['updated_at'] = time.strftime('%Y-%m-%d %H:%M')


def register_speaker(lib, name=None):
    spk = {'id': 'sp_' + uuid.uuid4().hex[:8], 'name': name or '',
           'created_at': time.strftime('%Y-%m-%d %H:%M'), 'updated_at': time.strftime('%Y-%m %H:%M'),
           'centroid': None, 'n_anchors': 0, 'anchors': []}
    lib['speakers'].append(spk)
    return spk


def match_speakers(sigs, lib, job_id):
    """簇签名 vs 库内质心。返回 speaker_map {cluster: {gid, sim, status}} 并就地更新库。
    ≥SIM_AUTO 自动匹配并加锚点;SIM_SUSPECT~SIM_AUTO 疑似(绑 gid 不动库,等确认);
    低于阈值新建说话人。同一库内说话人只被最高分簇命中,防同会两人并一人。"""
    speaker_map = {}
    if not sigs:
        return speaker_map
    cands = []
    for cluster, v in sigs.items():
        for spk in lib['speakers']:
            c = spk.get('centroid')
            if not c:
                continue
            sim = float(np.dot(v, np.asarray(c, dtype=np.float32)))
            if sim >= SIM_SUSPECT:
                cands.append((sim, cluster, spk))
    cands.sort(key=lambda x: -x[0])
    claimed_spk = set()
    for sim, cluster, spk in cands:
        if str(cluster) in speaker_map or spk['id'] in claimed_spk:
            continue
        if sim >= SIM_AUTO:
            speaker_map[str(cluster)] = {'gid': spk['id'], 'sim': round(sim, 3), 'status': 'auto'}
            spk_add_anchor(spk, job_id, cluster, sim, sigs[cluster])
        else:
            speaker_map[str(cluster)] = {'gid': spk['id'], 'sim': round(sim, 3), 'status': 'suspect'}
        claimed_spk.add(spk['id'])   # 疑似也独占,避免另一簇再绑同一人
    for cluster, v in sigs.items():
        if str(cluster) not in speaker_map:
            spk = register_speaker(lib)
            spk_add_anchor(spk, job_id, cluster, 1.0, v)
            speaker_map[str(cluster)] = {'gid': spk['id'], 'sim': None, 'status': 'new'}
    return speaker_map


def resolve_names(meta, lib=None):
    """簇号 → 显示名:本任务 speaker_names 覆盖 > 全局库 name(由调用方兜底「说话人N」)。"""
    local = {str(k): v for k, v in (meta.get('speaker_names') or {}).items() if str(v).strip()}
    smap = meta.get('speaker_map') or {}
    if not smap:
        return dict(local)
    lib = lib or load_speakers()
    gnames = {s['id']: (s.get('name') or '').strip() for s in lib['speakers']}
    out = dict(local)
    for k, v in smap.items():
        gid = (v or {}).get('gid')
        if k not in out and gid and gnames.get(gid):
            out[k] = gnames[gid]
    return out


def iter_done_jobs():
    if not os.path.isdir(JOBS_DIR):
        return
    for jid in os.listdir(JOBS_DIR):
        mp = os.path.join(job_dir(jid), 'meta.json')
        if not os.path.isfile(mp):
            continue
        try:
            with open(mp, encoding='utf-8') as f:
                yield jid, json.load(f)
        except (json.JSONDecodeError, OSError):
            continue


def regenerate_md_for_gid(gid):
    """全局改名/删除后,重生成所有引用该说话人的任务文稿。"""
    for jid, meta in list(iter_done_jobs()):
        smap = meta.get('speaker_map') or {}
        if any((v or {}).get('gid') == gid for v in smap.values()):
            try:
                write_md(jid)
            except Exception:  # noqa: BLE001 单个任务 md 失败不阻断
                pass


def assign_speakers(wsegs, dsegs):
    """wsegs: [{start_ms,end_ms,text}], dsegs: [(start_s,end_s,speaker)] → 就地填 speaker。"""
    dspan = [(s * 1000, e * 1000, sp) for s, e, sp in dsegs]
    last = 0
    for seg in wsegs:
        best, best_ov = last, 0.0
        for ds, de, sp in dspan:
            if ds >= seg['end_ms']:
                break
            ov = min(seg['end_ms'], de) - max(seg['start_ms'], ds)
            if ov > best_ov:
                best, best_ov = sp, ov
        seg['speaker'] = best
        last = best
    return wsegs


def read_wav_float(path):
    """afconvert 输出 WAVE_FORMAT_EXTENSIBLE(65534),标准库 wave 不支持,直接解析 RIFF。"""
    import struct
    with open(path, 'rb') as f:
        data = f.read()
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise RuntimeError('不是有效的 WAV 文件')
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
        if cid == b'data':
            n = size - (size % 2)
            return np.frombuffer(data[pos + 8:pos + 8 + n], dtype='<i2').astype(np.float32) / 32768.0
        pos += 8 + size + (size % 2)
    raise RuntimeError('WAV 中未找到 data 块')


def to_srt(segments, names):
    rows, i = [], 0
    for s in segments:
        i += 1
        a = fmt_srt_ts(s['start_ms'])
        b = fmt_srt_ts(s['end_ms'])
        nm = names.get(str(s['speaker'])) or '说话人%d' % (s['speaker'] + 1)
        rows.append(f"{i}\n{a} --> {b}\n{nm}:{s['text']}\n")
    return '\n'.join(rows)


def fmt_srt_ts(ms):
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def fmt_mmss(ms):
    m, s = divmod(ms // 1000, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def sp_name(seg, names):
    return names.get(str(seg['speaker']), '说话人%d' % (seg['speaker'] + 1))


def render_transcript(meta, segments, fmt):
    names = resolve_names(meta)
    if fmt == 'srt':
        return to_srt(segments, names), 'text/plain; charset=utf-8'
    if fmt == 'json':
        return json.dumps({'meta': job_public(meta), 'segments': segments},
                          ensure_ascii=False, indent=1), 'application/json'
    if fmt == 'txt':
        lines = [f"[{fmt_mmss(s['start_ms'])}] {sp_name(s, names)}: {s['text']}" for s in segments]
        return '\n'.join(lines), 'text/plain; charset=utf-8'
    lines = [f"# {meta['title']} 转录文稿", '',
             f"> 时长 {fmt_mmss(int((meta.get('duration_s') or 0) * 1000))} · "
             f"说话人 {meta.get('num_speakers_detected', '?')} 人 · 转录于 {meta['created_at'][:16]}", '']
    for s in segments:
        lines.append(f"**[{fmt_mmss(s['start_ms'])}] {sp_name(s, names)}**:{s['text']}")
        lines.append('')
    return '\n'.join(lines), 'text/markdown; charset=utf-8'


# ---------- 管线 worker(串行,whisper 占 GPU) ----------

def run_whisper(job_id, wav, language):
    prefix = os.path.join(job_dir(job_id), 'whisper')
    cmd = [WHISPER_BIN, '-m', WHISPER_MODEL, '-f', wav, '-oj', '-otxt', '-olrc', '-of', prefix, '-pp']
    if language != 'auto':
        cmd += ['-l', language]
    if language in PROMPTS:
        cmd += ['--prompt', PROMPTS[language]]
    GPU_LOCK.acquire()   # GPU 互斥:与 live pass2 窗口修正(live_pass2.Pass2Runner)串行
    try:
        proc = subprocess.Popen(cmd, cwd=WHISPER_DIR, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        write_meta(job_id, worker_pid=proc.pid)
        tail = deque(maxlen=30)
        for line in proc.stderr:
            tail.append(line)
            m = re.search(r'progress = (\d+)', line)
            if m:
                pct = int(m.group(1))
                write_meta(job_id, progress=5 + pct * 80 // 100)
        if proc.wait() != 0:
            raise RuntimeError('whisper 转录失败:' + ''.join(tail)[-1500:])
    finally:
        GPU_LOCK.release()
    with open(prefix + '.json', encoding='utf-8') as f:
        data = json.load(f)
    segs = [{'start_ms': t['offsets']['from'], 'end_ms': t['offsets']['to'], 'text': t['text'].strip()}
            for t in data.get('transcription', []) if t.get('text', '').strip()]
    duration = segs[-1]['end_ms'] / 1000 if segs else 0
    return duration, segs


def run_diarization(wav, num_speakers, progress_cb):
    import sherpa_onnx
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=SEG_MODEL)),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=EMB_MODEL, num_threads=4),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers and num_speakers > 0 else -1),
        min_duration_on=0.3, min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)
    samples = read_wav_float(wav)
    try:
        res = sd.process(samples, callback=progress_cb)
    except TypeError:
        res = sd.process(samples)
    return [(s.start, s.end, s.speaker) for s in res.sort_by_start_time()]


def process_job(job_id):
    try:
        if read_meta(job_id).get('cancel_requested'):
            raise RuntimeError('已放弃')
        write_meta(job_id, status='converting', stage='转码音频', progress=2)
        meta = read_meta(job_id)
        src = os.path.join(job_dir(job_id), meta['source_file'])
        wav = os.path.join(job_dir(job_id), 'audio16k.wav')
        r = subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', src, wav],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError('音频转码失败(afconvert):' + r.stderr.strip()[:300])

        if read_meta(job_id).get('cancel_requested'):
            raise RuntimeError('已放弃')
        write_meta(job_id, status='transcribing', stage='语音转录(whisper)', progress=5)
        duration, wsegs = run_whisper(job_id, wav, meta['language'])
        if not wsegs:
            raise RuntimeError('没有识别到任何语音内容')

        nspeakers = meta.get('num_speakers', 0)
        if nspeakers == 1:
            for s in wsegs:
                s['speaker'] = 0
            detected = 1
            dsegs = single_speaker_dsegs(wsegs)
        else:
            if read_meta(job_id).get('cancel_requested'):
                raise RuntimeError('已放弃')
            write_meta(job_id, status='diarizing', stage='说话人识别', progress=86)

            def on_diar_progress(done, total):
                write_meta(job_id, progress=86 + int(done * 12 / max(total, 1)))
                return True

            dsegs = run_diarization(wav, nspeakers, on_diar_progress)
            wsegs = assign_speakers(wsegs, dsegs)
            detected = len({s for _, _, s in dsegs})

        if read_meta(job_id).get('cancel_requested'):
            raise RuntimeError('已放弃')
        # diar 结果与声纹签名落盘:换库重放、事后重匹配的原料,避免重跑 12 分钟 CPU
        with open(os.path.join(job_dir(job_id), 'dsegs.json'), 'w', encoding='utf-8') as f:
            json.dump([[s, e, sp] for s, e, sp in dsegs], f, ensure_ascii=False)

        speaker_map = {}
        if meta.get('voiceprint', True):
            sigs = extract_cluster_embeddings(wav, dsegs)
            if sigs:
                with open(os.path.join(job_dir(job_id), 'embeddings.json'), 'w', encoding='utf-8') as f:
                    json.dump({str(k): [round(float(x), 6) for x in v] for k, v in sigs.items()},
                              f, ensure_ascii=False)
                lib = load_speakers()
                speaker_map = match_speakers(sigs, lib, job_id)
                save_speakers(lib)

        write_meta(job_id, status='merging', stage='生成文稿', progress=99,
                   speaker_map=speaker_map)
        with open(os.path.join(job_dir(job_id), 'transcript.json'), 'w', encoding='utf-8') as f:
            json.dump({'segments': wsegs}, f, ensure_ascii=False)
        write_meta(job_id, status='done', stage='完成', progress=100,
                   duration_s=round(duration, 1), num_speakers_detected=detected)
        write_md(job_id)
    except Exception as e:  # noqa: BLE001 边界捕获,写回状态机
        try:
            cancelled = False
            try:
                cancelled = bool(read_meta(job_id).get('cancel_requested'))
            except Exception:  # noqa: BLE001
                pass
            write_meta(job_id, status='error', stage='失败',
                       error='已放弃' if cancelled else str(e)[:500])
        except Exception:  # noqa: BLE001
            pass


def worker():
    while True:
        job_id = job_queue.get()
        process_job(job_id)


# ---------- HTTP 边界(校验只在这里) ----------

@app.before_request
def local_only():
    host = request.host.split(':')[0]
    if host not in ('127.0.0.1', 'localhost'):
        return jsonify({'error': 'forbidden'}), 403


@app.get('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')


@app.get('/recorder.js')
def recorder_js():
    return send_from_directory(STATIC_DIR, 'recorder.js')


@app.post('/api/jobs')
def create_job():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': '缺少音频文件'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({'error': f'不支持的格式 {ext},支持 {" ".join(sorted(ALLOWED_EXT))}'}), 400
    try:
        nspeakers = int(request.form.get('speakers', '0'))
        assert -1 <= nspeakers <= 10
    except (ValueError, AssertionError):
        return jsonify({'error': '说话人数参数无效'}), 400
    language = request.form.get('language', 'zh')
    if language not in ('zh', 'en', 'ja', 'auto'):
        return jsonify({'error': '语言参数无效'}), 400
    voiceprint = request.form.get('voiceprint', '1') != '0'

    job_id = time.strftime('%Y%m%d-%H%M%S') + '_' + uuid.uuid4().hex[:4]
    os.makedirs(job_dir(job_id), exist_ok=True)
    src_name = 'source' + ext
    f.save(os.path.join(job_dir(job_id), src_name))
    title = (request.form.get('title') or os.path.splitext(f.filename)[0])[:80]
    meta = {'id': job_id, 'title': title, 'created_at': time.strftime('%Y-%m-%d %H:%M'),
            'language': language, 'num_speakers': nspeakers, 'source_file': src_name,
            'status': 'queued', 'stage': '排队中', 'progress': 0, 'error': None,
            'duration_s': None, 'num_speakers_detected': None, 'speaker_names': {},
            'voiceprint': voiceprint, 'speaker_map': {}}
    with open(os.path.join(job_dir(job_id), 'meta.json'), 'w', encoding='utf-8') as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=1)
    job_queue.put(job_id)
    return jsonify(job_public(meta)), 201


@app.get('/api/jobs')
def list_jobs():
    jobs = []
    if os.path.isdir(JOBS_DIR):
        for jid in os.listdir(JOBS_DIR):
            try:
                if os.path.isfile(os.path.join(job_dir(jid), 'meta.json')):
                    jobs.append(job_public(read_meta(jid)))
            except (json.JSONDecodeError, OSError):
                continue
    jobs.sort(key=lambda m: m['created_at'], reverse=True)
    # 排队位次:单 worker 串行,创建早者优先;正在处理的一个占第 1 位
    running = sum(1 for m in jobs if m['running'])
    pos = running
    for m in reversed(jobs):
        if m['status'] == 'queued':
            pos += 1
            m['queue_pos'] = pos
    return jsonify(jobs)


@app.get('/api/jobs/<job_id>')
def get_job(job_id):
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(os.path.join(job_dir(job_id), 'meta.json')):
        abort(404)
    meta = read_meta(job_id)
    resp = {'job': job_public(meta)}
    tpath = os.path.join(job_dir(job_id), 'transcript.json')
    if meta['status'] == 'done' and os.path.isfile(tpath):
        with open(tpath, encoding='utf-8') as f:
            resp['segments'] = json.load(f)['segments']
    return jsonify(resp)


@app.patch('/api/jobs/<job_id>')
def patch_job(job_id):
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(os.path.join(job_dir(job_id), 'meta.json')):
        abort(404)
    meta = read_meta(job_id)
    if meta['status'] != 'done':
        return jsonify({'error': '任务完成后才能编辑'}), 409
    body = request.get_json(force=True, silent=True) or {}
    fields = {}
    names = body.get('speaker_names')
    if isinstance(names, dict):
        clean = {str(k)[:2]: str(v)[:30] for k, v in names.items() if re.fullmatch(r'\d+', str(k))}
        fields['speaker_names'] = {**meta.get('speaker_names', {}), **clean}
    if isinstance(body.get('title'), str) and body['title'].strip():
        fields['title'] = body['title'].strip()[:80]
    if not fields:
        return jsonify({'error': '没有可更新字段'}), 400
    meta = write_meta(job_id, **fields)
    try:
        write_md(job_id)  # 改名后让落盘的 md 同步
    except Exception:  # noqa: BLE001 md 写失败不影响改名本身
        pass
    return jsonify(job_public(meta))


@app.delete('/api/jobs/<job_id>')
def delete_job(job_id):
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isdir(job_dir(job_id)):
        abort(404)
    meta = read_meta(job_id)
    if meta['status'] not in TERMINAL:
        return jsonify({'error': '任务进行中,暂不能删除'}), 409
    subprocess.run(['rm', '-rf', job_dir(job_id)], check=True)
    return jsonify({'ok': True})


@app.get('/api/jobs/<job_id>/audio')
def job_audio(job_id):
    wav = os.path.join(job_dir(job_id), 'audio16k.wav')
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(wav):
        abort(404)
    return send_file(wav, mimetype='audio/wav', conditional=True)


@app.get('/api/jobs/<job_id>/export/<fmt>')
def job_export(job_id, fmt):
    if fmt not in ('md', 'txt', 'srt', 'json'):
        abort(404)
    meta_path = os.path.join(job_dir(job_id), 'meta.json')
    tpath = os.path.join(job_dir(job_id), 'transcript.json')
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(meta_path):
        abort(404)
    meta = read_meta(job_id)
    if meta['status'] != 'done' or not os.path.isfile(tpath):
        return jsonify({'error': '任务尚未完成'}), 409
    with open(tpath, encoding='utf-8') as f:
        segments = json.load(f)['segments']
    content, mime = render_transcript(meta, segments, fmt)
    from flask import Response
    return Response(content, mimetype=mime,
                    headers={'Content-Disposition': f"attachment; filename={job_id}.{fmt}"})


@app.post('/api/jobs/<job_id>/reveal')
def job_reveal(job_id):
    """在 Finder 中显示任务目录,完成任务优先定位到 transcript.md(缺失则补写)。"""
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isdir(job_dir(job_id)):
        abort(404)
    md = write_md(job_id)
    if md:
        subprocess.run(['open', '-R', md], check=False)
        return jsonify({'ok': True, 'revealed': md})
    subprocess.run(['open', job_dir(job_id)], check=False)
    return jsonify({'ok': True, 'revealed': job_dir(job_id)})


@app.post('/api/jobs/<job_id>/cancel')
def job_cancel(job_id):
    """放弃进行中的任务:杀掉 whisper 子进程立即生效;diarizing 阶段在下一个阶段边界放弃。"""
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(os.path.join(job_dir(job_id), 'meta.json')):
        abort(404)
    meta = read_meta(job_id)
    if meta['status'] in TERMINAL:
        return jsonify({'error': '任务已结束,无需放弃'}), 409
    pid = meta.get('worker_pid')
    if pid:
        subprocess.run(['kill', str(pid)], capture_output=True)
    write_meta(job_id, cancel_requested=True)
    return jsonify({'ok': True})


@app.post('/api/jobs/<job_id>/retry')
def job_retry(job_id):
    """用任务内已有的原始音频重跑(失败/放弃的任务可挽回,录音任务不丢音频)。"""
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(os.path.join(job_dir(job_id), 'meta.json')):
        abort(404)
    meta = read_meta(job_id)
    if meta['status'] not in TERMINAL:
        return jsonify({'error': '任务进行中,不能重跑'}), 409
    if not os.path.isfile(os.path.join(job_dir(job_id), meta['source_file'])):
        return jsonify({'error': '原始音频文件缺失,无法重跑'}), 400
    write_meta(job_id, status='queued', stage='排队中', progress=0, error=None, cancel_requested=False)
    job_queue.put(job_id)
    return jsonify({'ok': True})


def load_job_embeddings(job_id):
    p = os.path.join(job_dir(job_id), 'embeddings.json')
    if not os.path.isfile(p):
        return {}
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def spk_remove_anchor(spk, job_id, cluster):
    """拆分说话人时移除该任务对该说话人的贡献并重算质心。"""
    before = len(spk.get('anchors', []))
    spk['anchors'] = [a for a in spk['anchors']
                      if not (a.get('job_id') == job_id and a.get('cluster') == int(cluster))]
    if len(spk['anchors']) == before:
        return
    if spk['anchors']:
        vecs = [np.asarray(a['vec'], dtype=np.float32) for a in spk['anchors']]
        c = np.mean(vecs, axis=0)
        spk['centroid'] = [round(float(x), 6) for x in c / (np.linalg.norm(c) + 1e-9)]
        spk['n_anchors'] = len(spk['anchors'])
    else:
        spk['centroid'] = None
        spk['n_anchors'] = 0
    spk['updated_at'] = time.strftime('%Y-%m-%d %H:%M')


@app.post('/api/jobs/<job_id>/speaker_map')
def job_speaker_action(job_id):
    """声纹匹配的人工裁决:confirm=确认为同一人(疑似升为已确认并补锚点);
    separate=另存为新说话人(脱离原 gid,并把本任务锚点从原说话人移除)。"""
    if not re.fullmatch(r'[\w-]+', job_id) or not os.path.isfile(os.path.join(job_dir(job_id), 'meta.json')):
        abort(404)
    meta = read_meta(job_id)
    if meta['status'] != 'done':
        return jsonify({'error': '任务完成后才能操作'}), 409
    body = request.get_json(force=True, silent=True) or {}
    sp = str(body.get('speaker', ''))
    action = body.get('action')
    smap = dict(meta.get('speaker_map') or {})
    entry = smap.get(sp)
    if not re.fullmatch(r'\d+', sp) or not entry:
        return jsonify({'error': '该说话人没有声纹匹配记录'}), 404
    if action not in ('confirm', 'separate'):
        return jsonify({'error': '未知操作'}), 400

    embs = load_job_embeddings(job_id)
    vec = np.asarray(embs.get(sp, []), dtype=np.float32) if embs.get(sp) is not None else None
    lib = load_speakers()
    old = next((s for s in lib['speakers'] if s['id'] == entry.get('gid')), None)
    if action == 'confirm':
        smap[sp] = {**entry, 'status': 'confirmed'}
        if old is not None and vec is not None and len(vec) == lib.get('dim', EMB_DIM):
            spk_add_anchor(old, job_id, int(sp), entry.get('sim') or 0.0, vec)
    else:
        if old is not None:
            spk_remove_anchor(old, job_id, int(sp))
        new_spk = register_speaker(lib, (body.get('name') or '').strip()[:30] or None)
        if vec is not None and len(vec) == lib.get('dim', EMB_DIM):
            spk_add_anchor(new_spk, job_id, int(sp), 1.0, vec)
        smap[sp] = {'gid': new_spk['id'], 'sim': None, 'status': 'new'}
    save_speakers(lib)
    write_meta(job_id, speaker_map=smap)
    try:
        write_md(job_id)
    except Exception:  # noqa: BLE001
        pass
    return jsonify(job_public(read_meta(job_id)))


@app.get('/api/speakers')
def speakers_list():
    """说话人库 + 待确认的疑似匹配清单(扫全部完成任务)。"""
    lib = load_speakers()
    out = []
    for s in lib['speakers']:
        jobs_used = sorted({a.get('job_id') for a in s.get('anchors', []) if a.get('job_id')})
        out.append({'id': s['id'], 'name': s.get('name') or '',
                    'n_anchors': s.get('n_anchors', len(s.get('anchors', []))),
                    'last_used': s.get('updated_at') or s.get('created_at') or '',
                    'jobs': jobs_used})
    suspects = []
    for jid, meta in iter_done_jobs():
        smap = meta.get('speaker_map') or {}
        if not smap:
            continue
        gnames = {s['id']: (s.get('name') or '') for s in lib['speakers']}
        names = resolve_names(meta, lib)
        for k, v in smap.items():
            if (v or {}).get('status') == 'suspect':
                gid = v.get('gid')
                match_name = gnames.get(gid) or '未命名说话人'
                if not match_name:
                    sp = next((s for s in lib['speakers'] if s['id'] == gid), None)
                    match_name = ('说话人库 #' + gid[-4:]) if sp else match_name
                suspects.append({'job_id': jid, 'job_title': meta.get('title') or jid,
                                 'speaker': int(k), 'name': names.get(k) or ('说话人' + str(int(k) + 1)),
                                 'gid': gid, 'match_name': match_name, 'sim': v.get('sim')})
    return jsonify({'speakers': out, 'suspects': suspects})


def _find_speaker(lib, gid):
    return next((s for s in lib['speakers'] if s['id'] == gid), None)


@app.patch('/api/speakers/<gid>')
def speaker_rename(gid):
    lib = load_speakers()
    spk = _find_speaker(lib, gid)
    if not spk:
        abort(404)
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body.get('name'), str):
        return jsonify({'error': '缺少 name 字段'}), 400
    spk['name'] = body['name'].strip()[:30]
    spk['updated_at'] = time.strftime('%Y-%m-%d %H:%M')
    save_speakers(lib)
    regenerate_md_for_gid(gid)   # 全局改名,重生成所有引用该说话人的文稿
    return jsonify({'ok': True, 'id': gid, 'name': spk['name']})


@app.delete('/api/speakers/<gid>')
def speaker_delete(gid):
    """只删库内说话人;任务文稿回落到任务级名字或「说话人N」,不删任务。"""
    lib = load_speakers()
    spk = _find_speaker(lib, gid)
    if not spk:
        abort(404)
    lib['speakers'] = [s for s in lib['speakers'] if s['id'] != gid]
    save_speakers(lib)
    for jid, meta in list(iter_done_jobs()):
        smap = meta.get('speaker_map') or {}
        if any((v or {}).get('gid') == gid for v in smap.values()):
            smap = {k: ({'gid': None, 'sim': v.get('sim'), 'status': 'new'} if (v or {}).get('gid') == gid else v)
                    for k, v in smap.items()}
            write_meta(jid, speaker_map=smap)
            try:
                write_md(jid)
            except Exception:  # noqa: BLE001
                pass
    return jsonify({'ok': True})


def recover_interrupted():
    """重启后把非终态任务标记为中断,并清理孤儿 whisper 进程,避免永远卡在进行中。"""
    if not os.path.isdir(JOBS_DIR):
        return
    for jid in os.listdir(JOBS_DIR):
        mp = os.path.join(job_dir(jid), 'meta.json')
        if not os.path.isfile(mp):
            continue
        try:
            with open(mp, encoding='utf-8') as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get('status') in TERMINAL:
            continue
        pid = meta.get('worker_pid')
        if pid:
            r = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='], capture_output=True, text=True)
            if 'whisper' in r.stdout:
                subprocess.run(['kill', str(pid)])
        meta.update(status='error', stage='失败', error='服务重启导致中断,请删除后重新上传')
        tmp = mp + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, mp)


# ---------- live 实时字幕 WS 域(Task 4 追加;业务逻辑在 live_session.py) ----------

import live_session as live_domain
from flask_sock import Sock

sock = Sock(app)
LIVE_ROOT = os.path.join(BASE, 'live')
LIVE_MODEL_DIR = os.path.join(MODEL_DIR, 'streaming-zipformer-zh-en')       # 中英双语流式
LIVE_JA_MODEL_DIR = os.path.join(MODEL_DIR, 'streaming-zipformer-ja')       # 多语言流式(含日文,spec 2026-10-24)


def live_recover(exclude=()):
    """扫描 live/ 未终态会话目录 → 自动转正建 job(真身 live_finalize.recover_all)。

    启动期(main())不排除;运行期 /api/live/recover_now 传 live_session 的活跃
    会话 id 集合——正在录制的会话目录正在被写入,绝不能拿去转正。返回 [(会话, job)]。"""
    import live_finalize
    done = live_finalize.recover_all(LIVE_ROOT, exclude=frozenset(exclude))
    if done:
        print('live 恢复:%d 个未完成会话已转正:%s'
              % (len(done), ', '.join('%s→%s' % (s, j) for s, j in done)))
    return done


@app.post('/api/live/recover_now')
def live_recover_now():
    """运行期安全版 live_recover(spec WP6-4):转正失败寻回不要求重启服务;
    幂等(已终态目录沿用既有跳过/清理逻辑);活跃会话目录排除。"""
    done = live_recover(exclude=live_domain.active_session_ids())
    return jsonify({'recovered': [jid for _sid, jid in done]})


@app.get('/live')
def live_page():
    return send_from_directory(STATIC_DIR, 'live.html')


@app.get('/api/live/state')
def live_state_api():
    """侧栏动作行红点查询(spec WP2-3):有无进行中会话与开始时间;真身在 live_session。"""
    return jsonify(live_domain.live_state())


@app.get('/live.js')
def live_js():
    return send_from_directory(STATIC_DIR, 'live.js')


def live_start_brief(e):
    """live start 失败的下行文案:打码规则真身在 live_session.error_brief
    (start 失败与 finalize_failed 的 stopped.error 同源,勿在此另写正则)。"""
    return live_domain.error_brief(e)


@sock.route('/ws/live')
def ws_live(ws):
    """纯转发层:二进制帧→feed_bytes;start/stop/resume→LiveSession;事件队列→下行。

    每条连接生成 conn_id 并在 start/resume 成功时注册进会话连接注册表;resume 到达
    时已有其他连接附着则接管(spec WP3):旧连接先收 session_taken_over 终态通知再被
    置 gone 并关闭(旧 pump 经 gone 有界退出,EventTap 归还语义不变),新连接重放
    backlog 并挂 pump。接管收尾严格在锁外:锁内只从注册表取旧 ws 引用与 gone。"""
    session = None
    sender = None
    gone = threading.Event()
    conn_id = uuid.uuid4().hex

    def pump(last_seq):
        """转发会话事件队列(主循环在 live_session.pump_events,便于单测):stopped
        投出 / gone 置位 / 连续 send 失败(对端断开,事件已按序归还 tap)任一即退出
        ——转正异步期间客户端断开也不会活锁。"""
        def send(ev):
            ws.send(json.dumps(ev, ensure_ascii=False))
        return live_domain.pump_events(session.tap, send, gone, last_seq)

    def take_over(displaced):
        """锁外收尾被接管的旧连接:先发终态通知,再置 gone(旧 pump 随之退出)并关闭。
        send 失败只说明旧连接已断,照常置 gone;顺序即 spec:「先通知再断」。"""
        for c in displaced:
            try:
                c['ws'].send(json.dumps({'type': 'session_taken_over'}))
            except Exception:  # noqa: BLE001 旧连接已断:通知发不出,照样收尾
                pass
            c['gone'].set()
            try:
                c['ws'].close()
            except Exception:  # noqa: BLE001 关闭失败无碍:gone 已置位
                pass

    try:
        while True:
            data = ws.receive()
            if isinstance(data, (bytes, bytearray)):
                if session is not None:
                    session.feed_bytes(bytes(data))
                continue
            try:
                msg = json.loads(data)
            except (TypeError, ValueError):
                continue
            mtype = msg.get('type')
            if mtype == 'start' and session is None:
                # 构造(加载 300MB 流式模型)或 start() 抛错不得落进外层静默 pass:
                # 连接无消息即关,前端只会无限重连。显式回 error 后收尾断开(不发
                # started、不挂 pump;session 置回 None,半途对象不再接收字节)。
                # 前端收到该 error 即停止自动重连并显示原因,是否重试交给人。
                try:
                    lang_spec = str(msg.get('language') or 'zh>en')
                    src = lang_spec.split('>', 1)[0].strip()
                    # pass1 流式模型按识别语言选目录:ja → 多语言模型,其余 → 中英双语
                    model_dir = LIVE_JA_MODEL_DIR if src == 'ja' else LIVE_MODEL_DIR
                    session = live_domain.LiveSession(LIVE_ROOT, model_dir)
                    sid = session.start(str(msg.get('title') or ''), msg.get('speakers') or 0,
                                        lang_spec)
                except Exception as e:  # noqa: BLE001 任何 start 阶段失败都显式告知
                    session = None
                    ws.send(json.dumps({'type': 'error',
                                        'message': '无法开始实时会话:%s' % live_start_brief(e)},
                                       ensure_ascii=False))
                    return
                ws.send(json.dumps({'type': 'started', 'session_id': sid,
                                    'llm': bool(getattr(session, 'llm_enabled', False))},
                                   ensure_ascii=False))
                session.attach_conn(conn_id, ws, gone)   # 新会话无旧连接,不会顶掉谁
                sender = threading.Thread(target=pump, args=(0,), daemon=True)
                sender.start()
            elif mtype == 'stop' and session is not None and session.status == 'listening':
                session.handle_stop()   # 异步:返回即 stop_ack 入队;转正(尾窗+建 job)在后台
                if sender is not None:
                    sender.join()       # pump 发完 stopped(或对端断开)后自然退出
                return
            elif mtype == 'resume' and session is None:
                session = live_domain.find_session(str(msg.get('session_id') or ''))
                if session is None:
                    ws.send(json.dumps({'type': 'error', 'message': '会话不存在或已失效'},
                                       ensure_ascii=False))
                    return
                try:
                    last_seq = int(msg.get('last_seq') or 0)
                except (TypeError, ValueError):
                    last_seq = 0
                # 接管:注册本连接并顶掉旧附着连接(锁内取引用),锁外通知旧连接后
                # 再做本连接的 backlog 重放与挂 pump(顺序即 spec)
                take_over(session.attach_conn(conn_id, ws, gone))
                replay = session.backlog_after(last_seq)
                for ev in replay:
                    ws.send(json.dumps(ev, ensure_ascii=False))
                sender = threading.Thread(
                    target=pump, args=(replay[-1]['seq'] if replay else last_seq,), daemon=True)
                sender.start()
    except Exception:  # noqa: BLE001 客户端断开(ConnectionClosed)等,正常收尾
        pass
    finally:
        gone.set()
        if session is not None:
            session.detach_conn(conn_id)


def main():
    """服务入口(仅 __main__ 委托调用):worker 线程 + Flask 监听。

    收口成函数并让脚本态走 `import app` 是关键:`python3 app.py` 直跑时模块名是
    __main__,而 live_pass2/live_finalize 的 `import app` 会把 app.py 再执行一遍
    成第二个实例——两份 job_queue(live 转正的 job 进孤儿队列,worker 永远不取,
    2026-10-23 replay 对拍实测卡 queued 15 分钟)与两份 GPU_LOCK(离线 whisper 与
    live pass2 的互斥失效,Metal 抢占)。单实例化后两处身份恒等。"""
    os.makedirs(JOBS_DIR, exist_ok=True)
    recover_interrupted()
    live_recover()   # live 断电恢复:非终态会话目录自动转正建 job(Task 9)
    threading.Thread(target=worker, daemon=True).start()
    app.run(host='127.0.0.1', port=8765, threaded=True)


if __name__ == '__main__':
    import sys as _sys
    if __package__ in (None, ''):   # 直跑:确保能 import 到自身模块实例
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app as _real
    _real.main()
