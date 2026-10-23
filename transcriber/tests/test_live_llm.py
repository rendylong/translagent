# -*- coding: utf-8 -*-
"""Pass3 LLM 修正翻译层的行为测试。

三块:
  - ParseKeyTest:env.rtf 的 RTF 壳剥取/纯文本/无 key 三态(不触真文件,迷你壳用 tempfile);
  - RefinerTest:monkeypatch urllib.request.urlopen,测 超时→None、坏 JSON→None、
    连续 5 次失败熔断(成功复位)、请求契约(temperature=0、白名单字段、无音频/路径/说话人姓名);
  - RefineWorkerTest:假队列 + 假 emit 测 worker 语义(消费 pass2→refine→emit llm、
    跳过非 pass2、stop_flag 退出、熔断或 refine 失败不 emit llm 保留 whisper 文本)。

真调用用例(RealCallTest)仅在 env.rtf 可解析出真 key 时运行,输出只含 corrected/translation。
"""
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import live_llm  # noqa: E402
from live_llm import LLMRefiner, parse_key, refine_worker  # noqa: E402

ENV_RTF = str(Path(__file__).resolve().parent.parent.parent / 'env.rtf')
FAKE_KEY = 'sk-unittest-fake-key-0000000000000000'

RTF_SHELL = (
    '{\\rtf1\\ansi\\ansicpg936\\cocoartf2870\n'
    '{\\fonttbl\\f0\\fswiss\\fcharset0 Helvetica;}\n'
    '\\pard\\tx720\\pardirnatural\\partightenfactor0\n'
    '\\f0\\fs24 \\cf0 %s}'
)


def _tmp_file(content):
    fd, path = tempfile.mkstemp(suffix=('.rtf' if content.startswith('{') else '.txt'))
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


class _FakeResp:
    """urlopen 返回值替身:上下文管理器 + read()。"""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reply(content):
    """构造 OpenAI 兼容的成功响应体(content 为模型回复字符串)。"""
    return json.dumps({'choices': [{'message': {'content': content}}]}).encode('utf-8')


class ParseKeyTest(unittest.TestCase):

    def test_parse_key_rtf(self):
        """TextEdit 风格 RTF 壳:剥控制字与花括号后提取首个 sk- 串。"""
        path = _tmp_file(RTF_SHELL % FAKE_KEY)
        try:
            self.assertEqual(parse_key(path), FAKE_KEY)
        finally:
            os.unlink(path)

    def test_parse_key_plain(self):
        """纯文本(TextEdit 重存):直接命中,前后允许有杂项文字。"""
        path = _tmp_file('MiniMax 密钥如下,勿外传\n%s\n— 2026-10-23\n' % FAKE_KEY)
        try:
            self.assertEqual(parse_key(path), FAKE_KEY)
        finally:
            os.unlink(path)

    def test_parse_key_none(self):
        """无 sk- 串(纯 RTF 壳)与文件不存在都返回 None,不抛异常。"""
        path = _tmp_file(RTF_SHELL % '没有密钥的说明文字')
        try:
            self.assertIsNone(parse_key(path))
        finally:
            os.unlink(path)
        self.assertIsNone(parse_key('/nonexistent/env.rtf'))


