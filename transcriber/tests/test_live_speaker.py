# -*- coding: utf-8 -*-
"""titanet 在线聚类说话人分离的行为测试(Task 6)。

两块:
  - AnchorsAlternateTest:bench anchors 双说话人窗口交替喂 A/B 各 3 段
    → n_speakers==2、段→簇映射与 anchors 标签一致率 ≥5/6、首段开簇;
  - 短段路径:<min_turn_s 归上一簇(不开新簇、不动簇心),首个短段也必须开簇。

(原「Pass2 接线」测试类——speaker_cluster 经 pass2 窗口跑出带 speaker 的段落/
事件——已随 Pass2 于 2026-10-24 摘除退役;两层管线的分人接线由 test_live_session
的 test_on_final_speaker_assign 覆盖。)

窗口选择依据(2026-10-23 实测,DEFAULT_THETA=0.40 下 6/6,见 task-6-report):
  - en:两说话人各自前 3 窗(anchors.json 顺序);
  - zh:sp0 取列表下标 (0,2,3)=[9.8, 4389.3, 4804.9]、sp1 取 (5,6,7)=[4499.1,
    4529.1, 4555.9](避开寒暄区串音窗口 39.1/64.3/4073.7 与离群窗 82.3)。
锚点窗均 ≤6s < CHUNK_S(8s),单块喂入,不触发长段分块路径。
音频与标签同源:models/bench/anchors.json + 对应 job wav。
"""
import json
import sys
import unittest
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402  复用工作台的常量与 RIFF 解析
from live_speaker import DEFAULT_THETA, OnlineSpeakerCluster  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
ANCHORS = json.load(open(BASE / 'models' / 'bench' / 'anchors.json'))
SR = 16000


def seg_of(key, label, idx):
    """anchors[key][label] 第 idx 个窗口的音频切片(float32 PCM)。"""
    t0, t1 = ANCHORS[key][label][idx]
    x = app.read_wav_float(ANCHORS[key]['wav'])
    return np.ascontiguousarray(x[int(t0 * SR):int(t1 * SR)], dtype=np.float32)


class AnchorsAlternateTest(unittest.TestCase):
    """交替喂 A/B 各 3 段:2 簇、映射一致率 ≥5/6、首段开簇。"""

    def _run(self, key, idx_a, idx_b):
        labs = sorted(l for l in ANCHORS[key] if l != 'wav')   # en: candidate/interviewer;zh: sp0/sp1
        a, b = labs[0], labs[1]
        clu = OnlineSpeakerCluster()          # 默认 θ
        self.assertEqual(clu.n_speakers, 0)
        preds, labels = [], []
        for i, j in zip(idx_a, idx_b):        # A1 B1 A2 B2 A3 B3
            labels += [a, b]
            preds += [clu.assign(seg_of(key, a, i)), clu.assign(seg_of(key, b, j))]
        return clu, labels, preds

    def test_first_segment_opens_cluster(self):
        """首段必开簇:第 1 次 assign 返回 0,此后(只有 1 段时)n_speakers==1。"""
        clu, labels, preds = self._run('en', (0, 1, 2), (0, 1, 2))
        self.assertEqual(preds[0], 0)
        clu2 = OnlineSpeakerCluster()
        clu2.assign(seg_of('en', 'interviewer', 0))
        self.assertEqual(clu2.n_speakers, 1)

    def test_en_alternating_two_speakers(self):
        """英文双男声面试:交替 6 段 → 2 簇,映射一致率 ≥5/6。"""
        clu, labels, preds = self._run('en', (0, 1, 2), (0, 1, 2))
        self.assertEqual(clu.n_speakers, 2)
        self.assertGreaterEqual(self._consistency(labels, preds), 5)

    def test_zh_alternating_two_speakers(self):
        """中文双人会议:交替 6 段 → 2 簇,映射一致率 ≥5/6。"""
        clu, labels, preds = self._run('zh', (0, 2, 3), (5, 6, 7))
        self.assertEqual(clu.n_speakers, 2)
        self.assertGreaterEqual(self._consistency(labels, preds), 5)

    @staticmethod
    def _consistency(labels, preds):
        """多数映射口径的一致率:每标签取其多数簇,数一致的段。"""
        votes = defaultdict(Counter)
        for l, k in zip(labels, preds):
            votes[l][k] += 1
        mapping = {l: c.most_common(1)[0][0] for l, c in votes.items()}
        return sum(1 for l, k in zip(labels, preds) if mapping[l] == k)


class ShortSegmentTest(unittest.TestCase):
    """<min_turn_s 的段:归上一簇,不开新簇;首个短段也必须开簇(无处可归)。"""

    def test_short_joins_previous_cluster(self):
        clu = OnlineSpeakerCluster()
        clu.assign(seg_of('zh', 'sp0', 0))          # 开簇 0(6s 窗)
        before = clu.n_speakers
        k = clu.assign(seg_of('zh', 'sp1', 5)[:800])   # 0.5s 短段(sp1 音频,若算声纹会开新簇)
        self.assertEqual(k, 0)
        self.assertEqual(clu.n_speakers, before)

    def test_first_short_segment_still_opens_cluster(self):
        clu = OnlineSpeakerCluster()
        k = clu.assign(seg_of('zh', 'sp1', 5)[:800])
        self.assertEqual(k, 0)
        self.assertEqual(clu.n_speakers, 1)

    def test_empty_pcm_joins_previous(self):
        clu = OnlineSpeakerCluster()
        clu.assign(seg_of('zh', 'sp0', 0))
        self.assertEqual(clu.assign(np.zeros(0, dtype=np.float32)), 0)
        self.assertEqual(clu.n_speakers, 1)


if __name__ == '__main__':
    unittest.main()
