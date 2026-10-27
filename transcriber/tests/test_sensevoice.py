# -*- coding: utf-8 -*-
"""SenseVoice 离线转录(2026-10-26):句子切分纯函数、文本清理、可用性检查、
silero-vad 分窗喂入回归钉子(整段喂只处理尾窗的坑)。

模型相关用例在本机有模型文件才跑(models/sense-voice、models/silero-vad),
缺失时 skip——新鲜克隆在下载模型前仍应全绿。
"""
import os
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

HAS_VAD = os.path.isfile(app.SILERO_VAD_MODEL)
HAS_SV = app.sensevoice_available()


class TidyTextTest(unittest.TestCase):
    def test_collapses_repeated_punct(self):
        self.assertEqual(app._tidy_sv_text('断句还是有点问题。。'), '断句还是有点问题。')
        self.assertEqual(app._tidy_sv_text('真的吗??'), '真的吗?')
        self.assertEqual(app._tidy_sv_text('好;;'), '好;')

    def test_keeps_distinct_punct_and_strips(self):
        self.assertEqual(app._tidy_sv_text(' 一二三。 '), '一二三。')
        self.assertEqual(app._tidy_sv_text('什么?!'), '什么?!')   # 不同标点相连不折

    def test_strips_cjk_inner_spaces(self):
        self.assertEqual(app._tidy_sv_text('そうで、すね日本 語 も ちゃんと'), 'そうで、すね日本語もちゃんと')
        self.assertEqual(app._tidy_sv_text('翻译 到 日文'), '翻译到日文')
        self.assertEqual(app._tidy_sv_text('hello world'), 'hello world')   # 拉丁文间空格保留
        self.assertEqual(app._tidy_sv_text('中文 and English'), '中文 and English')   # CJK↔拉丁之间保留


class MergeSpansTest(unittest.TestCase):
    def test_merges_short_gaps(self):
        self.assertEqual(app.merge_vad_spans([(0, 3.0), (3.5, 6.0)]), [(0, 6.0)])
        self.assertEqual(app.merge_vad_spans([(0, 3.0), (3.8, 6.0)], max_gap=1.0), [(0, 6.0)])

    def test_keeps_long_gaps(self):
        # 真静音/噪声区的间隙(秒级)不合并——防幻觉的根基
        self.assertEqual(app.merge_vad_spans([(0, 29.0), (42.8, 43.2)]), [(0, 29.0), (42.8, 43.2)])

    def test_respects_max_len(self):
        spans = [(0, 14.0), (14.4, 20.0), (20.3, 26.0)]
        self.assertEqual(app.merge_vad_spans(spans, max_gap=0.8, max_len=28.0), [(0, 26.0)])
        self.assertEqual(app.merge_vad_spans(spans, max_gap=0.8, max_len=15.0),
                         [(0, 14.0), (14.4, 26.0)])   # 与首段并会 26s>15,退而从第二段起并


class SplitSentencesTest(unittest.TestCase):
    def test_exact_alignment(self):
        text = '我想试一下。现在看起来。'   # 「。」在 5 与 11
        toks = list(text)
        ts = [round(0.1 * i, 3) for i in range(len(text))]
        out = app.split_sv_sentences(text, toks, ts)
        self.assertEqual([t for t, _, _ in out], ['我想试一下。', '现在看起来。'])
        self.assertAlmostEqual(out[0][1], ts[0])
        self.assertAlmostEqual(out[0][2], ts[5])          # 「。」的时间
        self.assertAlmostEqual(out[1][1], ts[6])
        self.assertAlmostEqual(out[1][2], ts[11])

    def test_interp_when_token_count_differs(self):
        # ITN 改变字符数(token 5 个 vs 文本 10 字)→ 按占比内插,时间单调递增
        out = app.split_sv_sentences('一共一百二十三元。好的。', ['a'] * 5,
                                     [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(len(out), 2)
        self.assertLess(out[0][1], out[0][2])
        self.assertLessEqual(out[0][2], out[1][1] + 1e-6)

    def test_no_punct_single_sentence(self):
        out = app.split_sv_sentences('短句', list('短句'), [0.5, 0.9])
        self.assertEqual(out, [('短句', 0.5, 0.9)])

    def test_special_tokens_filtered(self):
        text = '一。'
        out = app.split_sv_sentences(text, ['<|zh|>', '一', '。', '<|NEUTRAL|>'],
                                     [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(out, [('一。', 1.0, 2.0)])

    def test_empty_inputs(self):
        self.assertEqual(app.split_sv_sentences('', ['a'], [1.0]), [])
        self.assertEqual(app.split_sv_sentences('文本', [], []), [])

    def test_halfwidth_punct_splits(self):
        out = app.split_sv_sentences('Hello! World.', list('Hello! World.'),
                                     [round(0.2 * i, 3) for i in range(13)])
        self.assertEqual([t for t, _, _ in out], ['Hello!', 'World.'])


@unittest.skipUnless(HAS_VAD, '本机无 silero-vad 模型')
class VadSpansTest(unittest.TestCase):
    def test_detects_speech_and_skips_silence(self):
        # 真实语音(模型发布包自带 zh.wav)前后各垫静音:speech 区段必须被检出,
        # 且不越过结尾静音。分窗喂入若被破坏(整段喂只处理尾窗),spans 会缺失/为空。
        sr = 16000
        wav = os.path.join(os.path.dirname(app.SENSEVOICE_MODEL), 'test_wavs', 'zh.wav')
        if not os.path.isfile(wav):
            self.skipTest('无 test_wavs/zh.wav')
        speech = app.read_wav_float(wav)
        x = np.concatenate([np.zeros(int(0.8 * sr), dtype=np.float32), speech,
                            np.zeros(int(1.5 * sr), dtype=np.float32)])
        spans = app.vad_speech_spans(x, sr)
        self.assertTrue(spans, '必须检出语音区段')
        total = sum(b - a for a, b in spans)
        self.assertGreater(total, len(speech) / sr * 0.7)  # 语音基本全覆盖(自然语料首尾轻声会略缩)
        self.assertLess(spans[-1][1], len(x) / sr - 1.0)   # 不越进结尾静音


class AvailabilityTest(unittest.TestCase):
    def test_paths_and_langs(self):
        self.assertEqual(app.SENSEVOICE_LANGS, {'zh', 'ja', 'en'})
        self.assertEqual(app.SENSEVOICE_MODEL.count('sense-voice'), 1)
        # auto 不在 SenseVoice 集合:混语录音交 whisper 的逐窗 LID,保持稳健
        self.assertNotIn('auto', app.SENSEVOICE_LANGS)


if __name__ == '__main__':
    unittest.main()
