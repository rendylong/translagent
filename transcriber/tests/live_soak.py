# -*- coding: utf-8 -*-
"""2 小时长跑(Task 9 Step 6):循环拼接 zh 锚点音频回放一场超长 live 会话(进程内,
不起服务、不走 WS),观察 spec §8 清单:内存平稳(RSS 波动 <20%)、无未捕获异常、
pass2 全窗出齐、静音幻觉窗、复读环、碎簇数(θ=0.40 已知风险)。

观察项实现:
  - RSS:每 15s 采 ps rss;剔除首分钟(模型加载)后统计 max/min/mean 与波动幅度;
  - 窗口齐全:pass2+pass2_skip 数 vs 预期(总时长/27.5s);stopped 后游标应 == 总时长;
  - 复读环:连续 ≥3 个 pass2 文本完全相同;
  - 幻觉:pass2 文本命中「点赞/订阅/关注频道/感谢观看」类静音幻觉词表;
  - 碎簇:采样到的最大 speaker 簇数(2 人会话预期 ≤4,>5 报警);
  - LLM 层默认禁用(数百窗真实外呼既不稳定也不必要;--llm 1 打开)。

转正走真实 finalize_session(临时 jobs 目录 + 一次性队列,不进工作台)。
运行:cd transcriber && python3 tests/live_soak.py [--minutes 120] [--llm 0]
真实跑 2h 回放约需 20-40 分钟墙钟(whisper GPU 串行是瓶颈)。
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import app  # noqa: E402
import live_finalize  # noqa: E402
import live_session  # noqa: E402
from live_session import LiveSession  # noqa: E402

CHUNK = 3200                    # 200ms @16k mono s16le
SAMPLE_INTERVAL = 15            # RSS 采样周期(秒)
HALLUC_WORDS = ('点赞', '订阅', '关注频道', '感谢观看', '请不吝', '一键三连')


def rss_kb():
    out = subprocess.run(['ps', '-p', str(os.getpid()), '-o', 'rss='],
                         capture_output=True, text=True)
    return int(out.stdout.strip())


def load_anchor_pcm():
    anchors = json.load(open(BASE / 'models' / 'bench' / 'anchors.json'))
    x = app.read_wav_float(anchors['zh']['wav'])[:600 * 16000]      # 取前 10min 做循环母本
    q = np.clip(x * 32768.0, -32768.0, 32767.0)
    return q.astype('<i2').tobytes()


def feed_all(sess, pcm, total_bytes, stop_flag, progress, speed=0.0):
    """循环拼接母本喂满 total_bytes;speed>0 时按 speed 倍实时配速(每 0.1s 音频
    睡 0.1/speed),让 Pass1(rtf≈0.04,约 25 倍实时)边喂边消化、写线程队列不积压
    ——全速猛灌会把整场音频堆进队列,RSS 暴涨是测试假象不是泄漏。"""
    off = 0
    fed = 0
    chunk_s = CHUNK / 32000.0
    while fed < total_bytes and not stop_flag.is_set():
        take = min(CHUNK, total_bytes - fed, len(pcm) - off)
        if off + take < len(pcm):
            sess.feed_bytes(pcm[off:off + take])
            off += take
        else:                                   # 跨母本边界:拼两段
            part = pcm[off:]
            need = take - len(part)
            sess.feed_bytes(part + pcm[:need])
            off = need
        fed += take
        if speed > 0 and (fed // CHUNK) % 10 == 0:
            time.sleep(chunk_s / speed * 10)
        if (fed // CHUNK) % 500 == 0:
            progress['fed_s'] = fed / 32000.0
            time.sleep(0.002)
    progress['fed_s'] = fed / 32000.0
    return fed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--minutes', type=float, default=120)
    ap.add_argument('--speed', type=float, default=8.0,
                    help='回放倍速(0=全速)。8 倍时 Pass1 边喂边消化,队列不积压')
    ap.add_argument('--llm', type=int, default=0, help='1=启用真实 LLM 层(数百次外呼)')
    ap.add_argument('--out', default=str(BASE / 'tests' / 'soak_report.json'))
    args = ap.parse_args()

    if not args.llm:
        live_session.parse_key = lambda: None   # soak 默认禁 Pass3(稳定性观测不依赖它)

    pcm = load_anchor_pcm()
    total_bytes = int(args.minutes * 60 * 32000)
    print('soak %.0f 分钟(%.0f 倍速),母本 %.0fs,总字节 %.1fMB'
          % (args.minutes, args.speed or 99, len(pcm) / 32000, total_bytes / 1e6))

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    jobs_dir = root / 'jobs'
    jobs_dir.mkdir()
    soak_q = queue.Queue()

    def finalizer(d, speakers_hint=None):
        return live_finalize.finalize_session(d, speakers_hint=speakers_hint,
                                              jobs_dir=str(jobs_dir), job_queue=soak_q)

    sess = LiveSession(str(root), str(BASE / 'models' / 'streaming-zipformer-zh-en'),
                       finalizer=finalizer)
    sid = sess.start('长跑 %.0fmin' % args.minutes, 2, 'zh>en')
    print('session=%s' % sid)

    # 事件消费 + 统计
    stats = {'counts': {}, 'pass2_texts': [], 'errors': [], 'cluster_max': 0, 'samples': []}
    stop_flag = threading.Event()

    def consumer():
        while not stop_flag.is_set():
            try:
                ev = sess.events.get(timeout=0.3)
            except queue.Empty:
                continue
            t = ev.get('type')
            stats['counts'][t] = stats['counts'].get(t, 0) + 1
            if t == 'pass2':
                stats['pass2_texts'].append(ev.get('text', ''))
            elif t == 'error':
                stats['errors'].append(str(ev))
            elif t == 'stopped':
                return

    def sampler():
        t0 = time.time()
        while not stop_flag.is_set():
            try:
                cl = sess._cluster.n_speakers
                stats['cluster_max'] = max(stats['cluster_max'], cl)
            except Exception:  # noqa: BLE001
                pass
            stats['samples'].append({'t': round(time.time() - t0, 1), 'rss_mb': round(rss_kb() / 1024, 1),
                                     'fed_s': round(progress.get('fed_s', 0), 1),
                                     'cursor_s': round(sess._cursor_ms / 1000.0, 1)})
            time.sleep(SAMPLE_INTERVAL)

    progress = {'fed_s': 0.0}
    th_c = threading.Thread(target=consumer, daemon=True)
    th_s = threading.Thread(target=sampler, daemon=True)
    th_c.start()
    th_s.start()

    t_start = time.time()
    fed = feed_all(sess, pcm, total_bytes, stop_flag, progress, speed=args.speed)
    feed_wall = time.time() - t_start
    print('喂完 %.0fs 音频(墙钟 %.0fs),停表转正…' % (fed / 32000, feed_wall))
    sess.handle_stop()

    deadline = time.time() + 25 * 60
    while time.time() < deadline:
        if not th_c.is_alive():
            break
        time.sleep(1)
    stop_flag.set()
    th_s.join(timeout=5)
    th_c.join(timeout=5)
    total_wall = time.time() - t_start

    # ---- 分析 ----
    rss = [s['rss_mb'] for s in stats['samples'][4:]] or [0]   # 剔除首分钟模型加载
    rss_min, rss_max = min(rss), max(rss)
    rss_mean = sum(rss) / len(rss)
    texts = stats['pass2_texts']
    reps = sum(1 for i in range(2, len(texts)) if texts[i] == texts[i - 1] == texts[i - 2])
    hallu = sum(1 for t in texts if any(w in t for w in HALLUC_WORDS))
    n_win = stats['counts'].get('pass2', 0)
    n_skip = stats['counts'].get('pass2_skip', 0)
    expected = fed / 32000.0 / 27.5
    cursor_final = sess._cursor_ms / 1000.0
    report = {
        'session': sid, 'minutes_requested': args.minutes, 'speed': args.speed,
        'audio_s': round(fed / 32000, 1), 'feed_wall_s': round(feed_wall),
        'total_wall_s': round(total_wall),
        'windows': {'pass2': n_win, 'skip': n_skip, 'expected': round(expected, 1),
                    'cursor_final_s': round(cursor_final, 1)},
        'clusters_max': stats['cluster_max'],
        'hallucination_windows': hallu, 'repeat_runs': reps,
        'llm_enabled': bool(args.llm),
        'events': stats['counts'], 'errors': stats['errors'][:5],
        'rss': {'min_mb': rss_min, 'max_mb': rss_max, 'mean_mb': round(rss_mean, 1),
                'swing_pct': round((rss_max - rss_min) / rss_mean * 100, 1)},
        'rss_curve': stats['samples'],
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')

    ok = True
    print('\n===== soak 结果 =====')
    print('音频 %.0fs,墙钟 %.0fs(喂 %.0fs + 转正 %.0fs)'
          % (fed / 32000, total_wall, feed_wall, total_wall - feed_wall))
    print('窗口:pass2=%d skip=%d(预期 ~%.0f)游标终值 %.1fs'
          % (n_win, n_skip, expected, cursor_final))
    print('碎簇最大数:%d' % stats['cluster_max'])
    print('静音幻觉窗:%d,复读环:%d' % (hallu, reps))
    print('RSS:min=%.0fMB max=%.0fMB mean=%.0fMB 波动 %.1f%%'
          % (rss_min, rss_max, rss_mean, report['rss']['swing_pct']))
    print('事件计数:%s' % stats['counts'])
    if stats['errors']:
        print('error 事件(前5):%s' % stats['errors'][:5])
    if abs(cursor_final - fed / 32000) > 1.0:
        print('!! 游标未推满(%.1fs / %.1fs),窗口不齐' % (cursor_final, fed / 32000))
        ok = False
    if report['rss']['swing_pct'] > 20:
        print('!! RSS 波动 %.1f%% 超 20%%' % report['rss']['swing_pct'])
        ok = False
    if stats['cluster_max'] > 5:
        print('!! 碎簇 %d 个(θ=0.40 风险项,需关注)' % stats['cluster_max'])
    if stats['errors']:
        ok = False
    print('报告:%s' % args.out)
    print('结论:%s' % ('PASS' if ok else 'FAIL'))
    tmp.cleanup()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
