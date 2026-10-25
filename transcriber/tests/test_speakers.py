# -*- coding: utf-8 -*-
"""跨会议声纹匹配(方案 A,2026-10-24):场内拆簇规整、弱证据门、认领/学习分离、合并 API。

全合成向量快测(毫秒级,不碰模型与真实库);API 测试用临时 SPEAKERS_JSON/Jobs 目录。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

DIM = app.EMB_DIM


def unit(vec):
    v = np.asarray(vec, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def vec_with_cos(base, cos, seed):
    """构造与 base 余弦恰为 cos 的单位向量(在与 base 正交的方向上加分量)。"""
    rng = np.random.RandomState(seed)
    base = unit(base)
    noise = rng.randn(DIM).astype(np.float32)
    noise -= base * float(np.dot(noise, base))   # 正交化
    noise = unit(noise)
    return unit(base * cos + noise * float(np.sqrt(max(0.0, 1 - cos * cos))))


class CanonicalizeTest(unittest.TestCase):
    BASE = unit(np.arange(DIM, dtype=np.float32) % 7)

    def test_merges_similar_and_relabels(self):
        dsegs = [(0, 5, 0), (6, 11, 4), (12, 17, 7)]
        sigs = {0: vec_with_cos(self.BASE, 1.0, 1),
                4: vec_with_cos(self.BASE, 0.79, 2),    # 同人拆簇(实测区间 0.72-0.79)
                7: vec_with_cos(self.BASE, 0.2, 3)}     # 异人
        stats = {0: {'n': 5, 'sec': 25}, 4: {'n': 4, 'sec': 20}, 7: {'n': 5, 'sec': 25}}
        nd, ns, nst = app.canonicalize_clusters(dsegs, sigs, dict(stats))
        self.assertEqual([s for _, _, s in nd], [0, 0, 7])          # c4 并入 c0,c7 不动
        self.assertEqual(sorted(ns), [0, 7])
        self.assertEqual(nst[0]['n'], 9)                             # 证据量随合并累加
        # 合并签名 = 按段数加权均值(两簇同向时仍在同向附近)
        self.assertGreater(float(np.dot(ns[0], self.BASE)), 0.95)

    def test_no_merge_below_theta(self):
        dsegs = [(0, 5, 0), (6, 11, 3)]
        sigs = {0: vec_with_cos(self.BASE, 1.0, 4), 3: vec_with_cos(self.BASE, 0.5, 5)}
        nd, ns, _ = app.canonicalize_clusters(dsegs, sigs, {0: {'n': 3, 'sec': 9}, 3: {'n': 3, 'sec': 9}})
        self.assertEqual([s for _, _, s in nd], [0, 3])
        self.assertEqual(len(ns), 2)

    def test_recompute_prevents_chaining(self):
        # a~b=0.8、b~c=0.8 但 a~c=0.28:并掉最高分对后,合成签名与 c ≈0.57 <阈,
        # 不再按原始相似度链式误并(若不重算合成签名,c 会经 b 被拖进来)
        a = unit(np.array([0.8, 0.6] + [0.0] * (DIM - 2), dtype=np.float32))
        b = unit(np.array([1.0, 0.0] + [0.0] * (DIM - 2), dtype=np.float32))
        c = unit(np.array([0.8, -0.6] + [0.0] * (DIM - 2), dtype=np.float32))
        self.assertAlmostEqual(float(np.dot(a, b)), 0.8, places=5)
        self.assertAlmostEqual(float(np.dot(b, c)), 0.8, places=5)
        self.assertAlmostEqual(float(np.dot(a, c)), 0.28, places=5)
        dsegs = [(0, 4, 0), (5, 9, 1), (10, 14, 2)]
        sigs = {0: a, 1: b, 2: c}
        stats = {0: {'n': 3, 'sec': 12}, 1: {'n': 3, 'sec': 12}, 2: {'n': 3, 'sec': 12}}
        nd, ns, _ = app.canonicalize_clusters(dsegs, sigs, stats)
        self.assertEqual(sorted(ns), [0, 2])            # b 并入 a(或对称),c 独立
        self.assertEqual(len({s for _, _, s in nd}), 2)

    def test_cluster_stats_split_and_filter(self):
        dsegs = [(0, 30, 0),          # 超长段:12+12+6 → 3 块(尾块 6s>0.3 保留)
                 (31, 32, 0),         # <min_seg 不计
                 (40, 45, 1)]
        st = app.cluster_stats(dsegs)
        self.assertEqual(st[0]['n'], 3)
        self.assertAlmostEqual(st[0]['sec'], 30.0)      # 只累计 ≥min_seg 的段
        self.assertEqual(st[1]['n'], 1)


class MatchTest(unittest.TestCase):
    BASE = unit(np.arange(DIM, dtype=np.float32) % 5)

    def _lib_with_speaker(self, name='', anchor=None):
        lib = {'model': 't', 'dim': DIM, 'speakers': []}
        spk = app.register_speaker(lib, name)
        app.spk_add_anchor(spk, 'job_seed', 0, 1.0, anchor if anchor is not None else self.BASE)
        return lib, spk

    def test_high_sim_strong_evidence_anchors(self):
        lib, spk = self._lib_with_speaker('甲')
        sigs = {0: vec_with_cos(self.BASE, 0.75, 8)}
        smap = app.match_speakers(sigs, lib, 'j1', {0: {'n': 5, 'sec': 20}})
        self.assertEqual(smap['0']['status'], 'auto')
        self.assertEqual(smap['0']['gid'], spk['id'])
        self.assertEqual(spk['n_anchors'], 2)                      # ≥SIM_ANCHOR 且证据充分 → 学习

    def test_borderline_auto_claims_but_does_not_learn(self):
        lib, spk = self._lib_with_speaker('甲')
        sigs = {0: vec_with_cos(self.BASE, 0.66, 9)}
        smap = app.match_speakers(sigs, lib, 'j1', {0: {'n': 5, 'sec': 20}})
        self.assertEqual(smap['0']['status'], 'auto')              # 0.65~0.70:只认不学
        self.assertEqual(spk['n_anchors'], 1)

    def test_weak_evidence_claims_but_never_learns(self):
        lib, spk = self._lib_with_speaker('甲')
        sigs = {0: vec_with_cos(self.BASE, 0.9, 10)}
        smap = app.match_speakers(sigs, lib, 'j1', {0: {'n': 1, 'sec': 2}})
        self.assertEqual(smap['0']['status'], 'auto')
        self.assertEqual(spk['n_anchors'], 1)                      # 弱证据:高似也不加锚

    def test_weak_evidence_never_registers_new(self):
        lib = {'model': 't', 'dim': DIM, 'speakers': []}
        sigs = {0: vec_with_cos(self.BASE, 0.1, 11)}               # 与库里任何人都低似
        smap = app.match_speakers(sigs, lib, 'j1', {0: {'n': 1, 'sec': 2}})
        self.assertEqual(smap, {})                                 # 碎片簇不建条目
        self.assertEqual(lib['speakers'], [])

    def test_strong_unmatched_registers_new(self):
        lib = {'model': 't', 'dim': DIM, 'speakers': []}
        sigs = {0: vec_with_cos(self.BASE, 0.1, 12)}
        smap = app.match_speakers(sigs, lib, 'j1', {0: {'n': 4, 'sec': 16}})
        self.assertEqual(smap['0']['status'], 'new')
        self.assertEqual(lib['speakers'][0]['n_anchors'], 1)

    def test_suspect_band_unchanged(self):
        lib, spk = self._lib_with_speaker('甲')
        sigs = {0: vec_with_cos(self.BASE, 0.55, 13)}
        smap = app.match_speakers(sigs, lib, 'j1', {0: {'n': 5, 'sec': 20}})
        self.assertEqual(smap['0']['status'], 'suspect')
        self.assertEqual(spk['n_anchors'], 1)


class MergeApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self._old_speakers, self._old_jobs = app.SPEAKERS_JSON, app.JOBS_DIR
        app.SPEAKERS_JSON = str(base / 'speakers.json')
        app.JOBS_DIR = str(base / 'jobs')
        os.makedirs(app.JOBS_DIR, exist_ok=True)
        lib = {'model': 't', 'dim': DIM, 'speakers': []}
        self.src = app.register_speaker(lib, '')
        app.spk_add_anchor(self.src, 'jobA', 2, 0.9, MergeApiTest.V1)
        app.spk_add_anchor(self.src, 'jobB', 1, 0.8, MergeApiTest.V1)
        self.dst = app.register_speaker(lib, '乙')
        app.spk_add_anchor(self.dst, 'jobA', 1, 0.9, unit(MergeApiTest.V1 * 1.0))
        app.save_speakers(lib)
        # 一个指向 src 的已完成任务
        os.makedirs(os.path.join(app.JOBS_DIR, 'jobB'), exist_ok=True)
        with open(os.path.join(app.JOBS_DIR, 'jobB', 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump({'status': 'done', 'speaker_map': {
                '1': {'gid': self.src['id'], 'sim': 0.8, 'status': 'auto'},
                '0': {'gid': self.dst['id'], 'sim': 0.9, 'status': 'auto'}}}, f)
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()

    V1 = unit(np.arange(DIM, dtype=np.float32) % 3)

    def tearDown(self):
        app.SPEAKERS_JSON, app.JOBS_DIR = self._old_speakers, self._old_jobs
        self._tmp.cleanup()

    def test_merge_moves_anchors_and_rewrites_jobs(self):
        r = self.client.post('/api/speakers/merge',
                             json={'src': self.src['id'], 'dst': self.dst['id']})
        self.assertEqual(r.status_code, 200)
        lib = app.load_speakers()
        self.assertEqual([s['id'] for s in lib['speakers']], [self.dst['id']])
        kept = lib['speakers'][0]
        self.assertEqual(kept['n_anchors'], 3)                     # jobA-c1 去重后 3 个锚
        self.assertEqual(kept['name'], '乙')                        # dst 有名不继承
        with open(os.path.join(app.JOBS_DIR, 'jobB', 'meta.json'), encoding='utf-8') as f:
            smap = json.load(f)['speaker_map']
        self.assertEqual({v['gid'] for v in smap.values()}, {self.dst['id']})

    def test_merge_rejects_same_and_missing(self):
        self.assertEqual(self.client.post(
            '/api/speakers/merge', json={'src': self.dst['id'], 'dst': self.dst['id']}).status_code, 400)
        self.assertEqual(self.client.post(
            '/api/speakers/merge', json={'src': 'sp_none', 'dst': self.dst['id']}).status_code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)
