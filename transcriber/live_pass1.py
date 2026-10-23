# -*- coding: utf-8 -*-
"""Pass1 流式识别引擎:sherpa-onnx 流式 transducer 的工作线程封装。

live-captions 的 Pass1:Task 4 的 WebSocket 会话只调 feed() 喂 float32 PCM,
从回调收 partial/final;finals() 取累积结果。recognizer 参数、endpoint 规则与
时间戳口径(t0_ms=utterance 起点、t1_ms=该 utterance 结束时已喂入的音频位置,
均取音频时间轴)与 Task 1 bench(models/bench/bench_streaming_asr.py)完全一致,
该脚本已在本目录实测通过。

注意:sherpa-onnx 1.13.8 的 OnlineRecognizer 没有 is_ready_retrieve,解码取词的
时机按 bench 实测路径:is_ready → decode_stream → get_result_as_json_string。
"""
import json
import queue
import sys
import threading
import traceback

import numpy as np
from sherpa_onnx.online_recognizer import OnlineRecognizer

RULE1 = 2.4   # rule1_min_trailing_silence(spec §6.1,与 Task 1 一致)
RULE2 = 0.6   # rule2_min_trailing_silence
RULE3 = 1.0   # rule3_min_utterance_length
NUM_THREADS = 2

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
            tokens=f'{model_dir}/tokens.txt',
            encoder=f'{model_dir}/encoder-epoch-99-avg-1.onnx',
            decoder=f'{model_dir}/decoder-epoch-99-avg-1.onnx',
            joiner=f'{model_dir}/joiner-epoch-99-avg-1.onnx',
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
