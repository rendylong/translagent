# -*- coding: utf-8 -*-
"""Pass3 LLM 修正与翻译层(live 实时字幕第三遍,MiniMax-M3.1-Flash)。

对 Pass2 定稿文本做术语级软修正 + 翻译:异步、尽力而为,任何失败(超时、HTTP 错误、
非 JSON 回复、字段缺失、截断响应等一切异常,含 http.client.HTTPException 家族)一律
返回 None 并保留 whisper 文本,异常绝不穿透 refine/refine_worker(防杀死 Task 9 驱动
线程),不阻塞、不撤销已下发内容;连续 5 次 refine 失败熔断(fused),之后短路返回 None
不再发请求(网络或额度问题,会话其余部分照常)。实现依据
docs/2026-10-23-live-captions-design.md §6.8。

隐私白名单(写死,出境内容仅限):本窗文本(说话人以"说话人N"编号形式,绝无姓名)、
上一窗已修正文本(滚动 ≤2 窗)、术语表字段(v1 预留)、模型名。音频任何形式、文件路径、
时间戳之外的其他 job 内容绝不出境。key 只在运行时从 env.rtf 剥壳解析,仅存内存,
不进日志、不进 meta、不进任何异常文本(last_error 只记异常类名,请求体永不落盘)。
"""
import http.client
import json
import queue
import re
import time
import urllib.error
import urllib.request
from collections import deque

ENV_RTF = '/Users/rendy/财务知识/env.rtf'
# 端点实测(2026-10-23,Task 8):本机 env.rtf 的 key 在官方中国域 api.minimaxi.com
# 与 api.minimax.chat 均鉴权通过;计划简报所写国际域 api.minimax.io 返回 401
# invalid api key(密钥属中国控制台)。默认取 minimaxi.com(minimax.chat 的新域名),
# 如换国际账号改这一行即可。
API_URL = 'https://api.minimaxi.com/v1/chat/completions'
MODEL = 'MiniMax-M3.1-Flash'
FUSE_AFTER = 5          # 连续 refine 失败次数达到即熔断(成功复位)
# max_tokens 下限:M3.1-Flash 是推理模型,思考(reasoning_content)计入 max_tokens,
# 实测本用例思考耗 392~951 token——按 spec「输入 2 倍」封顶(≈220)时 content 被截成
# 空(2026-10-23 实测 finish=length、content_len=0)。放大规则保留 2× 输入,下限提至 1536。
MAX_TOKENS_FLOOR = 1536

_KEY_RE = re.compile(r'sk-[A-Za-z0-9_-]{20,}')
_RTF_CTRL_WORD = re.compile(r'\\[a-zA-Z]+-?[0-9]* ?')   # \fs24 \pard1440 等(含尾随空格定界)
_RTF_CTRL_HEX = re.compile(r"\\'[0-9a-fA-F]{2}")        # \'hh 十六进制转义
_RTF_SYMBOL = re.compile(r'\\.')                        # \{ \} \\ \~ 等符号转义
_RTF_BRACES = re.compile(r'[{}]')

_LANG_NAME = {'zh': '简体中文', 'en': '英语'}


class _BadReply(Exception):
    """响应形状错误(非 JSON、缺字段)——非传输问题,不重试。"""


def parse_key(path=ENV_RTF):
    """从 env.rtf(RTF 壳或 TextEdit 重存的纯文本)提取首个 sk- 开头密钥;无则 None。

    剥壳策略:丢弃反斜杠控制字、十六进制转义、符号转义与花括号后按正则找 sk- 串。
    以 latin-1 读文件(逐字节保序):key 全 ASCII,RTF 内 GBK 中文注解不影响命中。
    文件不存在或不可读 → None(此时 Pass3 整层禁用,Pass1/2 不受影响)。
    """
    try:
        with open(path, 'r', encoding='latin-1') as f:
            s = f.read()
    except OSError:
        return None
    s = _RTF_CTRL_WORD.sub('', s)
    s = _RTF_CTRL_HEX.sub('', s)
    s = _RTF_SYMBOL.sub('', s)
    s = _RTF_BRACES.sub('', s)
    m = _KEY_RE.search(s)
    return m.group(0) if m else None


