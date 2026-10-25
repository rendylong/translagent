#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""声纹库一次性手术与重放验证(方案 A,2026-10-24)。

用法(在 transcriber/ 目录下):
  python3 repair_speakers.py            干跑:只打印拟执行的合并/清理计划,不动任何文件
  python3 repair_speakers.py --apply    执行:先备份 speakers.json 再动库与任务 meta
  python3 repair_speakers.py --replay   只读重放:用各任务已落盘的 dsegs+embeddings 在空库上
                                        按时间序跑新匹配逻辑,打印结果库(验证阈值/逻辑)

手术内容:
  1) 同人双身份合并:库内质心互似 ≥0.85 的条目并成一个(远高于 bench 异人上限 0.557,
     2026-10-24 实测 bae/2eca 双身份质心 0.91);任务 speaker_map 的 gid 随之改写。
  2) 碎片清理:无名字且锚点 ≤1 的条目删除(短会单段簇误入库的垃圾身份),任务
     speaker_map 中指向它的条目摘除。

--apply 前必须停转写服务(lsof -nP -i :8765 找 PID 再 kill):脚本直写 speakers.json,
与服务的内存态之间没有互斥,运行中执行会互相覆盖。备份写 speakers.backup-<时间>.json。
"""
import json
import os
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402

MERGE_LIB = 0.85   # 库内条目互似合并阈:双身份实测 0.91,异人上限 0.557,取高置信区


def service_running():
    import socket
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(('127.0.0.1', 8765)) == 0
    finally:
        s.close()


def plan_surgery(lib):
    """返回 (merges[(src, dst)], junk[spk]):纯计算不动库。"""
    spks = [s for s in lib['speakers'] if s.get('centroid')]
    parent = {s['id']: s['id'] for s in spks}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(spks)):
        for j in range(i + 1, len(spks)):
            va = np.asarray(spks[i]['centroid'], dtype=np.float32)
            vb = np.asarray(spks[j]['centroid'], dtype=np.float32)
            if float(np.dot(va, vb)) >= MERGE_LIB:
                parent[find(spks[j]['id'])] = find(spks[i]['id'])
    by_root = {}
    for s in spks:
        by_root.setdefault(find(s['id']), []).append(s)
    merges = []
    for members in by_root.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda s: (s.get('created_at') or '', s['id']))   # 最老的留作 dst
        for s in members[1:]:
            merges.append((s, members[0]))
    merged_ids = {s['id'] for s, _ in merges}
    junk = [s for s in lib['speakers']
            if s['id'] not in merged_ids
            and (s.get('name') or '').strip() == ''
            and len(s.get('anchors', [])) <= 1]
    return merges, junk


def apply_surgery(lib, merges, junk):
    for src, dst in merges:
        app.merge_library_speakers(lib, src, dst)
        touched = app.rewrite_gid_in_jobs(src['id'], new_gid=dst['id'])
        print('合并 %s(%s) → %s(%s),改写任务 %d 个' % (
            src['id'], src.get('name') or '未命名', dst['id'], dst.get('name') or '未命名', len(touched)))
    for s in junk:
        lib['speakers'] = [x for x in lib['speakers'] if x['id'] != s['id']]
        touched = app.rewrite_gid_in_jobs(s['id'], drop=True)
        print('清理碎片 %s(锚 %d,无名),摘除任务条目 %d 个' % (s['id'], len(s.get('anchors', [])), len(touched)))
    app.save_speakers(lib)


def replay():
    """空库上按任务时间序重放新逻辑(canonicalize + match),只读不动库。"""
    jobs = []
    for jid, meta in app.iter_done_jobs():
        dpath = os.path.join(app.job_dir(jid), 'dsegs.json')
        epath = os.path.join(app.job_dir(jid), 'embeddings.json')
        if os.path.isfile(dpath) and os.path.isfile(epath):
            jobs.append((jid, meta))
    jobs.sort(key=lambda x: (x[1].get('created_at') or '', x[0]))
    lib = {'model': 'replay', 'dim': app.EMB_DIM, 'speakers': []}
    print('== 重放 %d 个任务(新逻辑,空库起)==' % len(jobs))
    for jid, meta in jobs:
        with open(os.path.join(app.job_dir(jid), 'dsegs.json'), encoding='utf-8') as f:
            dsegs = [tuple(x) for x in json.load(f)]
        with open(os.path.join(app.job_dir(jid), 'embeddings.json'), encoding='utf-8') as f:
            raw = json.load(f)
        if not raw:
            print('%s 无声纹数据,跳过' % jid)
            continue
        sigs = {int(k): np.asarray(v, dtype=np.float32) for k, v in raw.items()}
        stats = app.cluster_stats(dsegs)
        old_dsegs = dsegs
        dsegs, sigs, stats = app.canonicalize_clusters(dsegs, sigs, stats)
        groups = {}
        for (s, e, old), (_, _, new) in zip(old_dsegs, dsegs):
            groups.setdefault(new, set()).add(old)
        merged = ['%d←%s' % (lab, ','.join(str(m) for m in sorted(ms - {lab})))
                  for lab, ms in sorted(groups.items()) if ms - {lab}]
        smap = app.match_speakers(sigs, lib, jid, stats)
        print('%s 规整簇 %s → 匹配 %s' % (
            jid, ' '.join(merged),
            ' '.join('c%s→%s(%s,%s)' % (k, v['gid'][-4:], v['status'], v['sim']) for k, v in smap.items())))
    print('== 结果库 ==')
    for s in lib['speakers']:
        jobs_used = ','.join(sorted({a['job_id'][9:13] for a in s.get('anchors', [])}))
        print('%s(%s) 锚 %d,任务 %s' % (s['id'], s.get('name') or '未命名', len(s.get('anchors', [])), jobs_used))
    for i in range(len(lib['speakers'])):
        for j in range(i + 1, len(lib['speakers'])):
            va = np.asarray(lib['speakers'][i]['centroid'], dtype=np.float32)
            vb = np.asarray(lib['speakers'][j]['centroid'], dtype=np.float32)
            sim = float(np.dot(va, vb))
            flag = ' <-- 疑似同人!' if sim >= MERGE_LIB else ''
            print('质心互似 %s vs %s = %.3f%s' % (
                lib['speakers'][i]['id'][-4:], lib['speakers'][j]['id'][-4:], sim, flag))


def main():
    if '--replay' in sys.argv:
        replay()
        return
    lib = app.load_speakers()
    merges, junk = plan_surgery(lib)
    if not merges and not junk:
        print('库无需手术:无互似 ≥%.2f 的条目,无无名单锚碎片。' % MERGE_LIB)
        return
    print('== 手术计划(干跑)==')
    for src, dst in merges:
        sim = float(np.dot(np.asarray(src['centroid'], dtype=np.float32),
                           np.asarray(dst['centroid'], dtype=np.float32)))
        print('合并 %s(%s,锚 %d) → %s(%s,锚 %d)  质心互似 %.3f' % (
            src['id'], src.get('name') or '未命名', len(src.get('anchors', [])),
            dst['id'], dst.get('name') or '未命名', len(dst.get('anchors', [])), sim))
    for s in junk:
        print('清理 %s(锚 %d,无名,来自 %s)' % (
            s['id'], len(s.get('anchors', [])), ','.join(a['job_id'] for a in s.get('anchors', []))))
    if '--apply' not in sys.argv:
        print('\n干跑结束,未改任何文件。确认后执行:python3 repair_speakers.py --apply')
        return
    if service_running():
        print('转写服务正在运行(127.0.0.1:8765),先停服务再 --apply,否则会互相覆盖。')
        sys.exit(1)
    backup = app.SPEAKERS_JSON.replace(
        'speakers.json', 'speakers.backup-%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(app.SPEAKERS_JSON, backup)
    print('\n已备份 → %s' % backup)
    apply_surgery(lib, merges, junk)
    print('手术完成。')


if __name__ == '__main__':
    main()
