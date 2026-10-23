# -*- coding: utf-8 -*-
"""ws_live 转发层单测(终审修复项 2):start 失败必须显式回 error 再收尾断开。

不起服务:flask_sock 的 route 装饰器不回传原函数(app.ws_live 是 None),从
Flask view_functions 取 __wrapped__ 拿到原始 ws_live,以假 ws 对象直调;
假 LiveSession 注入抛错——异常文本故意带本机路径,同时钉死「下行文案
不得泄露路径/密钥」的净化规则(live_start_brief)。
"""
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import live_session as live_domain  # noqa: E402

WS_LIVE = app.app.view_functions['ws_live'].__wrapped__


class _Closed(Exception):
    pass


class FakeWs:
    """ws 对象替身:receive 依次回放脚本消息,耗尽后抛 _Closed(模拟断开)。"""

    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.sent = []

    def receive(self):
        if not self.msgs:
            raise _Closed()
        return self.msgs.pop(0)

    def send(self, data):
        self.sent.append(json.loads(data))


class StartFailureTest(unittest.TestCase):

    def test_constructor_failure_sends_sanitized_error(self):
        """LiveSession 构造(加载 300MB 模型)抛错:下行恰一条 error,含「无法开始
        实时会话」与异常类名,路径打码为 [路径],原文案里的 /Users 绝不出现;
        不发 started;handler 正常返回(连接收尾,不再消费后续消息)。"""
        ws = FakeWs([json.dumps({'type': 'start', 'title': 'x', 'speakers': 2,
                                 'language': 'zh>en'}),
                     b'\x00\x00\x00\x00'])           # error 后应已收尾,此帧不被处理
        boom = RuntimeError('模型加载失败: /Users/rendy/secret/model.bin 缺失')
        with unittest.mock.patch.object(live_domain, 'LiveSession', side_effect=boom):
            WS_LIVE(ws)                              # 正常返回,不抛出
        self.assertEqual([e.get('type') for e in ws.sent], ['error'])
        msg = ws.sent[0]['message']
        self.assertIn('无法开始实时会话', msg)
        self.assertIn('RuntimeError', msg)
        self.assertIn('[路径]', msg)
        self.assertNotIn('/Users', msg, 'error 文案不得含本机路径')

    def test_start_method_failure_also_notified(self):
        """构造成功、start() 抛错(线程拉起等):同样回 error;session 不残留
        (后续二进制帧/stop 不会再碰半途对象——handler 已 return)。"""
        class HalfSession:
            started = False

            def __init__(self, *a, **kw):
                pass

            def start(self, *a, **kw):
                raise OSError('cannot start threads')
        ws = FakeWs([json.dumps({'type': 'start'}),
                     json.dumps({'type': 'stop'})])
        with unittest.mock.patch.object(live_domain, 'LiveSession', HalfSession):
            WS_LIVE(ws)
        self.assertEqual([e.get('type') for e in ws.sent], ['error'])
        self.assertIn('OSError', ws.sent[0]['message'])
        self.assertFalse(HalfSession.started)

    def test_brief_masks_paths_and_keys(self):
        """live_start_brief 净化规则:unix/win 路径打码、sk- 密钥打码、多行只取
        首行、空文案退化为类名。"""
        b = app.live_start_brief
        self.assertNotIn('/Users', b(RuntimeError('nope /Users/a/b.bin')))
        self.assertNotIn('C:\\', b(RuntimeError('missing C:\\tools\\x.dll')))
        self.assertNotIn('sk-secret', b(KeyError('bad sk-abcdefgh1234567890 token')))
        self.assertIn('ValueError', b(ValueError('')))          # 空文案 → 仅类名
        self.assertEqual(b(ValueError('')), 'ValueError')
        out = b(RuntimeError('第一行 /p/q\n第二行秘密'))
        self.assertNotIn('第二行', out, '只保留首行')


if __name__ == '__main__':
    unittest.main()
