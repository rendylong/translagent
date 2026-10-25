# -*- coding: utf-8 -*-
"""Task 7 前端页面自测:静态存在性断言 + SentenceSplit/几何离屏断言 + 协议级流程模拟。

四段:
  1. StaticCheck:live.html / live.js 关键实现的字符串存在性
     (audio_flush_done、流式降采样、resume、5 分钟缓冲上限、「停止」文案、不出现「转正」,
     及 2026-10-24 句子账本化钉子:双轨删除回归、静态骨架、色板变量、reduced-motion)。
  2. SentenceSplitCheck:从 live.js 源码抠出 splitSentences(花括号配平,不 eval 整文件),
     经本机 .tools/electron 的 node 模式执行断言向量(本机无 node;二进制缺失时 skip)。
  3. BarGeometryCheck:离屏 BrowserWindow 加载真实 live.html,实测账本两静态行
     (prow/empty)行高 >0 且 ≤30px、与正常句行同高(Task 1 复审的持久回归钉子;同 skip 模式)。
  4. ProtocolFlow:起 app.py 子进程,按前端 live.js 的真实时序走一遍
     start → 喂 zh 锚点 30s(200ms/包)→ 收 partial/pass1_final →
     断线重连 resume(last_seq=0 重放)→ 发 audio_flush_done → stop → stop_ack → stopped。
     断言与 live_session.py / app.py ws_live 的实际实现逐点对齐(无 resumed 消息、
     pass2 字段名 t0/t1 在本流程不出现——pass2 管线尚未接入 WS,前端先行画好)。
     另有 test_stop_after_reconnect 覆盖「断线期按停止」路径:重连后必须先 resume
     挂回会话再补发音频再 stop(否则新连接 session=None,stop 落空、会话悬死),
     断言 stop_ack/stopped 到达且服务端 wav 已 finalize。

运行:cd transcriber && python3 tests/live_page_selftest.py
(文件名不含 test 前缀,unittest discover 默认模式不会扫到它,需单独跑;
 端口 8765 被占时协议段自动跳过,静态段始终执行。)
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import numpy as np
import websocket

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import app  # noqa: E402  只为复用 read_wav_float(与 ws_smoke 同款)

PORT = 8765
CHUNK = 3200            # 200ms @16k 单声道 16bit(与 live.js CHUNK_SAMPLES 一致)
FEED_SEC = 30
LIVE_HTML = BASE / 'static' / 'live.html'
LIVE_JS = BASE / 'static' / 'live.js'
INDEX_HTML = BASE / 'static' / 'index.html'   # 主界面(实时字幕入口钉子用)
REPO = BASE.parent                            # 仓库根(.tools/electron 按 AGENTS.md 约定在此)
ELECTRON = REPO / '.tools' / 'electron' / 'Electron.app' / 'Contents' / 'MacOS' / 'Electron'


def electron_missing():
    """本机无 node,SentenceSplit/几何两段都靠 .tools/electron 执行;二进制缺失时跳过
    (与协议段的端口占用跳过同款协议:环境不满足→skip,而非误报失败)。"""
    return not ELECTRON.is_file()


def extract_function(src, name):
    """按花括号配平从 live.js 源码抠出单个函数定义。

    不 eval 整文件:live.js 是 IIFE,顶层还有 DOM/WS 依赖,整文件进 node 只会炸;
    函数体内无字符串/正则含裸花括号,配平才可靠。"""
    m = re.search(r'function\s+' + name + r'\s*\(', src)
    if not m:
        raise AssertionError('live.js 缺少函数 %s' % name)
    start = src.index('{', m.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError('%s 花括号未配平(源码残缺?)' % name)


def run_electron_node(script_path):
    """ELECTRON_RUN_AS_NODE=1 把 Electron 二进制当纯 node 用(本机装 node 的替代)。"""
    env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
    return subprocess.run([str(ELECTRON), str(script_path)], env=env,
                          capture_output=True, timeout=60, cwd=str(REPO))


def last_json_line(raw):
    """从子进程 stdout 抠 JSON 行(倒序找,容忍上游警告行)。"""
    for ln in reversed(raw.decode('utf-8', 'replace').splitlines()):
        ln = ln.strip()
        if ln[:1] in ('[', '{'):
            return ln
    raise AssertionError('子进程输出中无 JSON 行:%r' % raw[:200])


def port_busy():
    try:
        with socket.create_connection(('127.0.0.1', PORT), timeout=1):
            return True
    except OSError:
        return False


def wait_port(proc, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('app.py 子进程提前退出(code=%s)' % proc.returncode)
        if port_busy():
            return
        time.sleep(0.3)
    raise RuntimeError('服务 %.0fs 内未就绪' % timeout)


def load_pcm_bytes():
    with open(BASE / 'models' / 'bench' / 'anchors.json') as fp:
        anchors = json.load(fp)
    samples = app.read_wav_float(anchors['zh']['wav'])[:FEED_SEC * 16000]
    assert len(samples) == FEED_SEC * 16000, '锚点音频不足 %ds' % FEED_SEC
    q = np.clip(samples * 32768.0, -32768.0, 32767.0)
    return q.astype('<i2').tobytes()


def cleanup_job(job_id, timeout_s=420):
    """测试转正产生的 job:等服务跑完再删,不留垃圾在工作台(Task 9 起停止即建 job)。"""
    base = 'http://127.0.0.1:%d/api/jobs/%s' % (PORT, job_id)
    deadline = time.time() + timeout_s
    status = ''
    while time.time() < deadline:
        with urllib.request.urlopen(base, timeout=10) as r:
            status = json.load(r)['job']['status']
        if status in ('done', 'error'):
            break
        time.sleep(2)
    req = urllib.request.Request(base, method='DELETE')
    urllib.request.urlopen(req, timeout=10).read()
    print('job %s %s,已删除' % (job_id, status))


class StaticCheck(unittest.TestCase):
    """live.html / live.js 关键实现的静态存在性(可回归的最低保障)。"""

    @classmethod
    def setUpClass(cls):
        cls.html = LIVE_HTML.read_text(encoding='utf-8')
        cls.js = LIVE_JS.read_text(encoding='utf-8')

    def test_ws_url_and_upstream_messages(self):
        self.assertIn('/ws/live', self.js)
        for word in ('start', 'stop', 'resume'):
            self.assertRegex(
                self.js, r"type\s*:\s*['\"]" + word + r"['\"]",
                '上行消息 %s 缺失或未按 {type: ...} 形式发送' % word)

    def test_audio_flush_done_sent_after_resume(self):
        self.assertIn('audio_flush_done', self.js)

    def test_streaming_resample_present(self):
        self.assertIn('resampleTo16k', self.js)      # 流式降采样(16k AudioContext 失败时)

    def test_buffer_cap_5min(self):
        self.assertIn('BUFFER_MAX_SEC = 5 * 60', self.js)

    def test_stop_copy_and_jargon_limit(self):
        self.assertIn('停止', self.html)
        self.assertIn('停止', self.js)
        # 「转正」曾属内部术语、整体禁入界面文案;WP6 起唯一例外是寻回按钮
        # 「立即重试转正」(spec 点名)——出现必须成对,不许别处漏进来
        for text in (self.html, self.js):
            self.assertEqual(text.count('转正'), text.count('立即重试转正'),
                             '「转正」只能以「立即重试转正」整词出现(界面术语纪律)')
            self.assertGreaterEqual(text.count('立即重试转正'), 1,
                                    'WP6 寻回按钮「立即重试转正」缺失')

    def test_three_tier_rendering(self):
        for key in ('pass1_final', 'pass2_skip', "'pass2'", "'llm'", 'translation'):
            self.assertIn(key, self.js)
        self.assertIn('t0_ms', self.js)   # pass1_final 字段名(与 live_session.py 一致)
        self.assertIn('ev.t0', self.js)   # pass2/llm 字段名(与 live_pass2.py 一致)

    def test_end_card_three_states(self):
        """WP6 三分态(Task 7):成功「文稿已生成」/失败「文稿生成失败,录音已保留」
        + 寻回文案/空会话「本場没有录到语音」;旧文案「正在生成完整文稿」三分支绝迹;
        结束卡结构新增「立即重试转正」按钮(hidden 默认)与就地提示槽;
        成功态 web 深链开新标签(不再本页导航)。"""
        self.assertIn('文稿已生成', self.js)
        self.assertIn('文稿生成失败,录音已保留', self.js)
        self.assertIn('本場没有录到语音', self.js)
        self.assertIn('录音仍在服务端,重启转写服务后会自动转为任务', self.js)
        self.assertNotIn('正在生成完整文稿', self.js, '三分支均不得再出现旧文案')
        self.assertNotIn('正在生成完整文稿', self.html)
        self.assertIn("window.open('/#' + jobId, '_blank')", self.js)
        retry = re.search(r'<button[^>]*id="endRetry"[^>]*>', self.html)
        self.assertTrue(retry, '结束卡缺「立即重试转正」按钮')
        self.assertIn('hidden', retry.group(0), '#endRetry 默认须 hidden')
        self.assertIn('id="endRetryNote"', self.html)

    def test_bar_and_panel_forms(self):
        self.assertIn('backdrop-filter', self.html)   # 字幕条半透明+模糊
        self.assertIn('light-dark(', self.html)       # 面板随系统深浅色
        self.assertIn('URLSearchParams', self.js)     # ?mode=bar/panel 形态参数
        self.assertIn("'bar'", self.js) and self.assertIn("'panel'", self.js)

    def test_start_failure_and_copy_fixes(self):
        """终审修复项 2/3 的静态钉子:start 阶段 error 前端停止自动重连并显示;
        语言对文案如实(参与 Pass2/LLM);stopped 带 job_id 的旧失实注释已删。"""
        self.assertIn('无法开始实时会话', self.js, 'onError 应处理 start 失败分支')
        self.assertIn("clearTimeout(state.reconnectTimer)", self.js)
        self.assertNotIn('当前版本不参与识别', self.html, '语言对文案失实(实际参与 Pass2/LLM)')
        self.assertIn('翻译目标', self.html)
        self.assertNotIn('Task 9 接入', self.js, 'stopped 无 job_id 的过期注释应删')

    def test_stop_deep_link(self):
        """停止后的完整转录记录:结束卡深链到工作台对应任务(hash 路由)。
        WP6 起 web 端 window.open 新标签(本页停在结束卡,防 /live 内导航出
        双工作台);悬浮窗内点击仍聚焦主窗而非在小窗内导航。"""
        self.assertIn("window.open('/#' + jobId, '_blank')", self.js)
        self.assertIn('openMainJob', self.js)

    def test_stale_auto_resume_falls_back_to_setup(self):
        """打开页面自动恢复死会话(服务重启过)不得死路报错卡:
        清陈旧记录、回开始表单并提示,可直接开新会话。"""
        self.assertIn('autoResume', self.js)
        self.assertIn('上次会话已因服务重启结束', self.js)
        self.assertIn('resumeNote', self.html)
        self.assertIn('会话不存在', self.js)

    def test_ja_and_target_language_selects(self):
        """spec 2026-10-24:识别语言与翻译目标拆成两个下拉(日文可选),
        「不翻译」序列化为 tgt==src,仍是 'src>tgt' 对字符串。"""
        self.assertIn('id="fSrc"', self.html)
        self.assertIn('id="fTgt"', self.html)
        self.assertIn('<option value="ja">日文</option>', self.html)
        self.assertIn('不翻译', self.html)
        self.assertIn('currentLangSpec', self.js)
        self.assertIn("src + '>' + tgt", self.js)

    def test_end_card_close_and_llm_notice(self):
        """2026-10-24 修复:结束卡可关闭(桌面隐藏悬浮窗/网页回工作台),打开文稿后
        悬浮窗隐藏;started 无 llm 密钥且选了翻译目标时显式提示(不再静默无翻译)。"""
        self.assertIn('id="endClose"', self.html)
        self.assertIn('closeLiveCaption', self.js)
        lwjs = (BASE / 'desktop' / 'live-window.js').read_text(encoding='utf-8')
        self.assertIn("ipcMain.handle('hide-live'", lwjs)
        pre = (BASE / 'desktop' / 'preload.js').read_text(encoding='utf-8')
        self.assertIn('closeLiveCaption', pre)
        self.assertIn('未配置翻译密钥', self.js)
        self.assertIn('ev.llm === false', self.js)
        app_py = (BASE / 'app.py').read_text(encoding='utf-8')
        self.assertIn("'llm': bool(getattr(session, 'llm_enabled'", app_py)

    def test_bar_log_history(self):
        """2026-10-24 句子账本化:字幕条主区(.blog)一句一行,条目 bel 与面板同源;
        主行/历史双轨(renderBar/syncBarLog)删除,回归钉子防复活。"""
        self.assertIn('id="barLog"', self.html)
        self.assertIn('.blog', self.html)
        self.assertIn('entry.bel', self.js)
        self.assertIn("autoScroll($('barLog')", self.js)
        self.assertIn('splitSentences', self.js)
        self.assertIn('makeBarEl', self.js)
        # partial 行的 js 钉子用真实语义引用($('barPartial') 是 renderActive 操控该行
        # 文本的把手),勿退回行 id 字符串——live.js 里那只在注释里出现过,语义空心。
        self.assertIn("$('barPartial')", self.js)
        self.assertNotIn('id="barMain"', self.html)
        self.assertIn('.blog .srow', self.html)
        self.assertIn('bspace', self.html)
        # 账本条目紧凑间距必须压过全局 .e 的面板 8px 边距(否则每个窗口边界吃掉一整行;
        # 终审曾连带误删,几何钉子只测条目内行高拦不住,故静态钉死)
        self.assertIn('.blog .e{margin:2px 0;padding:0', self.html)
        self.assertNotIn('syncBarLog', self.js)
        self.assertNotIn('renderBar', self.js)

    def test_static_skeleton_pins(self):
        """评审补的静态钉子(spec §6.2):账本静态骨架与降级路径在位——空态行
        html 骨架 + js 真实引用、reduced-motion 停动画、bspace 兼作悬浮窗拖动区、
        --lc- 色板变量集中、no-anim 防眨眼类 html/js 双侧存在。"""
        self.assertIn('id="barEmpty"', self.html)    # 空态「等待语音…」行骨架
        self.assertIn("$('barEmpty')", self.js)      # js 侧显隐控制的真实引用(非注释字符串)
        self.assertIn('prefers-reduced-motion', self.html)   # 系统减弱动效:入场/caret 动画停用
        self.assertTrue(any('.bspace' in ln and '-webkit-app-region:drag' in ln
                            for ln in self.html.splitlines()),
                        '.bspace 规则行须含 -webkit-app-region:drag(悬浮窗早期空窗整块可拖)')
        self.assertIn('--lc-', self.html)            # 账本灰度分层色板(改动须重验对比度)
        self.assertIn('no-anim', self.html)
        self.assertIn("classList.add('no-anim')", self.js)   # 重建路径真的加类,不是只有 CSS 没人用

    def test_floating_window_injection(self):
        """2026-10-24 交互修复(desktop/live-window.js 注入):
        表单/结束卡有拖动区;字幕条铺满窗口且按钮常显在条内;手柄拖拽对
        setPointerCapture 容错(document 级兜底)。"""
        lwjs = (BASE / 'desktop' / 'live-window.js').read_text(encoding='utf-8')
        self.assertIn('.card{-webkit-app-region:drag}', lwjs)
        self.assertIn('.ecard{-webkit-app-region:drag}', lwjs)
        self.assertIn('#bar{left:6px', lwjs)
        self.assertIn('#barLog{flex:1 1 auto', lwjs)
        self.assertIn('#barActions{top:5px', lwjs)
        self.assertIn('opacity:1 !important', lwjs)
        self.assertIn('try{ grip.setPointerCapture', lwjs)
        self.assertIn("document.addEventListener('pointermove', move, true)", lwjs)

    def test_workbench_live_entry(self):
        """主界面实时字幕入口(WP2 起为「新任务」卡下动作行,不再是 Tab)+
        拉起逻辑(桌面悬浮窗/网页命名 target 新标签页);历史列表对 live 转正
        任务显示「实时」徽标。"""
        idx = INDEX_HTML.read_text(encoding='utf-8')
        self.assertIn('id="liveRow"', idx)
        self.assertIn('openLiveCaption', idx)
        self.assertIn("window.open('/live?mode=panel', 'livePanel')", idx)
        self.assertIn('pill live', idx)


# 断言向量(spec §6.3):(输入, 期望句列表)。期望断到句内容而非只断句数——
# 「→ 2 句」防的是切多,切错位置(如小数处切)句数可能仍对,内容断言才拦得住。
SPLIT_VECTORS = [
    ('好的,先看数据。营收 320 万,涨 12%。', ['好的,先看数据。', '营收 320 万,涨 12%。']),
    ('Revenue was 3.2M, up 12% QoQ. Margins dipped.',
     ['Revenue was 3.2M, up 12% QoQ.', 'Margins dipped.']),       # 小数 3.2/12 不切
    ('See Mr. Smith. He agreed.', ['See Mr. Smith.', 'He agreed.']),   # 缩写 Mr. 不切
    ('好!!!知道了。', ['好!!!', '知道了。']),                     # 连续终符并入前一句,非 4 句
    ('这句话还没说完', ['这句话还没说完']),                        # 无终符尾巴自成一行(pass1 裸文本)
    ('', []),                                                    # 空串
    ('。。。!?!', ['。。。!?!']),                                # 纯标点:并入一行,不产孤标点行
    ('明日は晴れます。Really? はい!', ['明日は晴れます。', 'Really?', 'はい!']),   # 日文混排半角 ?!
    ('完了!次へ?了解;', ['完了!', '次へ?', '了解;']),           # 半角 !?; 对照
    # 全角 ！？; 必须在终符集(勘误 v2)。输入/期望都按码点转义构造并经用例内码点自检——
    # 手打全角会在工具链路被折成半角(Task 1 修复轮实测的坑),字面直写不可信。
    ('完了\uff01次へ\uff1f了解\uff1b',
     ['完了\uff01', '次へ\uff1f', '了解\uff1b']),
]


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断句向量用例')
class SentenceSplitCheck(unittest.TestCase):
    """spec §6.3:splitSentences 是纯函数,从 live.js 源码抠出函数体经 node 模式执行。
    提取用花括号配平而非 eval 整文件——live.js 是 IIFE 且依赖 DOM/WS,
    整文件进 node 会炸;纯函数单独执行才有确定性的断言向量。"""

    def test_vectors(self):
        # 码点自检:末位全角例的输入必须真含 U+FF01/U+FF1F/U+FF1B(手打全角曾被折半角,
        # 字面直写的「全角用例」可能整个是半角假货——审查实测发生过)
        fw_input = SPLIT_VECTORS[-1][0]
        for cp in ('\uff01', '\uff1f', '\uff1b'):
            self.assertIn(cp, fw_input, '全角对照向量缺码点 %s' % hex(ord(cp)))
        extracted = extract_function(LIVE_JS.read_text(encoding='utf-8'), 'splitSentences')
        with tempfile.TemporaryDirectory(prefix='lc_split_') as td:
            script = Path(td) / 'split.js'
            script.write_text(
                extracted + '\n'
                + 'var CASES = ' + json.dumps([v[0] for v in SPLIT_VECTORS],
                                              ensure_ascii=False) + ';\n'
                + 'var out = [];\n'
                + 'for (var i = 0; i < CASES.length; i++) out.push(splitSentences(CASES[i]));\n'
                + 'console.log(JSON.stringify(out));\n',
                encoding='utf-8')
            proc = run_electron_node(script)
        self.assertEqual(proc.returncode, 0,
                         'node 模式执行失败:%s' % proc.stderr.decode('utf-8', 'replace')[:400])
        out = json.loads(last_json_line(proc.stdout))
        self.assertEqual(len(out), len(SPLIT_VECTORS), '返回向量数不符:%r' % out)
        for (text, want), got in zip(SPLIT_VECTORS, out):
            self.assertEqual(got, want, 'splitSentences(%r) 切分错误' % text)


# 离屏测量时注入页面的取数脚本:揭开运行中舞台让静态骨架进入真实 CSS 布局,
# partial 行灌一段单行文本,并造一条与 makeBarEl 同构的句行当对照组。
GEOM_PAGE_JS = """(function(){
  var $ = function(id){ return document.getElementById(id); };
  $('stage').hidden = false;
  $('barEmpty').hidden = false;
  $('barPartial').textContent = '正在识别的滚动字幕内容示意';
  var e = document.createElement('div'); e.className = 'e f1';
  var row = document.createElement('div'); row.className = 'srow f1';
  var dot = document.createElement('i'); dot.className = 'sdot';
  var stx = document.createElement('div'); stx.className = 'stx';
  stx.textContent = '定稿句行高度对照行';
  row.appendChild(dot); row.appendChild(stx); e.appendChild(row);
  $('barLog').insertBefore(e, $('barEmpty'));
  function h(el){ return el.getBoundingClientRect().height; }
  return {prow: h($('barPartialRow')), empty: h($('barEmpty')), srow: h(row)};
})()"""


def geom_main_script(page_js):
    """临时 Electron 主进程脚本:离屏窗口加载 file:// 真实 live.html 后执行取数脚本。
    带超时兜底,防窗口事件循环挂死拖垮整个测试进程。"""
    return ("const {app, BrowserWindow} = require('electron');\n"
            "if (app.dock) app.dock.hide();\n"
            "setTimeout(function(){ console.error('geometry: 15s 超时'); app.exit(2); }, 15000);\n"
            "app.whenReady().then(function(){\n"
            "  const win = new BrowserWindow({show: false, width: 760, height: 420,\n"
            "    webPreferences: {offscreen: true}});\n"
            "  const page = " + json.dumps(page_js, ensure_ascii=False) + ";\n"
            "  win.loadFile(process.argv[2]).then(function(){\n"
            "    return win.webContents.executeJavaScript(page, false);\n"
            "  }).then(function(r){\n"
            "    console.log(JSON.stringify(r));\n"
            "    win.destroy(); app.exit(0);\n"
            "  }).catch(function(e2){\n"
            "    console.error('geometry: ' + (e2 && e2.message ? e2.message : String(e2)));\n"
            "    app.exit(1);\n"
            "  });\n"
            "});\n")


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过账本几何钉子')
class BarGeometryCheck(unittest.TestCase):
    """Task 1 复审的硬性要求:prow/empty 两静态行必须退出 .srow 的 grid——
    10px 固定列会把纯文本竖排成一字一行。此前防线只在 /private/tmp 临时脚本里,
    随清理即失;此处离屏加载真实 live.html 实测行高,落成持久回归钉子:
    两静态行高度 >0(在布局,非 display:none 假绿)且 ≤30px(单行 13.5px×1.6≈22px,
    grid 回归时 13 字竖排 ≈280px 必炸),并与正常句行同高(对照,防上下文未布局的假绿)。"""

    def test_static_row_heights(self):
        with tempfile.TemporaryDirectory(prefix='lc_geom_') as td:
            script = Path(td) / 'geom.js'
            script.write_text(geom_main_script(GEOM_PAGE_JS), encoding='utf-8')
            proc = subprocess.run([str(ELECTRON), str(script), str(LIVE_HTML)],
                                  capture_output=True, timeout=90, cwd=str(REPO))
        self.assertEqual(proc.returncode, 0,
                         '离屏测量失败:%s' % proc.stderr.decode('utf-8', 'replace')[:400])
        r = json.loads(last_json_line(proc.stdout))
        srow = r['srow']
        self.assertTrue(0 < srow <= 30,
                        '对照句行高度 %.1f 越界 (0,30]:测量上下文疑似未真实布局' % srow)
        for key, name in (('prow', 'partial 行'), ('empty', '空态行')):
            h = r[key]
            self.assertTrue(0 < h <= 30, '%s高度 %.1f 越界 (0,30]:'
                            '疑似退回 grid 被 10px 固定列竖排' % (name, h))
            self.assertLessEqual(abs(h - srow), 3, '%s高度 %.1f 与句行 %.1f 不一致:'
                                 '两静态行应与句行同高(单行文本)' % (name, h, srow))
        print('几何钉子 ok: prow=%.1fpx empty=%.1fpx srow=%.1fpx' %
              (r['prow'], r['empty'], r['srow']))


@unittest.skipIf(port_busy(), '端口 8765 已被占用(服务可能正在跑),跳过协议段')
class ProtocolFlow(unittest.TestCase):
    """模拟前端的完整 WS 时序,断言服务端真实行为与 live.js 实现的对齐点。"""

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([sys.executable, 'app.py'], cwd=str(BASE),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            wait_port(cls.proc)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def test_full_flow(self):
        pcm = load_pcm_bytes()
        ws = websocket.create_connection('ws://127.0.0.1:%d/ws/live' % PORT, timeout=60)

        # ① start → started(无 seq 字段;session_id 供刷新后 resume)
        ws.send(json.dumps({'type': 'start', 'title': '前端自测', 'speakers': 2,
                            'language': 'zh>en'}))          # language v1 被服务端忽略,不得报错
        started = json.loads(ws.recv())
        self.assertEqual(started.get('type'), 'started')
        self.assertTrue(started.get('session_id'))
        sid = started['session_id']
        self.assertNotIn('seq', started)

        # ② 喂 30s 音频(200ms/包二进制帧),期间应出 partial 与 pass1_final
        ws.settimeout(90)
        fed = bytearray()
        partials, finals, others = 0, 0, []
        deadline = time.time() + 90
        feed_iter = range(0, len(pcm), CHUNK)
        for i in feed_iter:
            ws.send_binary(pcm[i:i + CHUNK])
            fed += pcm[i:i + CHUNK]
        while time.time() < deadline and (partials < 3 or finals < 1):
            ev = json.loads(ws.recv())
            t = ev.get('type')
            if t == 'partial':
                partials += 1
                self.assertIn('text', ev)
                self.assertIn('seq', ev)
            elif t == 'pass1_final':
                finals += 1
                for k in ('text', 't0_ms', 't1_ms', 'seq'):   # 字段名 t0_ms/t1_ms
                    self.assertIn(k, ev)
            else:
                others.append(t)
        self.assertGreaterEqual(partials, 3, '90s 内 partial 不足:%d' % partials)
        self.assertGreaterEqual(finals, 1, '90s 内 pass1_final 不足:%d' % finals)

        # ③ 断线重连:resume(last_seq=0,模拟刷新页面全量重建)
        ws.close()
        time.sleep(0.5)
        ws = websocket.create_connection('ws://127.0.0.1:%d/ws/live' % PORT, timeout=60)
        ws.send(json.dumps({'type': 'resume', 'session_id': sid, 'last_seq': 0}))
        replayed, replay_types = 0, set()
        ws.settimeout(15)
        try:
            while True:                                     # 重放批次以断流或新事件间隔告终,15s 足够
                ev = json.loads(ws.recv())
                self.assertNotEqual(ev.get('type'), 'resumed',
                                    '服务端实现无 resumed 消息,若出现说明协议变了,前端需同步')
                replayed += 1
                replay_types.add(ev.get('type'))
                if replayed >= 5 and ev.get('type') == 'partial':
                    break
        except websocket.WebSocketTimeoutException:
            pass
        self.assertGreater(replayed, 0, 'resume 未重放任何事件')

        # ④ 补发音频 + audio_flush_done(服务端当前忽略该消息,不得断连/报错)
        ws.send_binary(pcm[:CHUNK * 2])
        ws.send(json.dumps({'type': 'audio_flush_done'}))
        ws.settimeout(10)
        stayed_open = True
        try:
            ev = json.loads(ws.recv())                       # 收到任何事件即连接仍健康
            self.assertNotEqual(ev.get('type'), 'error', ev)
        except websocket.WebSocketTimeoutException:
            pass
        except Exception:
            stayed_open = False
        self.assertTrue(stayed_open, 'audio_flush_done 后连接异常关闭')

        # ⑤ stop → stop_ack 先于 stopped;Task 9 起 stopped 带 job_id(转正异步:
        #    stop_ack 即回,stopped 隔着尾窗 whisper + LLM 排空 + 建 job)
        ws.send(json.dumps({'type': 'stop'}))
        ws.settimeout(180)
        seen_ack = seen_stopped = False
        flush_finals = 0
        job_id = None
        while not seen_stopped:
            ev = json.loads(ws.recv())
            t = ev.get('type')
            if t == 'stop_ack':
                seen_ack = True
                self.assertIn('seq', ev)
            elif t == 'pass1_final':
                flush_finals += 1                            # 引擎尾部冲刷的 final 可晚于 stop_ack
            elif t == 'stopped':
                self.assertTrue(seen_ack, 'stopped 先于 stop_ack 到达')
                seen_stopped = True
                job_id = ev.get('job_id')
                self.assertTrue(job_id, 'Task 9 起 stopped 应带 job_id')
        # stopped 之后服务端主动断开(前端据此不再重连)
        closed = False
        try:
            ws.settimeout(10)
            while True:
                if ws.recv() is None:
                    closed = True
                    break
        except Exception:
            closed = True
        self.assertTrue(closed)
        cleanup_job(job_id)
        print('协议流程 ok: partials=%d finals=%d(含尾部冲刷 %d) resume 重放=%d 事件 job=%s' %
              (partials, finals, flush_finals, replayed, job_id))

    def test_stop_after_reconnect(self):
        """断线期按「停止」:重连后必须 resume→补发→stop(修复回归)。

        模拟修复后 live.js 的 onopen userStop 分支线上顺序:resume → 二进制补发 →
        audio_flush_done → stop。断言:stop_ack 与 stopped 均在本连接到达、顺序正确、
        服务端随后断开;Task 9 起停止即异步转正:live 目录删除、jobs/<job_id>/
        source.wav 读回 ≈15s(硬链接,头部长度已回填)。
        旧 pump 竞态(断连后 ≤0.5s 重连吞 stop_ack)已由 Task 9 EventTap 修复,
        此处 1s 等待保留——对齐真实前端 3s 重连节奏的保守下限。"""
        pcm = load_pcm_bytes()
        seg_a, seg_b = pcm[:12 * 32000], pcm[12 * 32000:15 * 32000]   # 12s + 3s,不重发
        ws = websocket.create_connection('ws://127.0.0.1:%d/ws/live' % PORT, timeout=60)
        ws.send(json.dumps({'type': 'start', 'title': '停止重连自测', 'speakers': 1}))
        sid = json.loads(ws.recv())['session_id']
        for i in range(0, len(seg_a), CHUNK):
            ws.send_binary(seg_a[i:i + CHUNK])
        last_seq, partials = 0, 0
        ws.settimeout(20)
        deadline = time.time() + 20
        while time.time() < deadline and partials < 2:                 # 确认会话确实在出事件
            ev = json.loads(ws.recv())
            if ev.get('type') == 'partial':
                partials += 1
            if isinstance(ev.get('seq'), int):
                last_seq = max(last_seq, ev['seq'])
        self.assertGreaterEqual(partials, 1, '连接1 未收到 partial')
        ws.close()                                                    # 模拟断线(音频 B 视为断线期缓冲)
        # 等 1s 对齐真实前端重连节奏(前端 RECONNECT_MS=3000);旧 pump 吞事件的竞态
        # 已由 Task 9 的 EventTap 修复(事件锁内归还队首),此处不再是竞态复现场景。
        time.sleep(1.0)

        ws = websocket.create_connection('ws://127.0.0.1:%d/ws/live' % PORT, timeout=60)
        ws.send(json.dumps({'type': 'resume', 'session_id': sid, 'last_seq': last_seq}))
        for i in range(0, len(seg_b), CHUNK):                         # flushPending 补发
            ws.send_binary(seg_b[i:i + CHUNK])
        ws.send(json.dumps({'type': 'audio_flush_done'}))
        ws.send(json.dumps({'type': 'stop'}))                         # 随后 stop

        ws.settimeout(180)
        ack = stopped = None
        while not stopped:
            ev = json.loads(ws.recv())
            t = ev.get('type')
            if t == 'stop_ack':
                ack = ev
            elif t == 'error':
                self.fail('停止重连路径收到 error:%s' % ev)
            elif t == 'stopped':
                self.assertIsNotNone(ack, 'stopped 先于 stop_ack')
                self.assertGreater(ev['seq'], ack['seq'])
                stopped = ev
        self.assertIsNotNone(ack, '未收到 stop_ack(会话未挂回,stop 落空)')
        closed = False
        try:
            ws.settimeout(10)
            while ws.recv() is not None:
                pass
        except Exception:
            closed = True
        self.assertTrue(closed, 'stopped 后服务端未断开')

        # Task 9 起:停止即转正——live 目录删除,音频以硬链接进 jobs/<job_id>/source.wav
        self.assertTrue(stopped.get('job_id'), 'stopped 应带 job_id')
        self.assertFalse((BASE / 'live' / sid).exists(), '转正后 live 目录应删除')
        src = BASE / 'jobs' / stopped['job_id'] / 'source.wav'
        self.assertTrue(src.is_file(), '转正产物 source.wav 不存在')
        n = len(app.read_wav_float(str(src)))
        self.assertGreaterEqual(n, int(15 * 16000 * 0.98),
                                'source.wav 仅 %.1fs,会话疑似悬死未收尾' % (n / 16000))
        cleanup_job(stopped['job_id'])
        print('停止重连路径 ok: stop_ack=seq%s stopped=seq%s job=%s audio=%.1fs' %
              (ack['seq'], stopped['seq'], stopped['job_id'], n / 16000))


if __name__ == '__main__':
    unittest.main(verbosity=2)
