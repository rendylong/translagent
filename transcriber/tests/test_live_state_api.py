# -*- coding: utf-8 -*-
"""GET /api/live/state 契约测试:侧栏动作行红点的查询端点(WP2-3)。

不起服务:Flask test_client 直调;_SESSIONS 里放入未 start 的 LiveSession 壳
(构造廉价:模型只在 start() 才加载),字段手工置位以覆盖状态矩阵。
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import live_session  # noqa: E402


class LiveStateApiTest(unittest.TestCase):

    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()
        with live_session._SESSIONS_LOCK:          # 防其他用例残留污染形状断言
            live_session._SESSIONS.clear()
        self.addCleanup(self._clear_sessions)

    @staticmethod
    def _clear_sessions():
        with live_session._SESSIONS_LOCK:
            live_session._SESSIONS.clear()

    def _put_session(self, sid, status, started_at, title):
        """往注册表放一个未 start 的会话壳(仅字段齐全,不拉线程)。"""
        s = live_session.LiveSession(tempfile.mkdtemp(), 'unused-model-dir')
        s.id = sid
        s.status = status
        s.started_at = started_at
        s._meta = {'title': title}
        with live_session._SESSIONS_LOCK:
            live_session._SESSIONS[sid] = s
        return s

    def _get_state(self):
        r = self.client.get('/api/live/state')
        assert r.status_code == 200, r.data
        return json.loads(r.data)

    def test_no_session_all_none(self):
        """无会话:全 None 假值的固定四键形状(spec §5 不变量:仅新增端点)。"""
        self.assertEqual(self._get_state(),
                         {'active': False, 'status': None, 'title': None, 'started_at': None})

    def test_single_listening_session(self):
        self._put_session('s1', 'listening', 1730000000, '周会')
        self.assertEqual(self._get_state(),
                         {'active': True, 'status': 'listening', 'title': '周会',
                          'started_at': 1730000000})

    def test_stopping_session_still_active(self):
        """stopping(转正排空期)仍算 active:GPU/管线还被占,红点不应提前熄灭。"""
        self._put_session('s2', 'stopping', 1730000001, '收尾会')
        self.assertEqual(self._get_state(),
                         {'active': True, 'status': 'stopping', 'title': '收尾会',
                          'started_at': 1730000001})

    def test_multiple_sessions_latest_wins(self):
        """多个活跃会话取 started_at 最新者(含 stopping 在内的比较)。"""
        self._put_session('old', 'listening', 1730000000, '早场')
        self._put_session('new', 'stopping', 1730000005, '晚场')
        st = self._get_state()
        self.assertTrue(st['active'])
        self.assertEqual(st['title'], '晚场')
        self.assertEqual(st['started_at'], 1730000005)

    def test_idle_session_ignored(self):
        """注册表里非 listening/stopping(如壳对象 idle)不计入 active。"""
        self._put_session('ghost', 'idle', 1730000009, '幽灵')
        self.assertEqual(self._get_state(),
                         {'active': False, 'status': None, 'title': None, 'started_at': None})


if __name__ == '__main__':
    unittest.main()
