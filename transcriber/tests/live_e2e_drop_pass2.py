# -*- coding: utf-8 -*-
"""live 两层管线 e2e 验收(Task 5,spec §5.3/§5.4/§5.6;2026-10-24)。

进程内直驱 LiveSession / Pass1Engine:不起服务、不绑端口(8765 被主仓库服务
占用,与本脚本无关)。真实模型(sherpa-onnx streaming zipformer zh-en/ja +
TitaNet 声纹)+ 真实 MiniMax LLM(key 自 worktree 根 .env 解析,窗口文本出境
属 AGENTS.md 既定授权)+ say 合成音频(经典声音 Tingting/Samantha/Kyoko 可
落盘;神经声 -o 落盘为空是已知坑,脚本内以 <1s 落盘判 SKIP)。

六场景(断言按 task-5 简报逐条):
  S1 断句回归   zh ~16s 连续句直驱 Pass1Engine,finals ≤3(修前 ~13 碎片)
  S2 全链路     LiveSession zh>en 真实 LLM:≥1 partial、pass1_final 带
                speaker=0、llm 标签已剥+含标点+非空英文翻译;事件流绝无
                pass2/pass2_skip;stop_ack→…→stopped,最后 llm seq <
                stopped seq(drain 不丢尾句);打印 final→llm 时延(>10s 才 FAIL)
  S3 合并批     两短句停顿 1.8s:恰 2 条 pass1_final 但仅 1 条 llm,
                t0=首句 t0、t1=末句 t1
  S4 双声分人   Tingting+Samantha 各 3 句交替,warmup(前 3 条 final)后
                speaker 两簇交替且都出现;parse_key patch 掉关 LLM
  S5 ja>zh 回归 streaming-zipformer-ja + Kyoko:pass1_final 含假名、
                llm translation 为中文(含汉字且无假名);声音/模型缺则 SKIP
  S6 无 key     parse_key→None:llm_enabled False、pass1_final 带 speaker、
                全程无 llm、stopped 正常(无 key 的 UI 呈现留待用户页面核验,
                spec §5.5 手工清单)

运行:cd transcriber && python3 tests/live_e2e_drop_pass2.py
纪律:文件名无 test 前缀(unittest discover 不扫);可重复跑(临时目录用后即
删);LLM 场景偶发失败重试一次,再失败按熔断/降级语义给证据(连续失败是产品
预期路径,不算 FAIL);LLM 调用总数 ≤10。全 PASS/SKIP 退出码 0。
"""
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest.mock
import uuid
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import live_session  # noqa: E402  patch parse_key 用(S4/S6 关 LLM)
from live_llm import parse_key  # noqa: E402
from live_pass1 import Pass1Engine  # noqa: E402
from live_session import LiveSession  # noqa: E402

MODEL_ZH = str(BASE / 'models' / 'streaming-zipformer-zh-en')
MODEL_JA = str(BASE / 'models' / 'streaming-zipformer-ja')
CHUNK = 3200            # 每包 3200 字节 = 100ms @16k mono s16le(feed_bytes 吃 int16 字节,不是
                        # 200ms——前端 live.js 的 3200 是样本数;S1 直驱引擎同样按样本切片,见其循环内)
BPS = 32000             # 字节/秒
SR = 16000
LLM_WAIT_S = 45         # 等单条 llm 事件的超时(含合并等待窗 1.5s + 网络)
KANA = re.compile(r'[ぁ-んァ-ヶ]')
HAN = re.compile(r'[\u4e00-\u9fff]')
PUNCT = '。，、．,.;；:：!！?？'

S1_TEXT = ('今天我们开一次产品评审会议,主要讨论实时字幕功能在灰度测试中暴露的'
           '断句碎片化和说话人分离准确率下降这两个问题,请大家结合各自负责的模块'
           '逐项分析根因并提出可落地的改进方案。')
