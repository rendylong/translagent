# -*- coding: utf-8 -*-
"""端到端冒烟:临时起 app.py 子进程,走 /ws/live 全流程。

流程:start → 喂 zh 锚点前 60s 音频(200ms/包)→ 断言 30s 内 ≥5 个 partial
→ 发 stop → 收 stop_ack/stopped(Task 9 起 stopped 带 job_id,转正异步、
需等尾窗 whisper + LLM 排空)→ 等 job 跑完并删除(不污染工作台)→ 杀子进程。
端口被占则直接失败(不代杀)。
运行:cd transcriber && python3 tests/ws_smoke.py,期望输出 ok。
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import urllib.request
import websocket

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import app  # noqa: E402  只为复用 read_wav_float

PORT = 8765
CHUNK = 3200          # 200ms @16k 单声道 16bit
FEED_SEC = 60


def port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


def wait_port(proc, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('app.py 子进程提前退出(code=%s)' % proc.returncode)
        if port_busy():
            return
        time.sleep(0.3)
    raise RuntimeError('服务 %.0fs 内未就绪' % timeout)


def load_pcm_bytes():
    with open(BASE / 'models' / 'bench' / 'anchors.json') as fp:
        anchors = json.load(fp)
    samples = app.read_wav_float(anchors['zh']['wav'])[:FEED_SEC * 16000]
    assert len(samples) == FEED_SEC * 16000, '锚点音频不足 60s'
    q = np.clip(samples * 32768.0, -32768.0, 32767.0)
    return q.astype('<i2').tobytes()


def cleanup_job(job_id, timeout_s=420):
    """冒烟转正产生的 job:等子进程服务跑完再删,不留垃圾在工作台。"""
    base = 'http://127.0.0.1:%d/api/jobs/%s' % (PORT, job_id)
    deadline = time.time() + timeout_s
    status = ''
    while time.time() < deadline:
        with urllib.request.urlopen(base, timeout=10) as r:
            status = json.load(r)['job']['status']
        if status in ('done', 'error'):
            break
        time.sleep(2)
    req = urllib.request.Request(base, method='DELETE')
    urllib.request.urlopen(req, timeout=10).read()
    print('job %s %s,已删除' % (job_id, status))


def main():
    if port_busy():
        raise RuntimeError('端口 %d 已被占用,先处理既有服务再跑冒烟' % PORT)
    proc = subprocess.Popen([sys.executable, 'app.py'], cwd=str(BASE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        wait_port(proc)
        pcm = load_pcm_bytes()
        ws = websocket.create_connection('ws://127.0.0.1:%d/ws/live' % PORT, timeout=60)
        ws.send(json.dumps({'type': 'start', 'title': 'ws冒烟', 'speakers': 2}))
        started = json.loads(ws.recv())
        assert started.get('type') == 'started' and started.get('session_id'), started
        print('started:', started['session_id'])
        for i in range(0, len(pcm), CHUNK):
            ws.send_binary(pcm[i:i + CHUNK])
        print('fed %d bytes (%ds)' % (len(pcm), FEED_SEC))
        partials, stop_ack, stopped = 0, None, None
        ws.settimeout(30)
        deadline = time.time() + 30
        while time.time() < deadline and (partials < 5 or not stop_ack):
            ev = json.loads(ws.recv())
            t = ev.get('type')
            if t == 'partial':
                partials += 1
            elif t == 'stop_ack':
                stop_ack = ev
            elif t == 'stopped':
                stopped = ev
            if partials == 5 and not stop_ack:
                ws.send(json.dumps({'type': 'stop'}))
        assert partials >= 5, '30s 内只收到 %d 个 partial' % partials
        assert stop_ack, '未收到 stop_ack'
        if not stopped:
            try:
                ws.settimeout(180)          # 转正异步:尾窗 whisper + LLM 排空 + 建 job
                while True:
                    ev = json.loads(ws.recv())
                    if ev.get('type') == 'stopped':
                        stopped = ev
                        break
            except Exception as e:
                print('等 stopped 异常:%r' % e)
        print('partials=%d stop_ack=seq%s stopped=%s' %
              (partials, stop_ack.get('seq'), 'yes' if stopped else 'no'))
        assert partials >= 5 and stop_ack
        assert stopped and stopped.get('job_id'), 'stopped 应带 job_id(Task 9 转正)'
        ws.close()
        cleanup_job(stopped['job_id'])
        print('ok')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == '__main__':
    main()
