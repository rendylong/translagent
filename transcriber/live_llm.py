# -*- coding: utf-8 -*-
"""LLM 修正与翻译层(live 实时字幕第二遍,MiniMax-M3.1-Flash;2026-10-24 spec
摘 Pass2 后直接消费 Pass1 final,管线从三遍改两遍)。

对 Pass1 final 的合并批文本做术语级软修正 + 翻译:异步、尽力而为,任何失败
(超时、HTTP 错误、非 JSON 回复、字段缺失、截断响应等一切异常,含
http.client.HTTPException 家族)一律返回 None 并保留识别文本,异常绝不穿透
refine/refine_worker(防杀死 live-llm 线程),不阻塞、不撤销已下发内容;连续
5 次 refine 失败熔断(fused),之后短路返回 None 不再发请求(网络或额度问题,
会话其余部分照常)。实现依据 docs/2026-10-23-live-captions-design.md §6.8 与
docs/2026-10-24-live-captions-drop-pass2-and-segmentation-spec.md §4.3。

合并批:相邻 final 音频间隔 ≤ MERGE_GAP_MS 的并成一批发送(逐行「说话人N:句子」),
摊薄调用量;批内只有一个说话人时事件文本剥掉行首标签(speaker=簇号),跨说话人
(或任一行无合法标签)时保留行标签(speaker=None,前端通用色)。

离线标题生成(2026-10-25):generate_title(text, api_key) 供 app.process_job 在
任务 status='done' 前调用一次——前 TITLE_INPUT_MAX(2000)字符纯转录文本送
MiniMax 拟会议标题,失败静默返回 None(调用方保留原标题);出境面比 refine 更窄,
仅 system 指令 + 截断文本,无上下文无术语表。

延迟优化(2026-10-25):请求体固定 reasoning_effort=medium、传输改每线程复用
HTTPS 连接、攒批等待窗调至 0.5s。依据:M3.1-Flash 思考无法关闭且省略参数默认
max 档(实测);连续语音下 1.5s 等待窗从不真正并批(相邻 final 间隔总大于窗长),
纯加延迟。端到端实测(17 句连续语音仿真):翻译滞后中位 7.2s→2.6s、最差
10.3s→3.6s,call 中位 3.7s→1.6s、尾部 8.5s→2.7s(8s 超时风险随之消失)。

隐私白名单(写死,出境内容仅限):本窗文本(说话人以"说话人N"编号形式,绝无姓名)、
上一窗已修正文本(滚动 ≤2 窗)、术语表字段(v1 预留)、模型名。音频任何形式、文件路径、
时间戳之外的其他 job 内容绝不出境。key 只在运行时从 .env/env.rtf 剥壳解析,仅存内存,
不进日志、不进 meta、不进任何异常文本(last_error 只记异常类名,请求体永不落盘)。
"""
import http.client
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
from collections import deque

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_DOTENV = os.path.join(_REPO_ROOT, '.env')     # 优先:MINIMAX_API_KEY=sk-… 或裸密钥
ENV_RTF = os.path.join(_REPO_ROOT, 'env.rtf')     # 兼容旧文件(RTF 壳)
# 端点实测(2026-10-23,Task 8):本机 env.rtf 的 key 在官方中国域 api.minimaxi.com
# 与 api.minimax.chat 均鉴权通过;计划简报所写国际域 api.minimax.io 返回 401
# invalid api key(密钥属中国控制台)。默认取 minimaxi.com(minimax.chat 的新域名),
# 如换国际账号改这一行即可。
API_URL = 'https://api.minimaxi.com/v1/chat/completions'
_API_SPLIT = urllib.parse.urlsplit(API_URL)
API_HOST = _API_SPLIT.netloc    # 连接复用:HTTPSConnection 只需 host,路径见 API_PATH
API_PATH = _API_SPLIT.path or '/'
MODEL = 'MiniMax-M3.1-Flash'
# 思考档位(2026-10-25 实测定档):M3.1-Flash 是自适应思考模型,思考无法关闭
# (thinking disabled / effort none 服务端均 400 "requires adaptive thinking"),且
# 省略本参数时服务端默认 max 档——省略即最深思考,延迟被思考时长吃掉。实测 low
# 档多行批 5/5 把说话人标签泄进译文(指令遵循崩),medium 思考 token 减半以上、
# 指令遵循完好,是质量/速度均衡点(端到端滞后 7.2s→2.6s 的主因之一)。
REASONING_EFFORT = 'medium'
FUSE_AFTER = 5          # 连续 refine 失败次数达到即熔断(成功复位)
# 合并批常量(spec 2026-10-24 §4.3,摘 Pass2 后 Pass1 final 直喂本层):
MERGE_GAP_MS = 2500     # 相邻 final 间隔 ≤ 此值并批(spec 3B)
MERGE_WAIT_S = 0.5      # 队空攒批等待窗(2026-10-25 由 1.5 调低:连续语音下相邻
                        # final 间隔总 >1.5s,旧值从不真正等到并批、纯加延迟;真正
                        # 的并批发生在积压时,队列非空零等待抽取,不受本值影响)