class LLMRefiner:
    """MiniMax(OpenAI 兼容端点)窗口文本修正 + 翻译客户端。urllib 直调,零新依赖。

    api_key: parse_key 产物;lang_pair: ('zh','en') 源→目标语言;
    timeout_s: 单次请求超时(默认 8);retry_backoff_s: 传输层失败重试 1 次前的退避
    (默认 2s,测试置 0);glossary: v1 预留术语表(list[str],随请求出境)。
    线程模型:实例非线程安全,由 refine_worker 串行调用。
    """

    def __init__(self, api_key, lang_pair=('zh', 'en'), timeout_s=8,
                 glossary=None, retry_backoff_s=2.0):
        self._key = api_key
        self.lang_pair = lang_pair
        self.timeout_s = timeout_s
        self.glossary = glossary
        self.retry_backoff_s = retry_backoff_s
        self._fails = 0
        self.last_error = ''    # 只记异常类名或 'bad_reply',永不带 key/请求体

    @property
    def fused(self):
        """连续 FUSE_AFTER 次 refine 失败 → True;熔断后 refine 短路 None。"""
        return self._fails >= FUSE_AFTER

    def refine(self, text, prev_refined=''):
        """修正 + 翻译一窗文本,成功 {'corrected','translation'};失败 None(降级)。

        text 可带"说话人N:"前缀(指令要求模型原样保留);prev_refined 为上一窗
        已修正文本(滚动 ≤2 窗,由 refine_worker 维护)。
        """
        if self.fused:
            return None
        text = (text or '').strip()
        if not text:
            return None
        try:
            content = self._post(text, prev_refined or '')
        except _BadReply:
            self._fail('bad_reply')
            return None
        except OSError as e:            # URLError/HTTPError/socket.timeout 均 OSError 子类
            self._fail(type(e).__name__)
            return None
        except Exception as e:          # 兜底(评审 Important):http.client.HTTPException
            self._fail(type(e).__name__)   # 家族(IncompleteRead 等)非 OSError,历史上曾
            return None                    # 穿透本层杀死 worker;任何异常都不出 refine
        out = self._parse_reply(content)
        if out is None:
            self._fail('bad_reply')
            return None
        self._fails = 0                # 成功复位连续失败计数
        return out

    # ---------- 请求 ----------

    def _build_messages(self, text, prev_refined):
        target = _LANG_NAME.get(self.lang_pair[1], self.lang_pair[1])
        system = (
            '你是实时会议字幕的校对与翻译引擎,处理一段语音识别(ASR)产出的窗口文本。规则:'
            '1.只修 ASR 类错误,即同音错字、系统性错词、专有名词、标点与口语顺滑;'
            '2.不得改写意思,不得增删信息,不得合并或拆分说话人,行首的说话人编号标签必须原样保留;'
            '3.corrected 输出修正后的原文语言文本,若无错误则原样返回输入;'
            '4.translation 输出%s译文,忠实原意;'
            '5.只输出一个 JSON 对象,键为 corrected 与 translation,不要输出其他文字或代码栅栏;'
            '6.术语表给出的词条优先按术语表纠正。' % target)
        user = json.dumps({'prev_refined': prev_refined, 'text': text,
                           'glossary': self.glossary}, ensure_ascii=False)
        return system, user

    def _post(self, text, prev_refined):
        """发起请求(传输层失败重试 1 次),返回模型回复字符串。

        抛 _BadReply(响应形状错,不重试)或原始传输层异常(重试后仍失败;
        含 OSError 家族与 http.client.HTTPException 家族——截断的 200 响应
        IncompleteRead、坏状态行等,均非 OSError,漏捕会穿透到调用方)。
        """
        system, user = self._build_messages(text, prev_refined)
        body = {'model': MODEL,
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': user}],
                'temperature': 0,
                'max_tokens': max(MAX_TOKENS_FLOOR, 2 * len(user))}   # 按输入长度 2 倍封顶
        req = urllib.request.Request(
            API_URL, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + self._key})
        last = None
        for attempt in (0, 1):                     # 首发 + 重试 1 次(退避 2s)
            if attempt and self.retry_backoff_s > 0:
                time.sleep(self.retry_backoff_s)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
                return payload['choices'][0]['message']['content']
            except (OSError, ValueError, KeyError, IndexError, TypeError,
                    http.client.HTTPException) as e:
                last = e
                if not isinstance(e, (OSError, http.client.HTTPException)):
                    raise _BadReply(str(type(e).__name__))   # 响应可读但形状不对 → 无重试价值
        raise last

    @staticmethod
    def _parse_reply(content):
        """模型回复 → {'corrected','translation'};非 JSON 或字段缺失/空 → None。"""
        s = (content or '').strip()
        if s.startswith('```'):                    # 容错:剥 markdown 代码栅栏
            s = re.sub(r'^```[a-zA-Z]*\s*', '', s)
            s = re.sub(r'\s*```$', '', s).strip()
        i, j = s.find('{'), s.rfind('}')
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(s[i:j + 1])
        except ValueError:
            return None
        corrected, translation = obj.get('corrected'), obj.get('translation')
        if not isinstance(corrected, str) or not corrected.strip():
            return None
        if not isinstance(translation, str) or not translation.strip():
            return None
        return {'corrected': corrected.strip(), 'translation': translation.strip()}

    def _fail(self, reason):
        self._fails += 1
        self.last_error = reason


