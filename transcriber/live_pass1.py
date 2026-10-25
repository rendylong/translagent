# -*- coding: utf-8 -*-
"""Pass1 流式识别引擎:sherpa-onnx 流式 transducer 的工作线程封装。

live-captions 的 Pass1:Task 4 的 WebSocket 会话只调 feed() 喂 float32 PCM,
从回调收 partial/final;finals() 取累积结果。recognizer 参数与时间戳口径
(t0_ms=utterance 起点、t1_ms=该 utterance 结束时已喂入的音频位置,均取音频
时间轴)沿用 Task 1 bench(models/bench/bench_streaming_asr.py)口径;endpoint
三规则已改用 sherpa-onnx 官方默认 2.4/1.2/20(spec 2026-10-24 §4.1),不再
沿用 bench 的 0.6/1.0——bench 值把 rule3 误当最小句长门槛,实为句长上限(见
下方常量注释),是断句碎片化的根因。

模型文件名按目录内容解析(spec 2026-10-24 §4.2):不同来源的流式模型 epoch 命名
不同(中英双语是 *-epoch-99-avg-1,多语言日文是别的编号),glob 匹配
encoder/decoder/joiner-*.onnx(排除 int8),无匹配回落硬编码名——兼容既有目录。

注意:sherpa-onnx 1.13.8 的 OnlineRecognizer 没有 is_ready_retrieve,解码取词的
时机按 bench 实测路径:is_ready → decode_stream → get_result_as_json_string。
"""
import glob
import json
import os
import queue
import sys
import threading
import traceback

import numpy as np
from sherpa_onnx.online_recognizer import OnlineRecognizer

# sherpa-onnx 2.0.0 EndpointConfig 三规则口径(官方文档,标定量,后续按真实会议
# 语速可微调,spec 2026-10-24 §4.1):
#   rule1 = 尾部静音时长超过即断句,与有没有说过话无关;
#   rule2 = 解码出过非静音内容后,尾部静音时长超过即断句;
#   rule3 = utterance 时长超过即断句,不管静音——是句长上限,不是 icefall 旧
#           语义的「rule2 生效所需最小句长」。
# 实测依据(2026-10-24,say -v Tingting 合成 15.7s 连续中文):RULE3=1.0 时被
# 切成 13 条每秒碎片;改官方默认 2.4/1.2/20 后同为 1 条整句。
RULE1 = 2.4   # rule1:尾部静音超过即断句(官方默认;与说话与否无关)
RULE2 = 1.2   # rule2:说过话后尾部静音超过即断句(官方默认;原 0.6 偏碎)
RULE3 = 20.0  # rule3:句长上限,超过强制断句(官方默认;原 1.0 = 每秒强切,断句碎片化根因)
NUM_THREADS = 2


def _model_file(model_dir, kind, fallback):
    """目录里找 <kind>-*.onnx,字典序首个;优先非 int8,目录里只有 int8 时取 int8
    (多语言日文发布包 encoder/joiner 仅 int8);完全无匹配回落硬编码名。"""
    hits = sorted(glob.glob(os.path.join(model_dir, kind + '-*.onnx')))
    full = [p for p in hits if not p.endswith('.int8.onnx')]
    pick = (full or hits)
    return pick[0] if pick else os.path.join(model_dir, fallback)


_EOF = object()   # 停止哨兵(区别于任何音频数据)


class Pass1Engine:
    """单工作线程驱动一个流式 recognizer。

    - feed():非阻塞,PCM 追加进内部队列即返回(daemon 工作线程消费)。
    - on_partial(text):工作线程触发,当前 utterance 的渐增文本,仅在文本变化时发出。
    - on_final(text, t0_ms, t1_ms):工作线程触发,endpoint 切分或 stop 冲刷的成句结果。
    - finals():[{text, t0_ms, t1_ms}] 快照,线程安全。
    - stop():幂等;排空队列并冲刷尾部残留文本后回收线程。
    """

    def __init__(self, model_dir, on_partial, on_final, sample_rate=16000):
        self.sample_rate = int(sample_rate)
        self.on_partial = on_partial
        self.on_final = on_final
        self._rec = OnlineRecognizer.from_transducer(
            tokens=os.path.join(model_dir, 'tokens.txt'),
            encoder=_model_file(model_dir, 'encoder', 'encoder-epoch-99-avg-1.onnx'),
            decoder=_model_file(model_dir, 'decoder', 'decoder-epoch-99-avg-1.onnx'),
            joiner=_model_file(model_dir, 'joiner', 'joiner-epoch-99-avg-1.onnx'),
            num_threads=NUM_THREADS,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=RULE1,
            rule2_min_trailing_silence=RULE2,
            rule3_min_utterance_length=RULE3,
            decoding_method='greedy_search')
        self._stream = self._rec.create_stream()
        self._q = queue.Queue()
        self._finals = []
        self._lock = threading.Lock()        # 守 _finals
        self._stop_lock = threading.Lock()   # 守 stop 幂等
        self._fed = 0                # 已喂入流的样本数(音频时间轴推进器)
        self._utt_start_ms = 0.0     # 当前 utterance 起点(bench 口径的 t0)
        self._last_partial = ''      # 当前 utterance 已发出的最近 partial(去重)
        self._stopped = False
        self._worker = threading.Thread(target=self._run, name='pass1-worker', daemon=True)
        self._worker.start()

    # ---------- 对外接口 ----------

    def feed(self, pcm):
        """追加一段 float32 PCM(任意长度),入队即返回,不阻塞。stop 后调用抛错。"""
        if self._stopped:
            raise RuntimeError('Pass1Engine already stopped')
        self._q.put(pcm)

    def finals(self):
        """累积 final 快照(浅拷贝列表),线程安全。"""
        with self._lock:
            return list(self._finals)

    def stop(self):
        """停止:等队列排空 → 冲刷尾部残留 → 回收工作线程。幂等,可重复调用。"""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self._q.put(_EOF)
            self._worker.join()

    # ---------- 工作线程 ----------

    def _run(self):
        while True:
            item = self._q.get()
            if item is _EOF:
                break
            self._accept(item)
        self._flush_tail()

    def _accept(self, pcm):
        seg = np.ascontiguousarray(pcm, dtype=np.float32)
        self._stream.accept_waveform(self.sample_rate, seg)
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        self._fed += len(seg)
        text = self._result_text()
        if text and text != self._last_partial:
            self._last_partial = text
            self._safe(self.on_partial, text)
        if self._rec.is_endpoint(self._stream):
            self._record_final(text)
            self._rec.reset(self._stream)
            self._utt_start_ms = self._fed * 1000.0 / self.sample_rate
            self._last_partial = ''

    def _flush_tail(self):
        """尾部冲刷(bench 同款):input_finished → 解码完 → 残留文本作最后一个 final。"""
        self._stream.input_finished()
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        self._record_final(self._result_text())

    # ---------- 内部工具 ----------

    def _result_text(self):
        r = json.loads(self._rec.get_result_as_json_string(self._stream))
        return r.get('text', '')

    def _record_final(self, text):
        if not text.strip():
            return
        t0_ms = round(self._utt_start_ms)
        t1_ms = round(self._fed * 1000.0 / self.sample_rate)
        with self._lock:
            self._finals.append({'text': text, 't0_ms': t0_ms, 't1_ms': t1_ms})
        self._safe(self.on_final, text, t0_ms, t1_ms)

    @staticmethod
    def _safe(fn, *args):
        try:
            fn(*args)
        except Exception:
            traceback.print_exc(file=sys.stderr)   # 回调异常不杀工作线程