MERGE_MAX_ITEMS = 6     # 批条数上限(到顶即出批,防长批尾延迟)
MERGE_MAX_SPAN_MS = 30000  # 批时间跨度上限(末句 t1 - 首句 t0,防跨话题并批)
# max_tokens 下限:M3.1-Flash 是推理模型,思考(reasoning_content)计入 max_tokens,
# 实测本用例思考耗 392~951 token——按 spec「输入 2 倍」封顶(≈220)时 content 被截成
# 空(2026-10-23 实测 finish=length、content_len=0)。放大规则保留 2× 输入,下限提至 1536。
MAX_TOKENS_FLOOR = 1536
TITLE_INPUT_MAX = 2000   # 标题生成送出转录文本的字符上限(出境上限,调小随意、调大须审)

_KEY_RE = re.compile(r'sk-[A-Za-z0-9_-]{20,}')


def _err_name(e):
    """异常类名归一:socket.timeout 在 Python 3.10+ 就是 TimeoutError(类名变了),
    统一记 'timeout',与既有日志与测试口径一致。"""
    import socket
    if isinstance(e, (TimeoutError, socket.timeout)):
        return 'timeout'
    return type(e).__name__
_RTF_CTRL_WORD = re.compile(r'\\[a-zA-Z]+-?[0-9]* ?')   # \fs24 \pard1440 等(含尾随空格定界)
_RTF_CTRL_HEX = re.compile(r"\\'[0-9a-fA-F]{2}")        # \'hh 十六进制转义
_RTF_SYMBOL = re.compile(r'\\.')                        # \{ \} \\ \~ 等符号转义
_RTF_BRACES = re.compile(r'[{}]')

_LANG_NAME = {'zh': '简体中文', 'en': '英语', 'ja': '日本語'}


class _BadReply(Exception):
    """响应形状错误(非 JSON、缺字段)——非传输问题,不重试。"""


def parse_key(path=None):
    """从 .env(优先,KEY=value 或裸密钥)或 env.rtf(兼容 RTF 壳/纯文本重存)提取
    首个 sk- 开头密钥;两者皆无则 None。

    剥壳策略:丢弃反斜杠控制字、十六进制转义、符号转义与花括号后按正则找 sk- 串
    (对纯文本/.env 无副作用)。以 latin-1 读文件(逐字节保序):key 全 ASCII,
    RTF 内 GBK 中文注解不影响命中。文件不存在或不可读 → None(此时 Pass3 整层
    禁用,Pass1/2 不受影响)。
    """
    if path is None:
        for cand in (ENV_DOTENV, ENV_RTF):
            key = parse_key(cand)
            if key:
                return key
        return None
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


_conn_tls = threading.local()   # 每线程一条复用连接:live-llm worker 串行调用是
                                # 常驻消费方;auto_title 每任务一次,无复用收益也无害