class RefinerTest(unittest.TestCase):

    def _refiner(self):
        return LLMRefiner(FAKE_KEY, timeout_s=1, retry_backoff_s=0)

    def test_timeout_returns_none(self):
        """传输层异常(含超时)→ None、未熔断;重试 1 次后仍失败算 1 次窗口失败。"""
        r = self._refiner()
        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        side_effect=socket.timeout('timed out')) as m:
            self.assertIsNone(r.refine('测试文本'))
            self.assertEqual(m.call_count, 2)      # 首发 + 重试 1 次
        self.assertFalse(r.fused)
        self.assertEqual(r.last_error, 'timeout')

    def test_http_exception_returns_none(self):
        """评审 Important:截断的 200 响应(IncompleteRead,HTTPException 子类、
        非 OSError)不穿透——refine 返回 None、计入失败、重试 1 次耗尽。"""
        import http.client
        r = self._refiner()
        with unittest.mock.patch.object(
                live_llm.urllib.request, 'urlopen',
                side_effect=http.client.IncompleteRead(b'{"choices')) as m:
            self.assertIsNone(r.refine('测试文本'))
            self.assertEqual(m.call_count, 2)      # 传输层类:首发 + 重试
        self.assertFalse(r.fused)
        self.assertEqual(r.last_error, 'IncompleteRead')

    def test_bad_json_returns_none(self):
        """HTTP 200 但回复不是可解析 JSON → None(不重试,响应格式问题重试无益)。"""
        r = self._refiner()
        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        return_value=_FakeResp(_reply('我不是 JSON'))) as m:
            self.assertIsNone(r.refine('测试文本'))
            self.assertEqual(m.call_count, 1)
        self.assertFalse(r.fused)
        self.assertEqual(r.last_error, 'bad_reply')

    def test_success_parses_reply(self):
        """标准回复(含 markdown 代码栅栏变体)→ {'corrected','translation'};成功复位失败计数。"""
        r = self._refiner()
        ok = _reply('```json\n{"corrected": "这季度毛利下滑", "translation": "Gross margin fell"}\n```')
        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        return_value=_FakeResp(ok)) as m:
            out = r.refine('这季度毛利下滑', prev_refined='上一窗')
            self.assertEqual(out, {'corrected': '这季度毛利下滑',
                                   'translation': 'Gross margin fell'})
            self.assertEqual(m.call_count, 1)
        self.assertFalse(r.fused)

    def test_fuse_after_5(self):
        """连续 5 次 refine 失败 → 熔断;熔断后再调用直接 None 且不再发请求。"""
        r = self._refiner()
        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        return_value=_FakeResp(_reply('坏回复'))) as m:
            for _ in range(5):
                self.assertIsNone(r.refine('文本'))
            self.assertTrue(r.fused)
            calls = m.call_count
            self.assertIsNone(r.refine('文本'))    # 熔断短路
            self.assertEqual(m.call_count, calls)  # 未再发任何请求

    def test_fuse_reset_by_success(self):
        """失败 2 次 → 1 次成功 → 再失败 4 次:计数被成功复位,未熔断。"""
        r = self._refiner()
        bad = _FakeResp(_reply('坏回复'))
        good = _FakeResp(_reply('{"corrected": "好", "translation": "ok"}'))
        side = [bad, bad, good] + [bad] * 8
        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        side_effect=side):
            r.refine('a'); r.refine('a')
            self.assertEqual(r.refine('a'), {'corrected': '好', 'translation': 'ok'})
            for _ in range(4):
                self.assertIsNone(r.refine('a'))
            self.assertFalse(r.fused)

    def test_prompt_contract(self):
        """请求契约:temperature=0、model 正确、user 载荷白名单三字段、上下文透传、
        不含音频/路径/说话人姓名等越权字段;Authorization 为 Bearer key。"""
        r = self._refiner()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['req'] = req
            captured['timeout'] = timeout
            return _FakeResp(_reply('{"corrected": "x", "translation": "y"}'))

        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        side_effect=fake_urlopen):
            r.refine('说话人1:这季度毛利下滑主要是运费和促销投放两块',
                     prev_refined='说话人1:上一窗已修正文本')
        req = captured['req']
        self.assertEqual(captured['timeout'], r.timeout_s)
        self.assertEqual(req.headers.get('Authorization'), 'Bearer ' + FAKE_KEY)
        body = json.loads(req.data.decode('utf-8'))
        # OpenAI 兼容最小面:只允许这四个顶层键
        self.assertEqual(set(body), {'model', 'messages', 'temperature', 'max_tokens'})
        self.assertEqual(body['model'], 'MiniMax-M3.1-Flash')
        self.assertEqual(body['temperature'], 0)
        self.assertIsInstance(body['max_tokens'], int)
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user'])
        # 出境白名单:user 载荷只有 三个字符串字段(术语表 v1 预留)+ 模型名
        user = json.loads(body['messages'][1]['content'])
        self.assertEqual(set(user), {'prev_refined', 'text', 'glossary'})
        self.assertEqual(user['text'], '说话人1:这季度毛利下滑主要是运费和促销投放两块')
        self.assertEqual(user['prev_refined'], '说话人1:上一窗已修正文本')
        self.assertIsNone(user['glossary'])
        # 越权内容零容忍:body 序列化后不含 路径分隔/音频字样/文件后缀(wav)/时间戳元数据
        raw = req.data.decode('utf-8')
        for forbidden in ('/', '.wav', 'audio', 'path', 't0', 't1'):
            self.assertNotIn(forbidden, raw)
        # 系统指令含契约要点:只修 ASR 错误、不改意思、JSON 输出、保留说话人标签
        sysmsg = body['messages'][0]['content']
        for kw in ('ASR', 'JSON', '说话人', 'translation', 'corrected'):
            self.assertIn(kw, sysmsg)


