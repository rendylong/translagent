# -*- coding: utf-8 -*-
"""终稿对拍(Task 9 Step 5):同一音频 ①直接上传出 job ②走 live 回放+stop 转正出 job,
两 transcript.json 的 segments 文本一致(允许时间戳 ±200ms)。

预期:一致。live 的转正是硬链接 live_audio.wav 为 jobs/<new>/source.wav,afconvert
对已是 16k 单声道 PCM 的 wav 是恒等转换,两 job 走完全相同的离线管线
(whisper 参数逐项一致),故 segments 应逐段相同(whisper 对同一输入确定性)。
live 期间的 partial/pass1_final/llm 事件只影响实时字幕,不进终稿。

音频取 zh 锚点(jobs/20261022-130447_9bef)前 SEC 秒。真实跑两个 job:
whisper+diarization 各约 1-3 分钟。运行:cd transcriber && python3 tests/replay_e2e.py
端口被占则复用现有服务(不代杀);结束删两个对拍 job,不留工作台垃圾。
"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import websocket

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import app  # noqa: E402
from live_wav import write_slice  # noqa: E402

PORT = 8765
CHUNK = 3200                       # 200ms @16k mono s16le(与 live.js 一致)
SEC = 150                          # 对拍时长:两层管线下足够出多条 pass1 final,两个 job 各 ~2min
SPEAKERS = 2
TOL_MS = 200


def port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


def wait_port(proc, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError('app.py 子进程提前退出(code=%s)' % proc.returncode)
        if port_busy():
            return
        time.sleep(0.3)
    raise RuntimeError('服务 %.0fs 内未就绪' % timeout)


def api(path, method='GET', data=None, files=None, form=None):
    url = 'http://127.0.0.1:%d%s' % (PORT, path)
    if files is not None:
        boundary = 'e2eboundary42'
        body = bytearray()
        for k, v in (form or {}).items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                     % (boundary, k, v)).encode()
        fname, blob = files
        body += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                 'Content-Type: audio/wav\r\n\r\n' % (boundary, fname)).encode()
        body += blob
        body += ('\r\n--%s--\r\n' % boundary).encode()
        req = urllib.request.Request(url, data=bytes(body), method='POST',
                                     headers={'Content-Type': 'multipart/form-data; boundary=%s' % boundary})
    else:
        req = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def make_wav(tmp):
    """zh 锚点前 SEC 秒写成标准 16k wav(上传与回放共用同一字节)。"""
    anchors = json.load(open(BASE / 'models' / 'bench' / 'anchors.json'))
    x = app.read_wav_float(anchors['zh']['wav'])[:SEC * 16000]
    assert len(x) == SEC * 16000
    path = str(Path(tmp) / 'e2e_src.wav')
    write_slice(path, x)
    return path


def wait_job(job_id, timeout_s=900):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        j = api('/api/jobs/%s' % job_id)['job']
        if j['status'] in ('done', 'error'):
            return j
        time.sleep(3)
    raise RuntimeError('job %s 超时未完成' % job_id)


def upload_job(wav_path):
    blob = Path(wav_path).read_bytes()
    out = api('/api/jobs', files=('e2e_src.wav', blob),
              form={'speakers': str(SPEAKERS), 'language': 'zh', 'title': '对拍-直传'})
    return out['id']


def live_job(pcm):
    ws = websocket.create_connection('ws://127.0.0.1:%d/ws/live' % PORT, timeout=60)
    ws.send(json.dumps({'type': 'start', 'title': '对拍-live', 'speakers': SPEAKERS,
                        'language': 'zh>en'}))
    started = json.loads(ws.recv())
    assert started.get('type') == 'started', started
    sid = started['session_id']
    for i in range(0, len(pcm), CHUNK):
        ws.send_binary(pcm[i:i + CHUNK])
        if i % (CHUNK * 500) == 0:          # 轻微让步,别把写线程队列顶爆
            time.sleep(0.02)
    ws.send(json.dumps({'type': 'stop'}))
    ws.settimeout(600)                      # 转正异步:尾窗 whisper + LLM 排空 + 建 job
    stopped = None
    live_events = []
    while True:
        ev = json.loads(ws.recv())
        live_events.append(ev.get('type'))
        if ev.get('type') == 'stopped':
            stopped = ev
            break
        if ev.get('type') == 'error':
            print('live error 事件:', ev)
    ws.close()
    assert stopped and stopped.get('job_id'), 'stopped 应带 job_id'
    print('live 事件统计:%s' % {t: live_events.count(t) for t in sorted(set(live_events))})
    return stopped['job_id'], sid


def segments_of(job_id):
    out = api('/api/jobs/%s' % job_id)
    assert 'segments' in out, 'job %s 无 segments:%s' % (job_id, out.get('job', {}).get('error'))
    return out['segments']


def compare(a, b):
    errs = []
    if len(a) != len(b):
        errs.append('段数不同:直传 %d vs live %d' % (len(a), len(b)))
    for i, (sa, sb) in enumerate(zip(a, b)):
        if sa['text'] != sb['text']:
            errs.append('第 %d 段文本不同:\n  直传:%r\n  live :%r' % (i, sa['text'], sb['text']))
        if abs(sa['start_ms'] - sb['start_ms']) > TOL_MS or abs(sa['end_ms'] - sb['end_ms']) > TOL_MS:
            errs.append('第 %d 段时间戳超 ±%dms:直传 %d-%d vs live %d-%d'
                        % (i, TOL_MS, sa['start_ms'], sa['end_ms'], sb['start_ms'], sb['end_ms']))
        if sa.get('speaker') != sb.get('speaker'):
            errs.append('第 %d 段说话人不同:%s vs %s' % (i, sa.get('speaker'), sb.get('speaker')))
    return errs[:10], len(errs)


def delete_job(job_id):
    api('/api/jobs/%s' % job_id, method='DELETE')


def main():
    proc = None
    tmp = None
    if not port_busy():
        proc = subprocess.Popen([sys.executable, 'app.py'], cwd=str(BASE),
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_port(proc)
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        wav_path = make_wav(tmp.name)
        pcm = Path(wav_path).read_bytes()[44:]      # 上传 wav 与回放喂的 PCM 同源同字节
        t0 = time.time()
        job_a = upload_job(wav_path)
        print('① 直传 job=%s' % job_a)
        ja = wait_job(job_a)
        assert ja['status'] == 'done', '直传 job 失败:%s' % ja.get('error')
        print('   done (%.0fs), %d 段' % (time.time() - t0, len(segments_of(job_a))))

        t1 = time.time()
        job_b, sid = live_job(pcm)
        print('② live 转正 job=%s (session=%s)' % (job_b, sid))
        jb = wait_job(job_b)
        assert jb['status'] == 'done', 'live job 失败:%s' % jb.get('error')
        print('   done (%.0fs), %d 段' % (time.time() - t1, len(segments_of(job_b))))

        errs, nerr = compare(segments_of(job_a), segments_of(job_b))
        for e in errs:
            print('DIFF', e)
        print('对拍结果:%s(%d 处差异,时长 %ds,段数 %d)'
              % ('一致' if nerr == 0 else '不一致', nerr, SEC, len(segments_of(job_a))))
        delete_job(job_a)
        delete_job(job_b)
        print('两个对拍 job 已删除')
        sys.exit(0 if nerr == 0 else 1)
    finally:
        if tmp is not None:
            tmp.cleanup()
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == '__main__':
    main()