def _get_conn(timeout_s):
    """取本线程复用的 HTTPS 连接;不存在或超时参数变了则重建。

    复用省掉每次请求的 TCP+TLS 握手(实测每 call 省 0.3~1s)。线程本地即线程
    安全:_chat 的两个调用方(refine_worker 线程、process_job 线程)各自串行,
    绝无并发共用一条连接。"""
    conn = getattr(_conn_tls, 'conn', None)
    if conn is not None and getattr(_conn_tls, 'timeout', None) != timeout_s:
        _drop_conn()
        conn = None
    if conn is None:
        conn = http.client.HTTPSConnection(API_HOST, timeout=timeout_s)
        _conn_tls.conn, _conn_tls.timeout = conn, timeout_s
    return conn


def _drop_conn():
    """丢弃并关闭本线程连接(传输异常后连接状态不可信、worker 收尾时)。"""
    conn = getattr(_conn_tls, 'conn', None)
    _conn_tls.conn = None
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def _roundtrip(data, headers, timeout_s):
    """单次 HTTP 往返:在复用连接上 POST、读尽响应体,返回 (status, reason,
    body_bytes)。非 200 不在此抛,统一由 _chat 决定语义(测试的传输层替身 seam)。"""
    conn = _get_conn(timeout_s)
    conn.request('POST', API_PATH, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    if resp.will_close:            # 服务端要求关连接:主动弃,免下轮首发撞死连接
        _drop_conn()
    return resp.status, resp.reason, raw


def _chat(api_key, system, user, timeout_s, retry_backoff_s):
    """MiniMax(OpenAI 兼容端点)单次对话请求(传输层失败重试 1 次),返回模型
    回复字符串。LLMRefiner._post 与 generate_title 共用。

    传输走每线程复用的 HTTPS 连接(_get_conn/_roundtrip);非 200 与 urllib
    时代同口径抛 HTTPError(OSError 子类 → 传输类,重试 1 次)。复用连接被
    服务端空闲关闭属正常,首发撞上 RemoteDisconnected 会丢弃重建并占用唯一
    一次重试额度。

    抛 _BadReply(响应形状错,不重试)或原始传输层异常(重试后仍失败;
    含 OSError 家族与 http.client.HTTPException 家族——截断的 200 响应
    IncompleteRead、坏状态行等,均非 OSError,漏捕会穿透到调用方)。
    """
    body = {'model': MODEL,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}],
            'temperature': 0,
            'reasoning_effort': REASONING_EFFORT,
            'max_tokens': max(MAX_TOKENS_FLOOR, 2 * len(user))}   # 按输入长度 2 倍封顶
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json',
               'Authorization': 'Bearer ' + api_key}
    last = None
    for attempt in (0, 1):                     # 首发 + 重试 1 次(退避 2s)
        if attempt and retry_backoff_s > 0:
            time.sleep(retry_backoff_s)
        try:
            status, reason, raw = _roundtrip(data, headers, timeout_s)
            if status != 200:
                raise urllib.error.HTTPError(API_URL, status, reason, None, None)
            payload = json.loads(raw.decode('utf-8'))
            return payload['choices'][0]['message']['content']
        except (OSError, ValueError, KeyError, IndexError, TypeError,
                http.client.HTTPException) as e:
            last = e
            _drop_conn()    # 连接状态不可信:丢弃,重试时重建
            if not isinstance(e, (OSError, http.client.HTTPException)):
                raise _BadReply(str(type(e).__name__))   # 响应可读但形状不对 → 无重试价值
    raise last


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
            self._fail(_err_name(e))
            return None
        except Exception as e:          # 兜底(评审 Important):http.client.HTTPException
            self._fail(_err_name(e))       # 家族(IncompleteRead 等)非 OSError,历史上曾
            return None                    # 穿透本层杀死 worker;任何异常都不出 refine
        out = self._parse_reply(content)
        if out is None:
            self._fail('bad_reply')
            return None
        self._fails = 0                # 成功复位连续失败计数
        return out

    # ---------- 请求 ----------

    def _build_messages(self, text, prev_refined):
        src, tgt = self.lang_pair
        # 剥标签指令(Task 6,Task 5 e2e 实测译文常带「Speaker 0:」行前缀):
        # 两分支都要求 translation 为纯译文、不得复述行首的说话人标签
        if src == tgt:
            # 「不翻译」组合(spec 2026-10-24 §4.4):不请求译文,省 token 且避免同语种重复上屏
            rule4 = ('4.translation 输出空字符串 ""(源语与目标语相同,无需翻译;'
                     '如非空也不得包含说话人标签或行前缀);')
        else:
            rule4 = ('4.translation 输出纯%s译文,忠实原意,'
                     '不得包含说话人标签或行前缀;' % _LANG_NAME.get(tgt, tgt))
        system = (
            '你是实时会议字幕的校对与翻译引擎,处理一段语音识别(ASR)产出的窗口文本。规则:'
            '1.只修 ASR 类错误,即同音错字、系统性错词、专有名词、标点与口语顺滑;'
            '输入文本可能没有标点,请在 corrected 中补全标点;'
            '2.不得改写意思,不得增删信息,不得合并或拆分说话人,行首的说话人编号标签必须原样保留;'
            '3.corrected 输出修正后的原文语言文本,若无错误则原样返回输入;'
            + rule4 +
            '5.只输出一个 JSON 对象,键为 corrected 与 translation,不要输出其他文字或代码栅栏;'
            '6.术语表给出的词条优先按术语表纠正。')
        user = json.dumps({'prev_refined': prev_refined, 'text': text,
                           'glossary': self.glossary}, ensure_ascii=False)
        return system, user

    def _post(self, text, prev_refined):
        """发起请求(传输层失败重试 1 次),返回模型回复字符串(实现收敛到 _chat,
        异常口径见其 docstring)。"""
        system, user = self._build_messages(text, prev_refined)
        return _chat(self._key, system, user, self.timeout_s, self.retry_backoff_s)

    def _parse_reply(self, content):
        """模型回复 → {'corrected','translation'};非 JSON 或字段缺失 → None。

        translation 校验按语言对:src==tgt(不翻译)时按指令返回空串,是合法值;
        跨语种时空翻译视为坏回复(保持旧行为)。"""
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
        no_translate = self.lang_pair[0] == self.lang_pair[1]
        if not isinstance(translation, str):
            return None
        if not no_translate and not translation.strip():
            return None
        return {'corrected': corrected.strip(), 'translation': translation.strip()}

    def _fail(self, reason):
        self._fails += 1
        self.last_error = reason


