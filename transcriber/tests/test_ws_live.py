# -*- coding: utf-8 -*-
"""ws_live 转发层单测(终审修复项 2):start 失败必须显式回 error 再收尾断开。

不起服务:flask_sock 的 route 装饰器不回传原函数(app.ws_live 是 None),从
Flask view_functions 取 __wrapped__ 拿到原始 ws_live,以假 ws 对象直调;
假 LiveSession 注入抛错——异常文本故意带本机路径,同时钉死「下行文案
不得泄露路径/密钥」的净化规则(live_start_brief)。

resume 接管语义(2026-10-24 spec WP3):第二连接 resume 顶掉旧连接——旧连接先收
session_taken_over 终态通知再断,新连接重放 backlog 并独占 pump。会话用未 start 的
LiveSession 壳(构造廉价,tap/_emit/backlog/pump 全真),不经模型。
"""
import json
import sys
import tempfile
import threading
import time
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


class BlockableWs(FakeWs):
    """耗尽脚本后阻塞在 receive(模拟「活着但不再发消息」的已附着连接),
    close() 或 release() 才收尾——旧连接被接管前的挂起态。"""

    def __init__(self, msgs):
        super().__init__(msgs)
        self.closed = False
        self._release = threading.Event()

    def receive(self):
        if self.msgs:
            return self.msgs.pop(0)
        self._release.wait(timeout=10)
        raise _Closed()

    def close(self):
        self.closed = True
        self._release.set()

    def release(self):
        self._release.set()


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


class _ShellSessionTest(unittest.TestCase):
    """接管语义测试的公共底座:注册表里放一个未 start 的会话壳。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sess = live_domain.LiveSession(self.root, 'unused-model-dir')
        self.sess.id = 'takeover-test'
        self.sess.status = 'listening'
        with live_domain._SESSIONS_LOCK:
            live_domain._SESSIONS[self.sess.id] = self.sess
        self.addCleanup(self._unregister)

    def _unregister(self):
        with live_domain._SESSIONS_LOCK:
            live_domain._SESSIONS.pop(self.sess.id, None)

    @staticmethod
    def _wait_until(pred, timeout=5.0, what='条件'):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if pred():
                return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _resume_msg(last_seq=0):
        return json.dumps({'type': 'resume', 'session_id': 'takeover-test',
                           'last_seq': last_seq})


class ResumeTakeoverTest(_ShellSessionTest):

    def test_second_conn_resume_takes_over(self):
        """第二连接 resume:旧连接收 session_taken_over 后不再收事件;新连接重放
        backlog 且独占后续事件(锁内取旧 ws 引用、锁外通知,旧 pump 靠 gone 有界退出)。"""
        self.sess._emit({'type': 'pass1_final', 'text': '接管前', 't0_ms': 0, 't1_ms': 100})
        old = BlockableWs([self._resume_msg()])
        new = BlockableWs([self._resume_msg()])
        t_old = threading.Thread(target=WS_LIVE, args=(old,), daemon=True)
        t_old.start()
        self.assertTrue(self._wait_until(lambda: [e['type'] for e in old.sent] == ['pass1_final']),
                        '旧连接应先收到 backlog 重放(已附着)')
        t_new = threading.Thread(target=WS_LIVE, args=(new,), daemon=True)
        t_new.start()
        t_old.join(timeout=5)
        self.assertFalse(t_old.is_alive(), '旧连接应被收尾断开')
        self.assertEqual([e['type'] for e in old.sent],
                         ['pass1_final', 'session_taken_over'], '旧连接:重放 + 终态通知')
        self.assertTrue(old.closed, '旧连接应被服务端关闭')
        # 接管后再发的事件只到新连接;新连接先重放 backlog 再收 live 事件
        self.sess._emit({'type': 'pass1_final', 'text': '接管后', 't0_ms': 200, 't1_ms': 300})
        self.assertTrue(self._wait_until(
            lambda: [e.get('text') for e in new.sent] == ['接管前', '接管后']),
            '新连接应收到 backlog + 后续事件')
        self.assertNotIn('接管后', [e.get('text') for e in old.sent],
                         '旧连接被接管后不得再收到事件')
        new.release()
        t_new.join(timeout=5)

    def test_takeover_replays_only_after_last_seq(self):
        """resume 带 last_seq 时只重放增量;被跳过的旧事件不重复投递。"""
        self.sess._emit({'type': 'pass1_final', 'text': 'a', 't0_ms': 0, 't1_ms': 100})
        seq_a = self.sess._emit({'type': 'pass1_final', 'text': 'b', 't0_ms': 100, 't1_ms': 200})['seq']
        ws = FakeWs([self._resume_msg(last_seq=seq_a)])
        WS_LIVE(ws)
        # FakeWs 耗尽即断开:pump 未及投递属正常,这里断言的是重放阶段
        self.assertIn('b', [e.get('text') for e in ws.sent])
        self.assertNotIn('a', [e.get('text') for e in ws.sent], 'last_seq 之前的事件不得重放')

    def test_same_conn_repeat_resume_idempotent(self):
        """同一连接重复 resume(前端兜底重发):第二条被忽略——不重复重放、不给自己
        发接管通知、连接注册表仍只有本连接一条。"""
        self.sess._emit({'type': 'pass1_final', 'text': '仅一次', 't0_ms': 0, 't1_ms': 100})
        ws = FakeWs([self._resume_msg(), self._resume_msg()])
        WS_LIVE(ws)
        self.assertEqual([e.get('text') for e in ws.sent], ['仅一次'])
        self.assertNotIn('session_taken_over', [e['type'] for e in ws.sent])
        with live_domain._SESSIONS_LOCK:
            self.assertEqual(len(self.sess._conns), 0,
                             'handler 收尾后注册表不得残留本连接(finally 摘除)')


if __name__ == '__main__':
    unittest.main()

