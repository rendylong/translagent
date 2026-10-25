# -*- coding: utf-8 -*-
"""会议标题自动生成(app.auto_title,2026-10-25)。

覆盖策略(用户拍板):非 live 任务的文件名占位标题与 live 未填写的默认
「实时会议」可覆盖;用户在 live 表单填写的标题(「实时 xxx」)不动。
LLM 失败/无 key/空转录静默返回 None,任何异常不穿透。全 mock,不打真 API。
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app       # noqa: E402
import live_llm  # noqa: E402


def _segments(*texts):
    return [{'start_ms': i * 1000, 'end_ms': i * 1000 + 900, 'speaker': 0, 'text': t}
            for i, t in enumerate(texts)]


def _patch_llm(title='LLM 拟的标题', key='sk-test'):
    """同时 mock 密钥解析与标题生成,返回 (parse_key, generate_title) 两个 Mock。"""
    pk = unittest.mock.patch.object(live_llm, 'parse_key', return_value=key)
    gen = unittest.mock.patch.object(live_llm, 'generate_title', return_value=title)
    return pk, gen


class AutoTitleTest(unittest.TestCase):

    def test_upload_job_gets_llm_title(self):
        """上传任务(无 live_session,标题是文件名占位):拼接全文送 LLM,标题被替换。"""
        pk, gen = _patch_llm()
        with pk, gen as g:
            out = app.auto_title({'title': '录音 2026-10-25 14.30'}, _segments('甲说了', '乙说了'))
        self.assertEqual(out, 'LLM 拟的标题')
        self.assertEqual(g.call_count, 1)
        self.assertEqual(g.call_args[0][0], '甲说了\n乙说了')   # 纯文本,无姓名无标签
        self.assertEqual(g.call_args[0][1], 'sk-test')

    def test_live_default_placeholder_replaced_with_prefix(self):
        """live 未填标题(默认「实时会议」):覆盖,并保留「实时 」前缀约定。"""
        pk, gen = _patch_llm()
        with pk, gen as g:
            out = app.auto_title({'title': '实时会议', 'live_session': 'ls_1'}, _segments('内容'))
        self.assertEqual(out, '实时 LLM 拟的标题')
        self.assertEqual(g.call_count, 1)

    def test_live_user_title_untouched(self):
        """live 表单用户填写的标题(「实时 周会」):不发请求,返回 None。"""
        pk, gen = _patch_llm()
        with pk, gen as g:
            out = app.auto_title({'title': '实时 周会', 'live_session': 'ls_1'}, _segments('内容'))
        self.assertIsNone(out)
        self.assertEqual(g.call_count, 0)

    def test_no_key_keeps_title(self):
        """无密钥:parse_key → None,不发请求,保留原标题。"""
        pk = unittest.mock.patch.object(live_llm, 'parse_key', return_value=None)
        gen = unittest.mock.patch.object(live_llm, 'generate_title', return_value='x')
        with pk, gen as g:
            self.assertIsNone(app.auto_title({'title': '文件名'}, _segments('内容')))
        self.assertEqual(g.call_count, 0)

    def test_llm_failure_keeps_title(self):
        """generate_title 返回 None(LLM 失败)→ None;抛异常也吞掉(绝不穿透)。"""
        pk = unittest.mock.patch.object(live_llm, 'parse_key', return_value='sk-test')
        with pk, unittest.mock.patch.object(live_llm, 'generate_title', return_value=None):
            self.assertIsNone(app.auto_title({'title': '文件名'}, _segments('内容')))
        with pk, unittest.mock.patch.object(live_llm, 'generate_title',
                                            side_effect=RuntimeError('炸了')):
            self.assertIsNone(app.auto_title({'title': '文件名'}, _segments('内容')))

    def test_empty_transcript_skips(self):
        """空转录(全空文本段):不发请求。"""
        pk, gen = _patch_llm()
        with pk, gen as g:
            self.assertIsNone(app.auto_title({'title': '文件名'}, _segments('', '  ')))
        self.assertEqual(g.call_count, 0)

    def test_live_prefix_truncates_to_80(self):
        """live 前缀拼接后仍守 80 字符限长(与 meta.title 两处 [:80] 同口径)。"""
        pk, gen = _patch_llm(title='标' * 80)
        with pk, gen:
            out = app.auto_title({'title': '实时会议', 'live_session': 'ls_1'}, _segments('内容'))
        self.assertEqual(out, ('实时 ' + '标' * 80)[:80])


if __name__ == '__main__':
    unittest.main()