class RefineWorkerTest(unittest.TestCase):
    """worker 语义:假 get_event(队列耗尽时置 stop 并抛 Empty)+ 假 emit 收集。"""

    def _run(self, events, refiner):
        emitted = []
        stop = threading.Event()
        state = {'i': 0}

        def get_event():
            if state['i'] < len(events):
                ev = events[state['i']]
                state['i'] += 1
                return ev
            stop.set()                      # 事件耗尽 → 请求退出(模拟外部 stop)
            raise queue.Empty()

        refine_worker(get_event, emitted.append, refiner, stop)
        return emitted

    def test_worker_consumes_pass2_and_emits_llm(self):
        """消费 pass2 → refine → emit llm(t0/t1 取自 pass2);prev_refined 滚动
        ≤2 窗上下文;非 pass2 事件(partial/pass2_skip)直接跳过。"""
        calls = []

        class FakeRefiner:
            fused = False

            def refine(self, text, prev_refined=''):
                calls.append((text, prev_refined))
                return {'corrected': text + '[修]', 'translation': 'T:' + text[:3]}

        events = [
            {'type': 'partial', 'text': '部分'},
            {'type': 'pass2', 't0': 100, 't1': 2000, 'speaker': 1, 'text': '毛利下滑'},
            {'type': 'pass2_skip', 't0': 2000, 't1': 3000},
            {'type': 'pass2', 't0': 3000, 't1': 5000, 'text': '运费上涨'},   # 无 speaker 键
        ]
        emitted = self._run(events, FakeRefiner())
        self.assertEqual([e['type'] for e in emitted], ['llm', 'llm'])
        self.assertEqual(emitted[0], {'type': 'llm', 't0': 100, 't1': 2000,
                                      'text': '说话人1:毛利下滑[修]',
                                      'translation': 'T:说话人'})
        self.assertEqual(emitted[1]['t0'], 3000)
        self.assertEqual(emitted[1]['text'], '运费上涨[修]')   # 无 speaker 不加标签
        # 输入带说话人编号标签(非姓名);上下文为上一窗 corrected(滚动 2 窗)
        self.assertEqual(calls[0][0], '说话人1:毛利下滑')
        self.assertEqual(calls[0][1], '')
        self.assertEqual(calls[1][0], '运费上涨')
        self.assertEqual(calls[1][1], '说话人1:毛利下滑[修]')

    def test_worker_context_rolls_two_windows(self):
        """滚动上下文 ≤2 窗:第 3 窗的 prev_refined 只含前 2 窗 corrected。"""
        seen = []

        class FakeRefiner:
            fused = False

            def refine(self, text, prev_refined=''):
                seen.append(prev_refined)
                return {'corrected': 'W%d' % (len(seen)), 'translation': 't'}

        events = [{'type': 'pass2', 't0': i, 't1': i + 1, 'text': 'x'} for i in range(3)]
        self._run(events, FakeRefiner())
        self.assertEqual(seen[0], '')
        self.assertEqual(seen[1], 'W1')
        self.assertEqual(seen[2], 'W1\nW2')

    def test_worker_refine_fail_no_emit(self):
        """refine 返回 None(超时/坏 JSON 已在 RefinerTest 钉死)→ 不 emit llm,
        UI 保留 pass2 whisper 文本(spec §6.8 unrefined 降级)。"""

        class FakeRefiner:
            fused = False

            def refine(self, text, prev_refined=''):
                return None

        emitted = self._run(
            [{'type': 'pass2', 't0': 0, 't1': 9, 'text': '原文'}], FakeRefiner())
        self.assertEqual(emitted, [])

    def test_worker_fused_no_emit(self):
        """熔断后:事件照常消费,但不再 refine、不 emit llm(透传 whisper 版本);
        熔断可见——恰一条 error 提示(spec §6.8「熔断并提示」,终审修复项 5)。"""
        calls = []

        class FusedRefiner:
            fused = True

            def refine(self, text, prev_refined=''):
                calls.append(text)
                return {'corrected': 'x', 'translation': 'y'}

        emitted = self._run(
            [{'type': 'pass2', 't0': 0, 't1': 9, 'text': '原文'}], FusedRefiner())
        self.assertEqual(emitted, [{'type': 'error', 'message': '翻译服务已暂停(连续失败)'}])
        self.assertEqual(calls, [])          # refine 根本没被调用

    def test_worker_fused_notifies_once(self):
        """运行中熔断(连续 FUSE_AFTER 次失败):提示恰好在熔断当窗发出一次,
        后续窗口不再重复;期间不 emit llm。"""
        class FuseRefiner:
            FUSE_AFTER = 5

            def __init__(self):
                self.fails = 0
                self.fused = False

            def refine(self, text, prev_refined=''):
                self.fails += 1
                if self.fails >= self.FUSE_AFTER:   # 第 5 次失败即熔断(与真实现一致)
                    self.fused = True
                return None

        r = FuseRefiner()
        events = [{'type': 'pass2', 't0': i, 't1': i + 1, 'text': 'x'} for i in range(8)]
        emitted = self._run(events, r)
        notices = [e for e in emitted if e.get('type') == 'error']
        self.assertEqual([e['type'] for e in emitted], ['error'],
                         '只应有一条熔断提示,无 llm:%s' % emitted)
        self.assertEqual(notices[0]['message'], '翻译服务已暂停(连续失败)')

    def test_worker_speaker_name_never_leaks(self):
        """评审 Minor1:speaker 传姓名字符串(上游 bug/恶意)→ 标签整体丢弃,
        姓名绝不出现在 refine 输入里。"""
        seen = []

        class FakeRefiner:
            fused = False

            def refine(self, text, prev_refined=''):
                seen.append(text)
                return {'corrected': text, 'translation': 't'}

        self._run([{'type': 'pass2', 't0': 0, 't1': 9, 'speaker': '张三',
                    'text': '原文'}], FakeRefiner())
        self.assertEqual(seen, ['原文'])          # 无标签,姓名未出境

    def test_worker_survives_transport_exception(self):
        """评审 Important:真 LLMRefiner + mock urlopen 抛 IncompleteRead——
        第一窗 refine 失败(不 emit),worker 存活并继续消费第二窗成功 emit。"""
        import http.client
        r = LLMRefiner(FAKE_KEY, retry_backoff_s=0)
        good = _FakeResp(_reply('{"corrected": "说话人1:第二窗", "translation": "W2"}'))
        bad = http.client.IncompleteRead(b'{"choices')
        events = [{'type': 'pass2', 't0': 0, 't1': 9, 'speaker': 1, 'text': '第一窗'},
                  {'type': 'pass2', 't0': 9, 't1': 19, 'speaker': 1, 'text': '第二窗'}]
        emitted = []
        stop = threading.Event()
        state = {'i': 0}

        def get_event():
            if state['i'] < len(events):
                ev = events[state['i']]
                state['i'] += 1
                return ev
            stop.set()
            raise queue.Empty()

        with unittest.mock.patch.object(live_llm.urllib.request, 'urlopen',
                                        side_effect=[bad, bad, good]):
            refine_worker(get_event, emitted.append, r, stop)   # 不抛即存活
        self.assertEqual(len(emitted), 1)          # 只有第二窗 emit
        self.assertEqual(emitted[0]['text'], '说话人1:第二窗')
        self.assertFalse(r.fused)                  # 一次失败未熔断

    def test_worker_survives_broken_refiner_and_emit(self):
        """兜底防线:refiner.refine 抛 RuntimeError、emit 抛异常——worker 都只丢
        当前窗继续消费,最终把 stop 前的最后事件 emit 成功。"""

        class ExplodingRefiner:
            fused = False
            n = 0

            def refine(self, text, prev_refined=''):
                ExplodingRefiner.n += 1
                if ExplodingRefiner.n == 1:
                    raise RuntimeError('模拟残余 bug')
                return {'corrected': '好', 'translation': 't'}

        emitted = []
        emit_calls = []

        def emit(ev):
            emit_calls.append(1)
            if len(emit_calls) == 1:               # 仅第一次 emit 炸
                raise RuntimeError('emit 故障')
            emitted.append(ev)

        events = [{'type': 'pass2', 't0': i, 't1': i + 1, 'text': 'x'} for i in range(4)]
        stop = threading.Event()
        state = {'i': 0}

        def get_event():
            if state['i'] < len(events):
                ev = events[state['i']]
                state['i'] += 1
                return ev
            stop.set()
            raise queue.Empty()

        refine_worker(get_event, emit, ExplodingRefiner(), stop)   # 不抛即存活
        self.assertEqual([e['text'] for e in emitted], ['好', '好'])   # 第 2/3 窗成功

    def test_worker_stop_exits_from_blocking(self):
        """队列空时阻塞等待(queue.Empty 轮询),stop_flag 置位后及时退出。"""
        q = queue.Queue()
        stop = threading.Event()
        emitted = []

        def get_event():
            return q.get(timeout=0.05)       # 真队列:空时抛 Empty

        worker = threading.Thread(
            target=refine_worker, args=(get_event, emitted.append,
                                        LLMRefiner(FAKE_KEY, retry_backoff_s=0), stop),
            daemon=True)
        worker.start()
        self.assertTrue(worker.is_alive())   # 队列空 → 阻塞等待而非忙退出
        stop.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


class RealCallTest(unittest.TestCase):
    """真调用一次(仅当 env.rtf 可解析出真 key)。输出只含 corrected/translation,
    绝不打印 key 或 Authorization。"""

    def test_real_refine_translation(self):
        key = parse_key(ENV_RTF)
        if not key:
            self.skipTest('env.rtf 无可用 key')
        r = LLMRefiner(key)
        out = r.refine('说话人1:这季度毛利下滑主要是运费和促销投放两块')
        self.assertIsNotNone(out, '真调用失败:%s' % r.last_error)
        self.assertTrue(out['translation'].strip())
        print('\n[真调用] corrected:', out['corrected'])
        print('[真调用] translation:', out['translation'])


if __name__ == '__main__':
    unittest.main()
