# -*- coding: utf-8 -*-
"""titanet 在线聚类说话人分离(live 实时字幕,Task 6)。

对 Pass2 的 whisper 段落音频逐段提 NeMo TitaNet-large 声纹(模型与离线管线同款,
app.EMB_MODEL),在线聚成「说话人N」:

  - OnlineSpeakerCluster.assign(seg_pcm) → int 簇号:段音频按 CHUNK_S(8s)分块逐块
    提单位化 embedding → 与各簇心比余弦;最近簇心 > θ → 并入该簇(簇心=成员均值后
    单位化);否则开新簇;返回值=段内多数块的簇号。按喂入顺序单线程使用
    (Pass2Runner.run_window 串行调用)。分块依据:whisper 段落可长达 20-30s 且跨
    说话人轮次(双人寒暄区实测整窗一段),整段一个声纹必然混两人;Task 1 实测
    ~1.2s final 句粒度声纹不稳、3.6-6s 锚点粒度分离良好,8s 块取锚点粒度。
  - 不足 min_turn_s 的短段直接归上一簇(短段声纹不稳,不提 embedding、不动簇心);
    首段无处可归,即使短也开簇 0(提声纹作种子)。段内 <min_turn_s 的尾块丢弃。
  - 前 WARMUP_N(=3)块内允许簇重并:任两簇心余弦 > θ 即合并(开局单块簇心未稳,
    防误分裂;之后不再重并——真人簇心可能天然相近(zh 实测两说话人簇心余弦 0.64),
    晚期重并会把真说话人错误合并,故设上限)。

θ 语义与 Task 1 标定同向:同人块间余弦应 > θ、异人 < θ。DEFAULT_THETA=0.40 的实测
依据(2026-10-23,详见 task-6-report):whisper 段落粒度比 Task 1 标定的 final 句
粒度(~1.2s)声纹更稳,zh 实测同人 8s 块间余弦 p50≈0.40-0.50、Task 1 建议值
(zh 0.167 / en 0.230)在段落粒度会过度合并(anchors 交替测试全并 1 簇);
0.40 是 anchors 双场景交替 6 段测试(en 前三窗 / zh 干净窗,均 6/6 且带余量)与
zh job 前 90s 真 whisper 段落时序回放(8s 分块,n_speakers==2、事件双说话人)的
联合可行点。
"""
import threading
from collections import Counter

import numpy as np

from app import EMB_MODEL

SAMPLE_RATE = 16000
DEFAULT_THETA = 0.40   # 同人余弦阈值:>θ 并入,≤θ 开新簇(实测依据见模块 docstring)
CHUNK_S = 8.0          # 长段分块粒度(秒):近似 anchors 锚点窗,过短不稳、整段混人
WARMUP_N = 3           # 前 N 块内允许簇重并


class OnlineSpeakerCluster:
    """whisper 段落声纹在线聚类。构造廉价(extractor 懒加载),assign 逐段调用。

    theta:同人段间余弦阈值(Task 1 同向语义);
    min_turn_s:不足此时长(秒)的段归上一簇,不提声纹;
    model_path:声纹 onnx,默认与离线管线同款(app.EMB_MODEL)。
    """

    def __init__(self, theta=DEFAULT_THETA, min_turn_s=1.0, model_path=EMB_MODEL):
        self.theta = float(theta)
        self.min_turn_s = float(min_turn_s)
        self.model_path = model_path
        self._ext = None                     # 懒加载:首次 assign 时建
        self._ext_lock = threading.Lock()    # 仅护懒加载;assign 本身约定单线程
        self._sums = []                      # 各簇成员 embedding 和(未归一)
        self._counts = []                    # 各簇成员数
        self._last = None                    # 上一簇号(短段归宿)

    @property
    def n_speakers(self):
        return len(self._sums)

    # ---------- 内部 ----------

    def _extractor(self):
        if self._ext is None:
            with self._ext_lock:
                if self._ext is None:
                    import sherpa_onnx
                    self._ext = sherpa_onnx.SpeakerEmbeddingExtractor(
                        sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                            model=self.model_path, num_threads=4))
        return self._ext

    def _embedding(self, pcm):
        """段音频 → 单位化 titanet embedding(提取方式同 bench/Task 1 标定)。"""
        s = self._extractor().create_stream()
        s.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(pcm, dtype=np.float32))
        s.input_finished()
        e = np.array(self._extractor().compute(s), dtype=np.float32)
        n = np.linalg.norm(e)
        return e / n if n > 0 else e

    def _centroids(self):
        return [s / max(np.linalg.norm(s), 1e-12) for s in self._sums]

    def _del_cluster(self, j, into):
        """簇 j 并入簇 into(成员求和),j 之后(含当前返回值 k)的下标移位。"""
        self._sums[into] = self._sums[into] + self._sums[j]
        self._counts[into] += self._counts[j]
        del self._sums[j], self._counts[j]

    def _reindex(self, k, j, into):
        if k == j:
            return into
        return k - 1 if k > j else k

    def _try_merge(self, k):
        """warmup 期:簇心余弦 > θ 的两簇合并(小号保留),直至无对可并。
        返回重并后原簇号 k 对应的现行簇号。"""
        while len(self._sums) > 1:
            cents = self._centroids()
            best, bi, bj = self.theta, -1, -1
            for i in range(len(cents)):
                for j in range(i + 1, len(cents)):
                    sim = float(cents[i] @ cents[j])
                    if sim > best:
                        best, bi, bj = sim, i, j
            if bi < 0:
                return k
            self._del_cluster(bj, bi)
            k = self._reindex(k, bj, bi)
            if self._last is not None:
                self._last = self._reindex(self._last, bj, bi)
        return k

    # ---------- 对外 ----------

    def assign(self, seg_pcm):
        """段音频 → 簇号(int)。段按 CHUNK_S 分块逐块聚类,返回段内多数块的簇号;
        不足 min_turn_s(或空)的段归上一簇、不提声纹;首段(即使短)开簇 0。"""
        pcm = np.asarray(seg_pcm, dtype=np.float32)
        n_min = self.min_turn_s * SAMPLE_RATE
        if len(pcm) < n_min and self._last is not None:
            return self._last
        step = int(CHUNK_S * SAMPLE_RATE)
        chunks = [pcm[i:i + step] for i in range(0, len(pcm), step)]
        chunks = [c for c in chunks if len(c) >= n_min] or [pcm]  # 短尾块丢弃;整段短则整段作种子
        preds = [self._assign_chunk(c) for c in chunks]
        return Counter(preds).most_common(1)[0][0]

    def _assign_chunk(self, chunk):
        """单块(≥min_turn_s 或首段种子)→ 簇号:最近簇心 > θ 并入,否则开新簇。"""
        emb = self._embedding(chunk)
        if not self._sums:
            self._sums.append(emb.copy())
            self._counts.append(1)
            k = 0
        else:
            sims = np.stack(self._centroids()) @ emb
            k = int(np.argmax(sims))
            if sims[k] > self.theta:
                self._sums[k] = self._sums[k] + emb
                self._counts[k] += 1
            else:
                self._sums.append(emb.copy())
                self._counts.append(1)
                k = len(self._sums) - 1
        if sum(self._counts) <= WARMUP_N:
            k = self._try_merge(k)
        self._last = k
        return k