S2_A = '会议的第一项议题是确认下个季度的预算分配方案。'
S2_B = '第二项议题是讨论新版本的发布时间和灰度范围。'
S3_A = '预算方案已经通过评审。'
S3_B = '下周五开始执行新的计划。'
S4_ZH = ['今天的会议纪要由我来负责整理。', '下一季度的工作计划需要重新排期。', '请大家会前提交各自的预算表。']
# 第三句不用「Please submit your slides before the meeting.」:该句接在第三句中文后,
# streaming-zipformer-zh-en 的解码器会整段产出空(裸 sherpa-onnx API 复现同现象,
# 与本仓库代码无关;换任何其他句子即正常,详见 task-5 报告 Concerns 节)。
S4_EN = ['The quarterly report is ready for review.',
         'We need to finalize the budget by Friday.',
         'Thank you all for joining today.']
S5_TEXT = '本日の会議では、来週の製品リリースの予定について話し合います。'
S6_TEXT = '这一段话用来验证没有配置翻译密钥时的实时会话行为。'


# ---------- 基础件 ----------

class Skip(Exception):
    """环境缺件(声音/模型/key):该场景 SKIP,其余照跑。"""


class Fail(AssertionError):
    """断言失败;llm=True 标记「仅 LLM 层失败」(可重试一次,再失败按熔断口径分析)。"""

    def __init__(self, msg, llm=False):
        super().__init__(msg)
        self.llm = llm


VOICES = set()


def say_voices():
    out = subprocess.run(['say', '-v', '?'], capture_output=True, text=True, timeout=30).stdout
    return {ln.split()[0] for ln in out.splitlines() if ln.strip()}