def refine_worker(get_event, emit, refiner, stop_flag):
    """串行消费 pass2 事件 → refine → emit llm(Task 9 挂进 LiveSession 驱动线程)。

    get_event: () -> dict,阻塞取事件;无事件的等待窗口内抛 queue.Empty
    (建议 lambda: q.get(timeout=0.5) 风格),worker 以 Empty 轮询 stop_flag;
    emit: dict -> None(接 LiveSession._emit,seq 自动补;llm 事件字段
    {type:'llm', t0, t1, text, translation} 与 spec §6.5/前端 live.js 一致);
    refiner: LLMRefiner 实例(串行调用,实例非线程安全由本 worker 保证);
    stop_flag: threading.Event,置位后 ≤ 一个取超时内退出。

    降级语义(设计决定):refine 失败(返回 None)或 refiner.fused 熔断 →
    本窗不 emit llm(不 emit 原文替位),UI 保留 pass2 whisper 文本、无译文——
    即 spec §6.8 的 unrefined 降级;非 pass2 事件(partial 等)直接丢弃。
    说话人标签只接受可 int() 化的编号(防御:上游若传姓名字符串,标签整体丢弃,
    姓名绝不出境),以"说话人N"拼进行首,corrected 按指令保留该标签。
    滚动上下文:最近 ≤2 窗 corrected 换行拼接为 prev_refined;失败窗不进上下文。
    线程存活(评审 Important):循环体整体兜底 try/except——任何残余异常(含
    LLMRefiner 之外的 emit 故障、畸形事件)只丢当前窗,worker 继续消费,绝不
    把 Task 9 的驱动线程带走;refine 自身的兜底在 LLMRefiner.refine 内。
    """
    ctx = deque(maxlen=2)
    while not stop_flag.is_set():
        try:
            ev = get_event()
        except queue.Empty:
            continue
        except Exception:               # 病态 get_event:不信任的异常不杀线程
            continue
        try:
            if not isinstance(ev, dict) or ev.get('type') != 'pass2':
                continue
            if getattr(refiner, 'fused', False):
                continue                   # 熔断:消费事件但不 refine、不 emit
            text = (ev.get('text') or '').strip()
            if not text:
                continue
            try:
                label = '说话人%d:' % int(ev.get('speaker'))
            except (TypeError, ValueError):    # 无 speaker 或给了姓名等非编号 → 无标签
                label = ''
            out = refiner.refine(label + text, '\n'.join(ctx))
            if not out:
                continue                   # 失败:保留 whisper 文本,不进上下文
            ctx.append(out['corrected'])
            emit({'type': 'llm', 't0': ev.get('t0'), 't1': ev.get('t1'),
                  'text': out['corrected'], 'translation': out['translation']})
        except Exception:                 # 兜底:丢当前窗,继续消费下一事件
            continue
