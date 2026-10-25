# -*- coding: utf-8 -*-
"""live 新增 HTTP API 契约测试(WP2-3 / WP6-4)。

GET /api/live/state:侧栏动作行红点查询。不起服务,Flask test_client 直调;
_SESSIONS 里放入未 start 的 LiveSession 壳(构造廉价:模型只在 start() 才加载),
字段手工置位以覆盖状态矩阵。

POST /api/live/recover_now:运行期安全版 live_recover——排除活跃会话目录,
LIVE_ROOT/JOBS_DIR/job_queue 全部 patch 进临时目录,不碰真实 jobs/。
"""
import json
import queue
import struct
import sys
import tempfile
import unittest
import unittest.mock
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


class RecoverNowApiTest(unittest.TestCase):
    """POST /api/live/recover_now(WP6-4):失败寻回不要求重启服务;
    正在录制的活跃会话目录绝不许被拿去转正(验收红线)。"""

    def setUp(self):
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()
        self.live_root = tempfile.mkdtemp()
        self.jobs_dir = tempfile.mkdtemp()
        self.job_queue = queue.Queue()
        for target, val in (('LIVE_ROOT', self.live_root),
                            ('JOBS_DIR', self.jobs_dir),
                            ('job_queue', self.job_queue)):
            p = unittest.mock.patch.object(app, target, val)
            p.start()
            self.addCleanup(p.stop)
        with live_session._SESSIONS_LOCK:          # 活跃注册表同法隔离
            live_session._SESSIONS.clear()
        self.addCleanup(LiveStateApiTest._clear_sessions)

    @staticmethod
    def _make_wav(path, data_bytes=8000):
        data = b'\x00\x00' * (data_bytes // 2)
        hdr = (b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVEfmt '
               + struct.pack('<IHHIIHH', 16, 1, 1, 16000, 32000, 2, 16)
               + b'data' + struct.pack('<I', len(data)))
        Path(path).write_bytes(hdr + data)

    def _make_live_dir(self, sid, status='listening'):
        """造一个「断电残留」的 live 会话目录(非终态 + 有音频)。"""
        d = Path(self.live_root) / sid
        d.mkdir()
        self._make_wav(d / 'live_audio.wav')
        (d / 'live_meta.json').write_text(
            json.dumps({'id': sid, 'title': '断电会', 'speakers': 0,
                        'status': status, 'language': 'zh'}), encoding='utf-8')
        return d

    def _put_active_session(self, sid, status='listening'):
        s = live_session.LiveSession(tempfile.mkdtemp(), 'unused-model-dir')
        s.id = sid
        s.status = status
        s.started_at = 1730000000
        s._meta = {'title': '正在录'}
        with live_session._SESSIONS_LOCK:
            live_session._SESSIONS[sid] = s
        return s

    def _post_recover_now(self):
        r = self.client.post('/api/live/recover_now')
        assert r.status_code == 200, r.data
        return json.loads(r.data)

    def test_recovers_dead_session_to_job(self):
        self._make_live_dir('dead-sess')
        out = self._post_recover_now()
        self.assertEqual(len(out['recovered']), 1)
        jid = out['recovered'][0]
        self.assertTrue((Path(self.jobs_dir) / jid / 'meta.json').is_file(), '应产出 job 目录')
        self.assertEqual(self.job_queue.get_nowait(), jid, 'job 应入队等待 worker')

    def test_active_session_dir_not_touched(self):
        """对录制中的活跃会话调用不得产出 job:目录原样保留、返回空列表。"""
        self._make_live_dir('live-sess-now')
        self._put_active_session('live-sess-now')
        out = self._post_recover_now()
        self.assertEqual(out, {'recovered': []})
        d = Path(self.live_root) / 'live-sess-now'
        self.assertTrue((d / 'live_audio.wav').is_file(), '活跃会话目录不得被清/转')
        self.assertTrue((d / 'live_meta.json').is_file())
        self.assertEqual(list(self.job_queue.queue), [], '不得入队任何 job')

    def test_start_window_idle_registered_dir_not_touched(self):
        """start 窗口(meta 已写、注册先行、status 仍 idle)的目录同样不得被转正或
        清空壳——TOCTOU 回归钉(2026-10-25 评审 Important):active_session_ids
        必须保持注册表全量语义,不得按 ACTIVE_STATUSES 过滤,否则重开本窗口。"""
        self._make_live_dir('window-sess')
        self._put_active_session('window-sess', status='idle')
        out = self._post_recover_now()
        self.assertEqual(out, {'recovered': []})
        d = Path(self.live_root) / 'window-sess'
        self.assertTrue((d / 'live_audio.wav').is_file(), 'start 窗口目录不得被清/转')
        self.assertTrue((d / 'live_meta.json').is_file())

    def test_dead_and_active_mixed(self):
        """混合场景:死目录转正、活跃目录跳过,互不影响。"""
        self._make_live_dir('dead')
        self._make_live_dir('alive')
        self._put_active_session('alive')
        out = self._post_recover_now()
        self.assertEqual(len(out['recovered']), 1)
        self.assertTrue((Path(self.live_root) / 'alive' / 'live_audio.wav').is_file())
        self.assertFalse((Path(self.live_root) / 'dead').exists(), '死目录转正后应清掉')

    def test_idempotent_second_call(self):
        """幂等:第一次已转正(目录清掉),第二次调用返回空、不再产出 job。"""
        self._make_live_dir('once-sess')
        first = self._post_recover_now()
        self.assertEqual(len(first['recovered']), 1)
        second = self._post_recover_now()
        self.assertEqual(second, {'recovered': []})


if __name__ == '__main__':
    unittest.main()