# ---------- 离线任务标题生成(app.auto_title 调用,2026-10-25) ----------

_TITLE_SYSTEM = (
    '你是会议标题生成器。输入是一场会议语音转录的开头片段,可能没有标点、'
    '含口语和识别错字。要求:1.用转录文本的主要语言为这场会议拟一个简短的标题,'
    '中文不超过 20 字,其他语言不超过 12 个词;2.标题概括会议主题,'
    '不包含日期、星期、时间段和说话人姓名;3.只输出标题这一行文字,'
    '不要引号、序号、「标题:」类前缀或任何其他内容。')


def _parse_title(content):
    """模型回复 → 单行标题(≤80 字符,与 meta.title 限长一致);无内容 → None。

    容错:剥 markdown 代码栅栏、取首个非空行、剥「标题/题目/title:」前缀与
    首尾引号书名号——模型对「只输出一行」的遵守是概率性的,防线放在解析侧。
    前缀可能在引号内层或外层,剥引号→剥前缀交替两轮兜住两种嵌套。
    """
    s = (content or '').strip()
    if s.startswith('```'):
        s = re.sub(r'^```[a-zA-Z]*\s*', '', s)
        s = re.sub(r'\s*```$', '', s).strip()
    line = next((ln.strip() for ln in s.splitlines() if ln.strip()), '')
    for _ in range(2):
        line = line.strip(' \t"\'“”「」『』《》')
        line = re.sub(r'^(?:标题|题目|title)\s*[:：]\s*', '', line, flags=re.IGNORECASE)
    return line[:80] or None