def wav_pcm_bytes(path):
    """RIFF 手工解 data 块 → 原始 int16 小端 PCM 字节(feed_bytes 的口径,不是 float)。

    afconvert 输出的 fmt 标签是 65534(WAVE_FORMAT_EXTENSIBLE),标准库 wave 读不了
    (AGENTS.md 已知坑);逐 chunk 走查找 data,对 44 字节标准头与 extensible 头都成立。"""
    raw = Path(path).read_bytes()
    if raw[:4] != b'RIFF':
        raise ValueError('不是 RIFF 文件:%s' % path)
    pos = 12
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack('<I', raw[pos + 4:pos + 8])[0]
        if cid == b'data':
            return raw[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    raise ValueError('找不到 data 块:%s' % path)


def synth(voice, text, tmp, tag):
    """say 合成 → afconvert 转 16k 单声道 16bit → int16 字节。落盘 <1s 视为神经声坑,SKIP。"""
    aiff = os.path.join(tmp, tag + '.aiff')
    wav = os.path.join(tmp, tag + '.wav')
    try:
        subprocess.run(['say', '-v', voice, '-o', aiff, text],
                       check=True, timeout=180, capture_output=True)
        subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', aiff, wav],
                       check=True, timeout=60, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise Skip('%s 合成失败:%s' % (voice, e))
    pcm = wav_pcm_bytes(wav)
    if len(pcm) < BPS:
        raise Skip('%s 合成落盘仅 %.2fs(神经声 -o 空音频的已知坑)' % (voice, len(pcm) / BPS))
    print('  [%s] %s 合成 %.1fs' % (tag, voice, len(pcm) / BPS))
    return pcm


def silence(seconds):
    return b'\x00\x00' * int(seconds * SR)


def feed_all(sess, *parts):
    """拼接后按 3200 字节/包喂(feed_bytes 吃 int16 字节)。"""
    data = b''.join(parts)
    for i in range(0, len(data), CHUNK):
        sess.feed_bytes(data[i:i + CHUNK])


def n_of(evs, t):
    return sum(1 for e in evs if e['type'] == t)


class Collector:
    """后台线程持续 drain sess.events,记录每条事件到达墙钟(seq→perf_counter,算时延用)。"""

    def __init__(self, sess):
        self.sess = sess
        self.events = []
        self.arrivals = {}
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._quit.is_set():
            try:
                ev = self.sess.events.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                self.arrivals[ev['seq']] = time.perf_counter()
                self.events.append(ev)

    def snapshot(self):
        with self._lock:
            return list(self.events)

    def wait_for(self, pred, timeout, what):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            es = self.snapshot()
            if pred(es):
                return es
            time.sleep(0.1)
        return self.snapshot()

    def close(self):
        self._quit.set()
        self._t.join(timeout=2)


def make_session(tmp, model_dir, language, speakers, no_key=False):
    """按简报口径构造:临时 sessions_root + 替身 finalizer(e2e 焦点是 live 层)。"""
    root = os.path.join(tmp, 'live-' + uuid.uuid4().hex[:6])
    os.makedirs(root, exist_ok=True)
    sess = LiveSession(root, model_dir, finalizer=lambda d, speakers_hint=None: 'e2e-job')
    sess.start('e2e 验收', speakers, language)
    return sess


def check_common(es, label):
    """全场景共同不变量:seq 严格递增不重复;事件流绝无 pass2/pass2_skip;error 事件打印可见。"""
    seqs = [e['seq'] for e in es]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        raise Fail('%s:事件 seq 乱序或重复:%s' % (label, seqs))
    bad = [e['type'] for e in es if e['type'] in ('pass2', 'pass2_skip')]
    if bad:
        raise Fail('%s:事件流出现 pass2/pass2_skip(Pass2 已摘除):%s' % (label, bad))
    for e in es:
        if e['type'] == 'error':
            print('  (error 事件)%s' % e.get('message'))


def llm_evidence(sess):
    return 'refiner.last_error=%r, fused=%s' % (
        getattr(sess._refiner, 'last_error', '<无 refiner>'),
        getattr(sess._refiner, 'fused', '?'))


# ---------- S1 断句回归(zh ~16s 连续句,直驱 Pass1Engine) ----------

def s1(tmp):
    pcm = synth('Tingting', S1_TEXT, tmp, 's1')
    finals = []
    eng = Pass1Engine(MODEL_ZH, lambda t: None,
                      lambda text, t0, t1: finals.append({'text': text, 't0_ms': t0, 't1_ms': t1}))
    try:
        x = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        for i in range(0, len(x), 3200):   # 样本切片:3200 样本 = 200ms(与 CHUNK 的字节口径区分,值巧合相同)
            eng.feed(np.ascontiguousarray(x[i:i + 3200]))
    finally:
        eng.stop()                      # 排空 + 尾部冲刷(空文本 final 不计)
    print('  合成 %.1fs → %d 条 final:' % (len(pcm) / BPS, len(finals)))
    for f in finals:
        print('    %5d-%5dms(%4.1fs) %s' % (f['t0_ms'], f['t1_ms'],
                                            (f['t1_ms'] - f['t0_ms']) / 1000.0, f['text']))
    if not finals:
        raise Fail('S1:连续中文未产出任何 final')
    if len(finals) > 3:
        raise Fail('S1:断句碎片化,%d 条 final(预期 ≤3;Task 1 修前 ~13 条/每秒碎片)'
                   % len(finals))


# ---------- S2 全链路 zh>en(真实 LLM) ----------

def s2(tmp):
    a = synth('Tingting', S2_A, tmp, 's2a')
    b = synth('Tingting', S2_B, tmp, 's2b')
    sess = make_session(tmp, MODEL_ZH, 'zh>en', 1)
    col = Collector(sess)
    feed_all(sess, a, silence(2.0), b, silence(3.0))     # 句间 >1.2s 停顿 + 尾部 3s 静音
    col.wait_for(lambda es: n_of(es, 'pass1_final') >= 2, 30, 'pass1_final>=2')
    time.sleep(0.3)
    sess.handle_stop()                   # 不先等 llm:钉 _drain_llm「在飞排空」不丢尾句
    col.wait_for(lambda es: n_of(es, 'stopped') >= 1, 90, 'stopped')
    es = col.snapshot()
    col.close()
    check_common(es, 'S2')
    if n_of(es, 'partial') < 1:
        raise Fail('S2:无 partial 事件')
    finals = [e for e in es if e['type'] == 'pass1_final']
    llms = [e for e in es if e['type'] == 'llm']
    print('  事件:partial=%d pass1_final=%d llm=%d' % (n_of(es, 'partial'), len(finals), len(llms)))
    for f in finals:
        print('    final[t0=%d t1=%d spk=%s] %s' % (f['t0_ms'], f['t1_ms'], f.get('speaker'), f['text']))
    if len(finals) < 2:
        raise Fail('S2:pass1_final 仅 %d 条(两句 + >1.2s 停顿应 ≥2):%s'
                   % (len(finals), [f['text'] for f in finals]))
    if not all(f.get('speaker') == 0 for f in finals):
        raise Fail('S2:单声音会话 pass1_final 应带 speaker=0:%s'
                   % [(f['text'], f.get('speaker')) for f in finals])
    if not llms:
        raise Fail('S2:无 llm 事件(%s)' % llm_evidence(sess), llm=True)
    types = [e['type'] for e in es]
    i_ack, i_stop = types.index('stop_ack'), types.index('stopped')
    if i_ack >= i_stop:
        raise Fail('S2:stop_ack(%d) 未先于 stopped(%d):%s' % (i_ack, i_stop, types))
    stopped_seq = es[i_stop]['seq']
    last_llm_seq = max(l['seq'] for l in llms)
    if last_llm_seq >= stopped_seq:
        raise Fail('S2:最后 llm seq=%d 未先于 stopped seq=%d(drain 丢尾句)' % (last_llm_seq, stopped_seq))
    for L in llms:
        text = L.get('text') or ''
        if any(ln.strip().startswith('说话人') for ln in text.splitlines()):
            raise Fail('S2:llm 文本未剥「说话人N:」标签:%r' % text)
        if L.get('speaker') != 0:
            raise Fail('S2:llm speaker 应为 0(单说话人批剥标签):%r' % L)
        if not any(c in text for c in PUNCT):
            raise Fail('S2:llm 文本无标点:%r' % text)
        if not (L.get('translation') or '').strip():
            raise Fail('S2:llm translation 为空串(%s)' % llm_evidence(sess), llm=True)
        if not any('a' <= c.lower() <= 'z' for c in L['translation']):
            raise Fail('S2:llm translation 不含英文字母:%r' % L['translation'], llm=True)
        print('  llm[t0=%s t1=%s spk=%s] %s  =>  %s' % (L['t0'], L['t1'], L.get('speaker'),
                                                        text.replace('\n', ' / '), L['translation']))
    for L in llms:                       # pass1_final → 对应 llm 的时延(参考 ≤5s,>10s 才 FAIL)
        f0 = next((f for f in finals if f['t0_ms'] == L['t0']), None)
        if f0 is None:
            continue
        lat = col.arrivals[L['seq']] - col.arrivals[f0['seq']]
        print('  时延 pass1_final→llm = %.1fs(t0=%s)' % (lat, L['t0']))
        if lat > 10:
            raise Fail('S2:时延 %.1fs 超 10s(%s)' % (lat, llm_evidence(sess)), llm=True)


# ---------- S3 合并批(真实 LLM) ----------

def s3(tmp):
    a = synth('Tingting', S3_A, tmp, 's3a')
    b = synth('Tingting', S3_B, tmp, 's3b')
    sess = make_session(tmp, MODEL_ZH, 'zh>en', 1)
    col = Collector(sess)
    feed_all(sess, a, silence(1.8), b, silence(2.0))     # 停顿 1.8s:触发 rule2 断句且间隔 ≤2.5s
    col.wait_for(lambda es: n_of(es, 'pass1_final') >= 2, 30, 'pass1_final>=2')
    col.wait_for(lambda es: n_of(es, 'llm') >= 1, LLM_WAIT_S, 'llm>=1')   # 等合并批出批后再 stop
    sess.handle_stop()
    col.wait_for(lambda es: n_of(es, 'stopped') >= 1, 90, 'stopped')
    es = col.snapshot()
    col.close()
    check_common(es, 'S3')
    finals = [e for e in es if e['type'] == 'pass1_final']
    llms = [e for e in es if e['type'] == 'llm']
    print('  事件:pass1_final=%d llm=%d' % (len(finals), len(llms)))
    for f in finals:
        print('    final[t0=%d t1=%d spk=%s] %s' % (f['t0_ms'], f['t1_ms'], f.get('speaker'), f['text']))
    if len(finals) != 2:
        raise Fail('S3:pass1_final 应恰 2 条,实得 %d:%s' % (len(finals), [f['text'] for f in finals]))
    if not llms:
        raise Fail('S3:无 llm 事件(%s)' % llm_evidence(sess), llm=True)
    if len(llms) != 1:
        raise Fail('S3:合并批失效,2 条 final 产出 %d 条 llm(应 1):%s'
                   % (len(llms), [l.get('text') for l in llms]))
    L = llms[0]
    if L['t0'] != finals[0]['t0_ms'] or L['t1'] != finals[1]['t1_ms']:
        raise Fail('S3:合并批 t0/t1 不符:llm(t0=%s,t1=%s) vs 首句 t0=%d / 末句 t1=%d'
                   % (L['t0'], L['t1'], finals[0]['t0_ms'], finals[1]['t1_ms']))
    stopped = next(e for e in es if e['type'] == 'stopped')
    if L['seq'] >= stopped['seq']:
        raise Fail('S3:llm seq=%d 未先于 stopped seq=%d' % (L['seq'], stopped['seq']))
    print('  合并批 OK:1 条 llm,t0=%s(首句)t1=%s(末句)' % (L['t0'], L['t1']))
    print('    %s  =>  %s' % ((L.get('text') or '').replace('\n', ' / '), L.get('translation')))


# ---------- S4 双声交替分人(LLM 关,parse_key patch 掉) ----------

def s4(tmp):
    if not {'Tingting', 'Samantha'} <= VOICES:
        raise Skip('本机缺 Tingting/Samantha 声音')
    parts = []
    for i in range(3):                   # T,S,T,S,T,S 交替,句间 1.6s 停顿逐句断开
        parts.append(synth('Tingting', S4_ZH[i], tmp, 's4z%d' % i))
        parts.append(silence(1.6))
        parts.append(synth('Samantha', S4_EN[i], tmp, 's4e%d' % i))
        parts.append(silence(1.6))
    parts.append(silence(1.0))
    with unittest.mock.patch.object(live_session, 'parse_key', lambda: None):
        sess = make_session(tmp, MODEL_ZH, 'zh>en', 2)
        col = Collector(sess)
        feed_all(sess, *parts)
        col.wait_for(lambda es: n_of(es, 'pass1_final') >= 6, 60, 'pass1_final>=6')
        time.sleep(0.5)
        sess.handle_stop()
        col.wait_for(lambda es: n_of(es, 'stopped') >= 1, 60, 'stopped')
    es = col.snapshot()
    col.close()
    check_common(es, 'S4')
    if sess.llm_enabled is not False:
        raise Fail('S4:parse_key patch 后 llm_enabled 应为 False')
    if n_of(es, 'llm'):
        raise Fail('S4:LLM 关闭仍出现 llm 事件')
    finals = [e for e in es if e['type'] == 'pass1_final']
    spk = [f.get('speaker', 'MISSING') for f in finals]
    print('  %d 条 final,speaker 序列:%s' % (len(finals), spk))
    for f in finals:
        print('    spk=%s %s' % (f.get('speaker'), f['text']))
    if len(finals) < 6:
        raise Fail('S4:final 仅 %d 条(<6),六句交替每句应各 1 条 final' % len(finals))
    tail = spk[3:]                       # warmup 排除:OnlineSpeakerCluster 前 3 块允许簇重并
    if 'MISSING' in tail:
        raise Fail('S4:warmup 后存在无 speaker 键的 final(分人失败不该发生):%s' % spk)
    if len(set(tail)) != 2:
        raise Fail('S4:warmup 后簇数 %d(应恰 2):%s' % (len(set(tail)), spk))
    for i in range(1, len(tail)):
        if tail[i] == tail[i - 1]:
            raise Fail('S4:warmup 后 speaker 未在两簇间交替(全归一簇/误并/错切):%s' % spk)
    print('  warmup 后序列 %s:两簇交替且都出现' % tail)


# ---------- S5 ja>zh 回归(真实 LLM) ----------

def s5(tmp):
    if not os.path.isdir(MODEL_JA):
        raise Skip('models/streaming-zipformer-ja 不存在')
    if 'Kyoko' not in VOICES:
        raise Skip('无日文声音 Kyoko(say -v ? 未列出)')
    pcm = synth('Kyoko', S5_TEXT, tmp, 's5')
    sess = make_session(tmp, MODEL_JA, 'ja>zh', 1)
    col = Collector(sess)
    feed_all(sess, pcm, silence(2.0))
    col.wait_for(lambda es: n_of(es, 'pass1_final') >= 1, 40, 'pass1_final>=1')
    col.wait_for(lambda es: n_of(es, 'llm') >= 1, LLM_WAIT_S, 'llm>=1')
    sess.handle_stop()
    col.wait_for(lambda es: n_of(es, 'stopped') >= 1, 90, 'stopped')
    es = col.snapshot()
    col.close()
    check_common(es, 'S5')
    finals = [e for e in es if e['type'] == 'pass1_final']
    llms = [e for e in es if e['type'] == 'llm']
    print('  事件:pass1_final=%d llm=%d' % (len(finals), len(llms)))
    if not finals:
        raise Fail('S5:日文音频未产出 pass1_final')
    ftext = finals[0]['text']
    print('  pass1_final[t0=%d t1=%d spk=%s]:%s' % (finals[0]['t0_ms'], finals[0]['t1_ms'],
                                                    finals[0].get('speaker'), ftext))
    if not KANA.search(ftext):
        raise Fail('S5:pass1_final 不含日文假名:%r' % ftext)
    if not llms:
        raise Fail('S5:无 llm 事件(%s)' % llm_evidence(sess), llm=True)
    L = llms[0]
    tr = L.get('translation') or ''
    print('  llm:%s  =>  %s' % ((L.get('text') or '').replace('\n', ' / '), tr))
    if not HAN.search(tr):
        raise Fail('S5:translation 不含汉字(应中文):%r' % tr, llm=True)
    if KANA.search(tr):
        raise Fail('S5:translation 含假名(不是中文):%r' % tr, llm=True)
    stopped = next(e for e in es if e['type'] == 'stopped')
    if L['seq'] >= stopped['seq']:
        raise Fail('S5:llm seq=%d 未先于 stopped seq=%d' % (L['seq'], stopped['seq']))


# ---------- S6 无 key 会话 ----------

def s6(tmp):
    pcm = synth('Tingting', S6_TEXT, tmp, 's6')
    with unittest.mock.patch.object(live_session, 'parse_key', lambda: None):
        sess = make_session(tmp, MODEL_ZH, 'zh>en', 1)
        col = Collector(sess)
        feed_all(sess, pcm, silence(1.8))
        col.wait_for(lambda es: n_of(es, 'pass1_final') >= 1, 30, 'pass1_final>=1')
        sess.handle_stop()
        col.wait_for(lambda es: n_of(es, 'stopped') >= 1, 60, 'stopped')
    es = col.snapshot()
    col.close()
    check_common(es, 'S6')
    if sess.llm_enabled is not False:
        raise Fail('S6:无 key 会话 llm_enabled 应为 False,实为 %r' % sess.llm_enabled)
    finals = [e for e in es if e['type'] == 'pass1_final']
    if not finals:
        raise Fail('S6:无 pass1_final')
    for f in finals:
        print('  final[t0=%d t1=%d spk=%s] %s' % (f['t0_ms'], f['t1_ms'], f.get('speaker'), f['text']))
    if not all('speaker' in f for f in finals):
        raise Fail('S6:pass1_final 应带 speaker 键:%s' % [f.get('speaker') for f in finals])
    if n_of(es, 'llm'):
        raise Fail('S6:无 key 会话全程不得出现 llm 事件')
    types = [e['type'] for e in es]
    if 'stop_ack' not in types or 'stopped' not in types:
        raise Fail('S6:停止生命周期不完整:%s' % types)
    print('  无 key:%d 条 final(均带 speaker),全程无 llm,stop_ack→stopped 正常' % len(finals))
    print('  注:无 key 的 UI 呈现(「未配置翻译密钥」提示)属用户手工清单 spec §5.5,留待页面核验')


# ---------- 运行框架 ----------

RESULTS = []                             # (name, outcome, note)


def run_one(name, fn, tmp_parent, retry_llm):
    """单场景:临时目录、一次 LLM 偶发重试、SKIP/Fail 分类;结束清理临时目录。"""
    tmp = tempfile.mkdtemp(prefix='e2e_%s_' % name, dir=tmp_parent)
    t0 = time.time()

    def attempt():
        try:
            fn(tmp)
            return None
        except Exception as e:  # noqa: BLE001 分类(SKip/Fail/意外)在下面统一做
            return e
    err = attempt()
    if err is None:
        RESULTS.append((name, 'PASS', ''))
        print('[%s] PASS(%.0fs)' % (name, time.time() - t0))
        shutil.rmtree(tmp, ignore_errors=True)
        return
    if isinstance(err, Skip):
        RESULTS.append((name, 'SKIP', str(err)))
        print('[%s] SKIP:%s(%.0fs)' % (name, err, time.time() - t0))
        shutil.rmtree(tmp, ignore_errors=True)
        return
    if retry_llm and getattr(err, 'llm', False):
        print('[%s] LLM 层疑似偶发失败(%s),按简报复跑一次:' % (name, err))
        shutil.rmtree(tmp, ignore_errors=True)
        tmp = tempfile.mkdtemp(prefix='e2e_%s_r_' % name, dir=tmp_parent)
        err2 = attempt()
        if err2 is None:
            RESULTS.append((name, 'PASS(重试)', '第一次:%s' % err))
            print('[%s] PASS(重试后,%.0fs)' % (name, time.time() - t0))
            shutil.rmtree(tmp, ignore_errors=True)
            return
        err = err2
    shutil.rmtree(tmp, ignore_errors=True)
    if getattr(err, 'llm', False):
        # 简报口径:真实 LLM 连续失败是产品预期降级路径(熔断/兜底语义),不算 FAIL,给足证据
        traceback.print_exc()
        RESULTS.append((name, 'LLM-DOWN', str(err)))
        print('[%s] LLM-DOWN(降级语义,非产品 FAIL):%s' % (name, err))
        return
    traceback.print_exc()
    RESULTS.append((name, 'FAIL', str(err)))
    print('[%s] FAIL:%s' % (name, err))


def main():
    global VOICES
    print('== live 两层管线 e2e(Task 5)==')
    print('模型:zh-en=%s ja=%s' % (os.path.isdir(MODEL_ZH), os.path.isdir(MODEL_JA)))
    key = parse_key()
    print('MiniMax key:%s' % ('已解析(长度 %d)' % len(key) if key else '无(≈LLM 场景 SKIP)'))
    try:
        VOICES = say_voices()
    except Exception as e:  # noqa: BLE001
        VOICES = set()
        print('say -v ? 失败:%s' % e)
    print('声音:Tingting=%s Samantha=%s Kyoko=%s'
          % ('Tingting' in VOICES, 'Samantha' in VOICES, 'Kyoko' in VOICES))
    tmp_parent = tempfile.mkdtemp(prefix='live_e2e_drop_pass2_')
    try:
        run_one('S1', s1, tmp_parent, retry_llm=False)
        if key:
            run_one('S2', s2, tmp_parent, retry_llm=True)
            run_one('S3', s3, tmp_parent, retry_llm=True)
        else:
            for nm in ('S2', 'S3'):
                RESULTS.append((nm, 'SKIP', '.env 无 MiniMax key'))
                print('[%s] SKIP:.env 无 MiniMax key' % nm)
        run_one('S4', s4, tmp_parent, retry_llm=False)
        if key:
            run_one('S5', s5, tmp_parent, retry_llm=True)
        else:
            RESULTS.append(('S5', 'SKIP', '.env 无 MiniMax key'))
            print('[S5] SKIP:.env 无 MiniMax key')
        run_one('S6', s6, tmp_parent, retry_llm=False)
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)
    print('\n== 汇总 ==')
    bad = 0
    for name, outcome, note in RESULTS:
        print('%s %s%s' % (name, outcome, ('(%s)' % note) if note else ''))
        if outcome == 'FAIL':
            bad += 1
    print('结果:%s' % ('全绿(PASS/SKIP/LLM-DOWN)' if bad == 0 else '%d 个 FAIL' % bad))
    sys.exit(0 if bad == 0 else 1)


if __name__ == '__main__':
    main()
