# -*- coding: utf-8 -*-
"""两层管线长跑稳定性闸门(2026-10-24 摘 Pass2 后重写;旧 pass2 窗口齐全口径已废):
循环拼接 zh 音频回放一场超长 live 会话(进程内,不起服务、不走 WS),验证两层管线
(Pass1 整句 → 内联 TitaNet 分人 → LLM 合并批)长跑不漏不炸。

闸门(全部满足才 PASS):
  - RSS 平稳:剔除模型加载窗(t≥30s,zipformer 构造 + TitaNet 首次分人)后,
    首末采样漂移 ≤±15%(沿旧版波动阈值口径);样本不足(短跑)时不判定只提示;
  - error 事件数为 0(全事件流);
  - pass2/pass2_skip 绝迹(两层管线无 Pass2,计数非 0 即旧路径泄漏);
  - pass1_final ≥ 1(识别流确实在出整句 final);
  - 有 key(--llm 1 且解析到 key):llm ≥ 1,且最后一条 llm 的 seq < stopped 的
    seq(停止时在飞批排空后才 stopped,spec §4.2);
    无 key(--llm 0):全程无 llm 事件且 llm_enabled 为 False;
  - LLM 内部队列最终排空(stopped 之后 _llm_q 为空);
  - 说话人簇数 ≤ speakers 参数 + 1(防碎簇爆炸);
  - live_pass1.jsonl 行数 == pass1_final 事件数(final 落盘守恒;行数在 finalizer
    删会话目录前顺手清点)。

骨架沿用旧版:--minutes/--speed/--llm/--out 参数、RSS 每 15s 采样线程、事件消费
线程、统计报告 JSON + 控制台摘要。母本音频:anchors.json 的 zh 锚点优先;本机
没有(旧机器绝对路径)时回落 BASE/jobs 下最新的 ≥30s audio16k.wav,再没有则
say Tingting 现场合成 zh 朗读。LLM 层默认禁用(长跑数百次真实外呼既慢也不稳;
--llm 1 打开)。转正走真实 finalize_session(临时 jobs 目录 + 一次性队列,不进
工作台)。运行:cd transcriber && python3 tests/live_soak.py [--minutes 120] [--llm 0]
真实跑 2h 回放约需 20-40 分钟墙钟(8 倍速配速让 Pass1 边喂边消化)。
快速冒烟:python3 tests/live_soak.py --minutes 0.5 --speed 0 --out /tmp/soak_smoke.json
(全速灌 ~30s 音频,几分钟内出结论;RSS 漂移项样本不足自动跳过)。
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
SPEAKERS = 2                    # 会话说话人数(碎簇闸门上限 = SPEAKERS + 1)
RSS_WARM_S = 30                 # RSS 暖机窗:zipformer+TitaNet 模型加载
RSS_DRIFT_PCT = 15              # 首末采样漂移闸门(±%)
SYNTH_TEXT = ('各位好,现在开始本周的财务例会。'
              '先看收入,第三季度营业收入环比增长百分之十二,毛利率保持稳定。'
              '成本端,原材料采购成本下降百分之五,物流费用与上季度基本持平。'
              '现金流方面,经营活动净现金流为正,应收账款周转天数缩短了三天。'
              '下面请大家结合报表提问,我们逐项讨论。'
              '最后提醒,下周三之前把部门预算初稿提交给财务部,逾期不再受理。')


def rss_kb():
    out = subprocess.run(['ps', '-p', str(os.getpid()), '-o', 'rss='],
                         capture_output=True, text=True)
    return int(out.stdout.strip())


def load_mother_pcm():
    """母本 PCM(s16le 字节)+ 来源描述:anchors zh 锚点(前 10min)→ BASE/jobs
    最新 ≥30s 的 audio16k.wav → say Tingting 现场合成(anchors 是旧机器绝对路径,
    换机器后前两级常双双落空,合成兜底保证脚本随处可跑)。"""
    src = None
    desc = None
    try:
        anchors = json.load(open(BASE / 'models' / 'bench' / 'anchors.json'))
        p = Path(anchors['zh']['wav'])
        if p.exists():
            src, desc = p, 'anchors zh 锚点'
    except Exception:  # noqa: BLE001 anchors 缺失/损坏走回落
        pass
    if src is None:
        for p in sorted((BASE / 'jobs').glob('*/audio16k.wav'),
                        key=lambda q: q.stat().st_mtime, reverse=True):
            if p.stat().st_size >= 32000 * 30:      # ≥30s 才当母本(太短循环过密)
                src, desc = p, 'jobs 回落 %s' % p.parent.name
                break
    if src is not None:
        x = app.read_wav_float(str(src))[:600 * 16000]
    else:
        desc = 'say Tingting 现场合成'
        with tempfile.TemporaryDirectory() as td:
            aiff, wav = os.path.join(td, 'm.aiff'), os.path.join(td, 'm.wav')
            subprocess.run(['say', '-v', 'Tingting', '-o', aiff, SYNTH_TEXT], check=True)
            subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1',
                            aiff, wav], check=True)
            x = app.read_wav_float(wav)
    q = np.clip(x * 32768.0, -32768.0, 32767.0)
    return q.astype('<i2').tobytes(), desc


def feed_all(sess, pcm, total_bytes, stop_flag, progress, speed=0.0):
    """循环拼接母本喂满 total_bytes;speed>0 时按 speed 倍实时配速(每 0.1s 音频
    睡 0.1/speed),让 Pass1 边喂边消化、写线程队列不积压——全速猛灌会把整场音频
    堆进队列,RSS 暴涨是测试假象不是泄漏(短冒烟音频量小,全速无妨)。"""
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
        live_session.parse_key = lambda: None   # soak 默认禁 LLM 层(稳定性观测不依赖它)

    pcm, src_desc = load_mother_pcm()
    total_bytes = int(args.minutes * 60 * 32000)
    print('soak %.0f 分钟(%.0f 倍速),母本 %s %.0fs,总字节 %.1fMB'
          % (args.minutes, args.speed or 99, src_desc, len(pcm) / 32000, total_bytes / 1e6))

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    jobs_dir = root / 'jobs'
    jobs_dir.mkdir()
    soak_q = queue.Queue()

    stats = {'counts': {}, 'errors': [], 'llm_seqs': [], 'stopped_seq': None,
             'cluster_max': 0, 'jsonl_lines': None, 'samples': []}

    def finalizer(d, speakers_hint=None):
        # 会话目录马上要被 finalize 删除:先清点 live_pass1.jsonl 行数(落盘守恒闸门)
        jp = os.path.join(d, 'live_pass1.jsonl')
        if os.path.exists(jp):
            with open(jp, encoding='utf-8') as f:
                stats['jsonl_lines'] = sum(1 for ln in f if ln.strip())
        return live_finalize.finalize_session(d, speakers_hint=speakers_hint,
                                              jobs_dir=str(jobs_dir), job_queue=soak_q)

    sess = LiveSession(str(root), str(BASE / 'models' / 'streaming-zipformer-zh-en'),
                       finalizer=finalizer)
    sid = sess.start('长跑 %.0fmin' % args.minutes, SPEAKERS, 'zh>en')
    print('session=%s' % sid)

    # 事件消费 + 统计
    stop_flag = threading.Event()

    def consumer():
        while not stop_flag.is_set():
            try:
                ev = sess.events.get(timeout=0.3)
            except queue.Empty:
                continue
            t = ev.get('type')
            stats['counts'][t] = stats['counts'].get(t, 0) + 1
            if t == 'error':
                stats['errors'].append(str(ev))
            elif t == 'llm':
                stats['llm_seqs'].append(ev.get('seq', 0))
            elif t == 'stopped':
                stats['stopped_seq'] = ev.get('seq', 0)
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
                                     'dur_s': round(sess.duration_s(), 1)})
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

    # ---- 分析(两层口径闸门) ----
    warm = [s for s in stats['samples'] if s['t'] >= RSS_WARM_S]   # 剔模型加载窗
    rss = [s['rss_mb'] for s in warm] or [s['rss_mb'] for s in stats['samples']]
    drift = None
    if len(warm) >= 2:
        drift = (warm[-1]['rss_mb'] - warm[0]['rss_mb']) / warm[0]['rss_mb'] * 100.0
    counts = stats['counts']
    n_p2 = counts.get('pass2', 0) + counts.get('pass2_skip', 0)
    n_final = counts.get('pass1_final', 0)
    n_llm = counts.get('llm', 0)
    last_llm_seq = stats['llm_seqs'][-1] if stats['llm_seqs'] else None
    llm_q_empty = sess._llm_q.empty()
    llm_on = bool(getattr(sess, 'llm_enabled', False))
    report = {
        'session': sid, 'minutes_requested': args.minutes, 'speed': args.speed,
        'audio_s': round(fed / 32000, 1), 'feed_wall_s': round(feed_wall),
        'total_wall_s': round(total_wall),
        'llm_enabled': llm_on, 'llm_requested': bool(args.llm),
        'pass1_final': n_final, 'pass2_leak': n_p2, 'jsonl_lines': stats['jsonl_lines'],
        'llm': {'count': n_llm, 'last_seq': last_llm_seq,
                'stopped_seq': stats['stopped_seq'], 'queue_empty_after_stop': llm_q_empty},
        'clusters_max': stats['cluster_max'], 'cluster_cap': SPEAKERS + 1,
        'events': counts, 'errors': stats['errors'][:5],
        'rss': {'warm_samples': len(warm), 'drift_pct': None if drift is None else round(drift, 1),
                'min_mb': min(rss) if rss else 0, 'max_mb': max(rss) if rss else 0,
                'mean_mb': round(sum(rss) / len(rss), 1) if rss else 0},
        'rss_curve': stats['samples'],
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')

    ok = True
    print('\n===== soak 结果(两层闸门) =====')
    print('音频 %.0fs,墙钟 %.0fs(喂 %.0fs + 转正 %.0fs)'
          % (fed / 32000, total_wall, feed_wall, total_wall - feed_wall))
    print('事件计数:%s' % counts)
    print('pass1_final=%d,live_pass1.jsonl 行数=%s(守恒应相等)' % (n_final, stats['jsonl_lines']))
    print('pass2/pass2_skip 泄漏:%d(两层管线应为 0)' % n_p2)
    print('LLM:%s(llm=%d,最后 llm seq=%s,stopped seq=%s,停止后队列空=%s)'
          % ('启用' if llm_on else '禁用', n_llm, last_llm_seq, stats['stopped_seq'], llm_q_empty))
    print('碎簇最大数:%d(上限 %d = speakers+1)' % (stats['cluster_max'], SPEAKERS + 1))
    if drift is not None:
        print('RSS(暖机后 %d 采样):首 %.0fMB → 末 %.0fMB,漂移 %.1f%%(闸门 ±%d%%)'
              % (len(warm), warm[0]['rss_mb'], warm[-1]['rss_mb'], drift, RSS_DRIFT_PCT))
    else:
        print('RSS:暖机后样本不足(%d 个),漂移不判定(短跑冒烟口径,仅供参考 %s)'
              % (len(warm), rss or '无采样'))
    if stats['errors']:
        print('error 事件(前5):%s' % stats['errors'][:5])

    if stats['errors']:
        print('!! error 事件 %d 条(应为 0)' % len(stats['errors']))
        ok = False
    if n_p2:
        print('!! pass2/pass2_skip 共 %d 条,旧路径泄漏' % n_p2)
        ok = False
    if n_final < 1:
        print('!! pass1_final=%d(识别流未出整句)' % n_final)
        ok = False
    if stats['stopped_seq'] is None:
        print('!! 未收到 stopped 事件')
        ok = False
    if llm_on:
        if n_llm < 1:
            print('!! LLM 启用但 llm 事件 %d 条(应 ≥1)' % n_llm)
            ok = False
        if last_llm_seq is None or stats['stopped_seq'] is None \
                or not last_llm_seq < stats['stopped_seq']:
            print('!! 最后 llm seq=%s 不在 stopped seq=%s 之前(在飞批排空次序破坏)'
                  % (last_llm_seq, stats['stopped_seq']))
            ok = False
    else:
        if n_llm:
            print('!! LLM 未启用却有 llm 事件 %d 条' % n_llm)
            ok = False
        if args.llm:
            print('注:--llm 1 但未解析到 key,按无 key 口径判定')
    if not llm_q_empty:
        print('!! stopped 后 LLM 内部队列未排空')
        ok = False
    if stats['cluster_max'] > SPEAKERS + 1:
        print('!! 碎簇 %d 个,超上限 %d(碎簇爆炸)' % (stats['cluster_max'], SPEAKERS + 1))
        ok = False
    if stats['jsonl_lines'] is None or stats['jsonl_lines'] != n_final:
        print('!! 落盘守恒破坏:jsonl 行数 %s != pass1_final %d'
              % (stats['jsonl_lines'], n_final))
        ok = False
    if drift is not None and abs(drift) > RSS_DRIFT_PCT:
        print('!! RSS 首末漂移 %.1f%% 超 ±%d%%' % (drift, RSS_DRIFT_PCT))
        ok = False
    print('报告:%s' % args.out)
    print('结论:%s' % ('PASS' if ok else 'FAIL'))
    tmp.cleanup()
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