def generate_title(text, api_key, timeout_s=8, retry_backoff_s=2.0):
    """转录文本前 TITLE_INPUT_MAX 字符 → 会议标题;任何失败返回 None。

    调用方(app.auto_title)在 process_job 的 status='done' 之前调用一次,
    失败即保留原标题。无 key/空文本不发请求;_chat 的传输/形状异常与解析
    缺陷全吞——离线管线里标题生成绝不影响任务成败。
    """
    text = (text or '').strip()
    if not api_key or not text:
        return None
    try:
        content = _chat(api_key, _TITLE_SYSTEM, text[:TITLE_INPUT_MAX],
                        timeout_s, retry_backoff_s)
    except Exception:                     # noqa: BLE001 尽力而为:标题是锦上添花
        return None
    return _parse_title(content)


def refine_worker(get_event, emit, refiner, stop_flag, busy_flag=None):
    """串行消费 pass1_final 事件 → 合并批 → refine → emit llm(live-llm 线程真身)。

    两层管线口径(2026-10-24 spec):Pass1 final 直喂本层,Pass2 窗口修正已摘除。
    get_event: () -> dict,可传 timeout(get_event(timeout=0.5) 风格);timeout=0
    为非阻塞探取(queue.Empty 立即抛),阻塞取用缺省 timeout,worker 以 Empty
    轮询 stop_flag;
    emit: dict -> None(接 LiveSession._emit,seq 自动补;llm 事件字段
    {type:'llm', t0, t1, text, translation, speaker} 与前端 live.js 一致);
    refiner: LLMRefiner 实例(串行调用,实例非线程安全由本 worker 保证);
    stop_flag: threading.Event,置位后 ≤ 一个取超时内退出;
    busy_flag: threading.Event 或 None(向后兼容)。取到首条 pass1_final 置位,
    本批链(含种子接续的下一批)全部处理完才复位——LiveSession._drain_llm 以
    「队列空且未置位」判排空(spec §4.2 在飞排空竞态修复)。

    合并批(用户拍板 3B):取到首条后在 MERGE_WAIT_S 等待窗内非阻塞探取——
    相邻 final 音频间隔(后句 t0 - 前句 t1)≤ MERGE_GAP_MS 且未超 MERGE_MAX_ITEMS
    条数 / MERGE_MAX_SPAN_MS 跨度 → 并入;间隔或上限超限的事件作下一批种子
    (当前批立即出批,事件不丢);队列空且未过等待窗小睡(≈50ms)再探;
    stop_flag 置位 → 立即出批(停止时不多等,在飞批不被掐)。

    出批处理(整体兜底 try/except,任何残余异常只丢当前批,worker 继续消费,
    绝不把 live-llm 线程带走;refine 自身的兜底在 LLMRefiner.refine 内):
      - 逐行拼「说话人N:句子」(speaker 可 int() 化才加标签,防御:上游若传姓名
        字符串,标签整体丢弃,姓名绝不出境);
      - refiner.fused 熔断 → 恰一次 error 提示(spec §6.8),本批不 refine;
      - refine 失败(None)→ 不 emit llm(UI 保留识别文本,unrefined 降级),
        失败批不进上下文;
      - 成功 → 标签剥离:批内仅一个 speaker 且全部行有合法标签时,corrected
        逐行防御式剥掉已知标签前缀后以 '\n' 连接,事件 speaker=该簇号;跨
        speaker(或任一行无合法标签)时 corrected 原样保留行标签,事件
        speaker=None(前端通用色,逐行标签可读);translation 为整批整体译文;
      - emit {'type':'llm','t0':批首 t0_ms,'t1':批尾 t1_ms,'text','translation',
        'speaker': <int|None>};ctx.append(corrected)(带标签全文,现行为,
        滚动 ≤2 批为 prev_refined)。
    """
    ctx = deque(maxlen=2)
    fused_noticed = False

    def notice_fused():
        """熔断提示只发一次:第 FUSE_AFTER 次失败当场提示,后续事件不再重复。"""
        nonlocal fused_noticed
        if not fused_noticed and getattr(refiner, 'fused', False):
            fused_noticed = True
            emit({'type': 'error', 'message': '翻译服务已暂停(连续失败)'})

    def process_batch(batch):
        """单批出批:拼标签 → refine → 标签剥离 → emit;异常只丢本批(busy 由外层复位)。"""
        try:
            if getattr(refiner, 'fused', False):
                notice_fused()          # 已熔断(含上一场会话遗留状态):提示一次
                return                  # 熔断:消费事件但不 refine、不 emit
            labels, speakers, lines = [], [], []
            for ev in batch:
                text = (ev.get('text') or '').strip()
                if not text:
                    continue            # 空文本条目不入行(不 refine,现口径)
                try:
                    spk = int(ev.get('speaker'))
                    label = '说话人%d:' % spk
                except (TypeError, ValueError):   # 无 speaker 或给了姓名等非编号 → 无标签
                    spk, label = None, ''
                labels.append(label)
                speakers.append(spk)
                lines.append(label + text)
            if not lines:
                return
            out = refiner.refine('\n'.join(lines), '\n'.join(ctx))
            if not out:
                notice_fused()          # 恰在第 FUSE_AFTER 次失败上熔断:当场提示
                return                  # 失败:保留识别文本,不进上下文
            corrected = out['corrected']
            distinct = set(speakers)
            if len(distinct) == 1 and None not in distinct:
                lab = labels[0]         # 单一说话人且全部行有合法标签 → 剥行首标签
                stripped = []
                for ln in corrected.splitlines():   # 防御式:模型漏标签/行首尾加空格都不炸
                    s = ln.strip()
                    stripped.append(s[len(lab):].lstrip() if s.startswith(lab) else s)
                text_out = '\n'.join(x for x in stripped if x)
                ev_speaker = speakers[0]
            else:
                text_out = corrected    # 跨 speaker(或无 speaker):保留行标签
                ev_speaker = None
            ctx.append(corrected)       # 上下文带标签全文(现行为)
            emit({'type': 'llm', 't0': batch[0].get('t0_ms'), 't1': batch[-1].get('t1_ms'),
                  'text': text_out, 'translation': out['translation'], 'speaker': ev_speaker})
        except Exception:               # 兜底:丢当前批,继续消费(refine/emit 故障不杀线程)
            return

    while not stop_flag.is_set():
        try:
            first = get_event()         # 阻塞取(缺省 timeout;Empty 轮询 stop)
        except queue.Empty:
            continue
        except Exception:               # 病态 get_event:不信任的异常不杀线程
            continue
        if not isinstance(first, dict) or first.get('type') != 'pass1_final':
            continue                    # 非 pass1_final(partial 等)照旧丢弃
        if busy_flag is not None:
            busy_flag.set()             # 在飞:从取到首条到本批链处理完
        try:
            batch = [first]
            window_end = time.monotonic() + MERGE_WAIT_S
            while True:
                if stop_flag.is_set():
                    break               # 停止:立即出批,不多等
                try:
                    nxt = get_event(timeout=0)      # 非阻塞探取
                except queue.Empty:
                    nxt = None
                except Exception:
                    nxt = None
                if nxt is None:
                    if time.monotonic() >= window_end:
                        break           # 超等待窗:出批
                    time.sleep(0.05)    # 队空且在窗内:小睡再探
                    continue
                if not isinstance(nxt, dict) or nxt.get('type') != 'pass1_final':
                    continue            # 混入的非目标事件:丢弃,继续探
                try:
                    ok_gap = nxt.get('t0_ms', 0) - batch[-1].get('t1_ms', 0) <= MERGE_GAP_MS
                    ok_items = len(batch) < MERGE_MAX_ITEMS
                    ok_span = nxt.get('t1_ms', 0) - batch[0].get('t0_ms', 0) <= MERGE_MAX_SPAN_MS
                except TypeError:       # 畸形事件(t0_ms/t1_ms 非数字):丢弃,不杀线程
                    continue
                if ok_gap and ok_items and ok_span:
                    batch.append(nxt)
                else:
                    process_batch(batch)   # 间隔/条数/跨度超限:当前批立即出批
                    batch = [nxt]          # 超限事件作下一批种子(不丢事件)
                    window_end = time.monotonic() + MERGE_WAIT_S
            process_batch(batch)
        finally:
            if busy_flag is not None:
                busy_flag.clear()       # 在飞结束:排空判据可观察到(emit 后复位)
    _drop_conn()    # 收尾:释放本线程复用的 API 连接(worker 是 _chat 的常驻调用方)
