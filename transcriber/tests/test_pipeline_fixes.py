# -*- coding: utf-8 -*-
"""管线快修测试(spec WP7 7.2 / 7.19 / 7.15,2026-10-25 Task 2)。

worker 层不起服务:JOBS_DIR patch 进临时目录,run_whisper / run_diarization /
afconvert(subprocess)全部 mock,只测 process_job / job_cancel /
recover_interrupted 的控制流。真模型探针默认 skip(worktree 无
transcriber/models,不进常规测试路径)。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402


def _make_job(jobs_dir, job_id, **fields):
    """在临时 JOBS_DIR 里落一个 meta.json 齐全的任务(voiceprint 关掉省真模型)。"""
    d = Path(jobs_dir) / job_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {'id': job_id, 'title': 't', 'created_at': '2026-10-25 10:00',
            'language': 'zh', 'num_speakers': 2, 'source_file': 'source.wav',
            'status': 'queued', 'stage': '排队中', 'progress': 0, 'error': None,
            'voiceprint': False, 'speaker_names': {}, 'speaker_map': {}}
    meta.update(fields)
    (d / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return meta


class PipelineFixture(unittest.TestCase):
    """公共夹具:临时 JOBS_DIR + afconvert 桩(转码恒成功)。"""

    def setUp(self):
        self.jobs = tempfile.mkdtemp()
        p = unittest.mock.patch.object(app, 'JOBS_DIR', self.jobs)
        p.start()
        self.addCleanup(p.stop)
        fake = unittest.mock.MagicMock()
        fake.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='', stderr='')
        p2 = unittest.mock.patch.object(app, 'subprocess', fake)
        p2.start()
        self.addCleanup(p2.stop)


class DiarCancelTest(PipelineFixture):
    """7.2:diarizing 阶段放弃须在回调内立即中止(sherpa 2.0.0 忽略返回值,
    回调内 raise 才是真中止),而不是等下一个阶段边界。"""

    _WSEGS = [{'start_ms': 0, 'end_ms': 1000, 'text': '你好'}]

    def test_cancel_during_diar_aborts_at_first_callback(self):
        """cancel_requested 置位后首次回调即中止:任务终态 error 含「已放弃」,
        且 diarization 假身不得在回调之后再往下跑(旧实现恒 return True 会跑完)。"""
        jid = 'j-diar-cancel'
        _make_job(self.jobs, jid, num_speakers=2)
        after_cb = []

        def fake_diar(wav, nspeakers, cb):
            # 模拟用户在说话人识别进行中点了「放弃」:标记落盘后回调到达
            app.write_meta(jid, cancel_requested=True)
            cb(1, 100)
            after_cb.append(True)          # 走到这里 = 回调没有中止
            return [(0.0, 1.0, 0)]

        with unittest.mock.patch.object(app, 'run_whisper', return_value=(60.0, self._WSEGS)), \
             unittest.mock.patch.object(app, 'run_diarization', fake_diar):
            app.process_job(jid)

        meta = app.read_meta(jid)
        self.assertEqual(meta['status'], 'error')
        self.assertIn('已放弃', meta['error'])
        self.assertEqual(after_cb, [], '首次回调即须中止,不得继续跑完 diarization')

    def test_no_cancel_still_completes(self):
        """回归护栏:不放弃时回调照常推进,管线走到 done、说话人数正确。"""
        jid = 'j-diar-ok'
        _make_job(self.jobs, jid, num_speakers=2)

        def fake_diar(wav, nspeakers, cb):
            cb(50, 100)
            return [(0.0, 1.0, 0)]

        with unittest.mock.patch.object(app, 'run_whisper', return_value=(60.0, self._WSEGS)), \
             unittest.mock.patch.object(app, 'run_diarization', fake_diar):
            app.process_job(jid)

        meta = app.read_meta(jid)
        self.assertEqual(meta['status'], 'done')
        self.assertEqual(meta['num_speakers_detected'], 1)


@unittest.skip('真模型探针:worktree 无 transcriber/models(分段/声纹模型缺失),'
               '不进常规测试路径;主树有模型时可临时去掉本装饰器复测'
               '(eng-review 2026-10-24 已实测双证:返回值被忽略、raise 秒级中止)')
class RealModelCancelProbe(unittest.TestCase):
    """7.2 真模型探针:sherpa-onnx 回调内 raise 的中止行为与实例无损复用。

    合成 10s 静音 wav,回调首次调用即 raise RuntimeError('已放弃'):
    - 异常须传播出 run_diarization(证明中止生效);
    - 耗时须秒级(<60s;60s 真实音频实测 ~2.5s);
    - 随后用正常回调完整跑完一遍(证明库/实例状态无损)。
    """

    def test_callback_raise_aborts_and_instance_survives(self):
        import wave as wave_mod
        wav = os.path.join(tempfile.mkdtemp(), 'probe.wav')
        with wave_mod.open(wav, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b'\x00\x00' * 16000 * 10)

        def abort_cb(done, total):
            raise RuntimeError('已放弃')

        t0 = time.time()
        with self.assertRaises(RuntimeError):
            app.run_diarization(wav, 0, abort_cb)
        self.assertLess(time.time() - t0, 60.0, '回调 raise 应秒级传播,不得跑完全程')

        seen = []
        res = app.run_diarization(wav, 0, lambda d, t: seen.append(d) or True)
        self.assertIsInstance(res, list, '中止后再次完整跑完:状态无损')
        self.assertTrue(seen, '正常路径回调应被调用')


class CancelCommGuardTest(PipelineFixture):
    """7.19:cancel 的 kill 前加与 recover_interrupted 同款 comm 校验——只杀
    comm 含 'whisper' 的进程。whisper 退出后 pid 被系统复用时,无条件 kill
    会误杀无辜进程;7.2 让 diarizing 放弃真正可用后该路径使用频率上升。"""

    def _cancel(self, job_id, comm_stdout):
        """POST /api/jobs/<job_id>/cancel,ps 桩返回 comm_stdout;返回全部 run 调用。"""
        app.subprocess.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=comm_stdout, stderr='')
        app.app.config['TESTING'] = True
        r = app.app.test_client().post(f'/api/jobs/{job_id}/cancel')
        assert r.status_code == 200, r.data
        return app.subprocess.run.call_args_list

    def test_non_whisper_pid_not_killed(self):
        """pid 的 comm 不是 whisper → 不发 kill;放弃标记仍须落盘。"""
        _make_job(self.jobs, 'j-cancel', status='transcribing', worker_pid=424242)
        calls = self._cancel('j-cancel', 'launchd\n')   # ps 桩:该 pid 是别的进程
        cmds = [c.args[0] for c in calls]               # 每个调用的命令列表
        self.assertNotIn(['kill', '424242'], cmds, '非 whisper 进程不得 kill')
        self.assertTrue(any(c and c[0] == 'ps' for c in cmds), 'kill 前应先做 comm 校验')
        self.assertTrue(app.read_meta('j-cancel')['cancel_requested'],
                        '即便没 kill,放弃标记仍要落盘让阶段边界收敛')

    def test_whisper_pid_killed(self):
        """comm 含 whisper → 照旧 kill,whisper 子进程立即生效的语义不回退。"""
        _make_job(self.jobs, 'j-cancel-w', status='transcribing', worker_pid=424243)
        calls = self._cancel('j-cancel-w', '/Users/x/.tools/whisper.cpp/build/bin/whisper-cli\n')
        cmds = [c.args[0] for c in calls]
        self.assertIn(['kill', '424243'], cmds, 'whisper 进程仍须被 kill')


class RestartMessageTest(PipelineFixture):
    """7.15:重启中断文案与「用原音频重跑」口径一致——旧文案教用户删任务,
    与 retry API 和前端「↻ 用原音频重跑」按钮打架(spec 7.15)。"""

    def test_recover_message_points_to_retry(self):
        jid = 'j-recover'
        _make_job(self.jobs, jid, status='transcribing')   # 无 worker_pid:不走 ps/kill
        app.recover_interrupted()
        meta = app.read_meta(jid)
        self.assertEqual(meta['status'], 'error')
        self.assertEqual(meta['error'], '服务重启导致中断,可用原音频重跑')

    def test_terminal_job_untouched(self):
        """终态任务不被 recover_interrupted 改写(幂等护边,顺带钉住)。"""
        _make_job(self.jobs, 'j-done', status='done', error=None)
        app.recover_interrupted()
        self.assertEqual(app.read_meta('j-done')['status'], 'done')


if __name__ == '__main__':
    unittest.main()
