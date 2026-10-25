# -*- coding: utf-8 -*-
"""WP1 路由与视图状态机静态钉子(Task 3,spec §6「静态钉子」层)。

对 transcriber/static/index.html 源码做字符串/正则断言,无浏览器、无服务即可跑:
  - hash 路由机制在位:hashchange 与 popstate 双监听、applyRoute 纯函数存在;
  - 单一写路径:程序化导航只许 location.hash 赋值,pushState/replaceState 绝迹;
  - 单一视图状态:view 取代 current/libView;
  - loadSeq 代际守卫;
  - renderEmpty 合一后含完整元素集合(SVG/h1/p/acts/meas),不再丢「实测参考」行;
  - 说话人库激活态:.side-foot.on 样式被 JS 真实使用、lib-cnt 被赋值;
  - 死代码清理:#setrow-lock 守卫移除;
  - 深链契约:#<job_id> 的 id 字符集正则不变(桌面端 loadURL('/#'+id) 依赖)。

另含 applyRoute 断言向量的 pytest 包装:实际向量与提取逻辑在
test_ui_route_vectors.mjs(独立可跑),此处只负责用 Electron 的 node 模式
拉起它并断言退出码(二进制缺失时 skip,与 live_page_selftest 同款协议)。
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
INDEX = BASE / 'static' / 'index.html'
LIVE_HTML = BASE / 'static' / 'live.html'
LIVE_JS = BASE / 'static' / 'live.js'
VECTORS = BASE / 'tests' / 'test_ui_route_vectors.mjs'
AGENTS = BASE.parent / 'AGENTS.md'


def _js_fn(src, name):
    """抠出 JS 源里 `function name(...) {…}` 的函数体(花括号配平,先例同款);
    找不到函数返回 None。"""
    m = re.search(r'function %s\s*\(' % re.escape(name), src)
    if not m:
        return None
    start = src.index('{', m.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return None


def _find_electron():
    """.tools/electron 是未跟踪目录,只存在于主检出;git worktree 里没有。
    从本目录向上逐级找主树的二进制(本仓库 worktree 与主树同根),找不到再 skip——
    与 live_page_selftest「二进制缺失→跳过」同款协议。"""
    for parent in [BASE] + list(BASE.parents):
        cand = parent / '.tools' / 'electron' / 'Electron.app' / 'Contents' / 'MacOS' / 'Electron'
        if cand.is_file():
            return cand
    return None


ELECTRON = _find_electron()


def electron_missing():
    return not ELECTRON.is_file()


class RouteStaticPins(unittest.TestCase):
    """index.html 源码静态钉子。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX.read_text(encoding='utf-8')

    def test_hashchange_and_popstate_listeners(self):
        """hash 路由双入口:hashchange(程序化赋值/手改 hash)与 popstate(返回键)
        都要接线——修的是「启动只读一次 hash,此后再无响应」的机制性坏死。"""
        self.assertIn("addEventListener('hashchange'", self.src)
        self.assertIn("addEventListener('popstate'", self.src)

    def test_apply_route_pure_function_exists_and_is_wired(self):
        self.assertRegex(self.src, r'function applyRoute\s*\(')
        # 监听器走同一个处理函数,处理函数内用 location.hash 调纯函数
        self.assertIn('applyRoute(location.hash)', self.src)

    def test_single_write_path_no_history_api(self):
        """程序化导航一律 location.hash 赋值;pushState/replaceState 与 hash 赋值
        并存必然双处理或漏处理(eng-review 单写路径决议),一个都不许留。"""
        self.assertNotIn('pushState', self.src)
        self.assertNotIn('replaceState', self.src)
        self.assertIn("location.hash = '#' +", self.src)   # 进任务视图
        self.assertIn("location.hash = '#'", self.src)     # 清回空态/出库

    def test_view_single_state_no_current_no_libview(self):
        """单一视图状态:view = {type,id?};旧的平行变量 current/libView 删除。
        (\\bcurrent\\b 不会误伤 audio.currentTime——无词边界。)"""
        self.assertRegex(self.src, r'let [^\n]*\bview = null')
        self.assertNotIn('libView', self.src)
        self.assertIsNone(re.search(r'\bcurrent\b', self.src),
                          '视图状态应已统一为 view,不得残留 current 变量')

    def test_loadseq_guard(self):
        """load() 为 async 且无 in-flight 保护,hash 化后快速连点两任务会慢者后到
        覆盖新视图——代际守卫(loadSeq)必须在 fetch 前快照、render 前校验。"""
        self.assertRegex(self.src, r'let loadSeq = 0')
        self.assertIn('++loadSeq', self.src)

    def test_render_empty_complete_element_set(self):
        """renderEmpty 合一为唯一实现,补齐波形 SVG 与「实测参考」行
        (与初始标记 443-462 同元素集合:SVG/h1/p/acts/meas)。"""
        m = re.search(r'function renderEmpty\s*\(', self.src)
        self.assertTrue(m, 'renderEmpty 缺失')
        start = self.src.index('{', m.end())
        depth = 0
        end = -1
        for i in range(start, len(self.src)):
            if self.src[i] == '{':
                depth += 1
            elif self.src[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        self.assertTrue(end > 0, 'renderEmpty 花括号未配平')
        body = self.src[start:end]
        self.assertIn('viewBox="0 0 22 22"', body, '空态须带波形 SVG(曾劣化丢失)')
        self.assertIn('<h1>', body)
        self.assertIn('<p>', body)
        self.assertIn('class="acts"', body)
        self.assertIn('class="meas"', body)
        self.assertIn('实测参考', body, '「实测参考」行曾劣化丢失')
        self.assertIn('按下开始录制,录音会出现在这里', body, 'record 变体文案缺失')

    def test_side_foot_on_actually_used(self):
        """.side-foot.on 曾是死样式(JS 从未加过)——现在必须被 libbtn 真实切换。"""
        self.assertIn('.side-foot.on', self.src)
        self.assertIn("$('#libbtn').classList.toggle('on'", self.src)

    def test_lib_cnt_wired(self):
        """#lib-cnt 从未被赋值过(死元素)——现在由 ensureSpeakers 的 suspects.length
        填充,0 时隐藏(空串)。"""
        self.assertIn("$('#lib-cnt').textContent", self.src)
        self.assertIn('suspects', self.src)

    def test_setrow_lock_dead_guard_removed(self):
        self.assertNotIn('setrow-lock', self.src,
                         '#setrow-lock 引用从未创建的元素,是死守卫,应删')

    def test_deep_link_id_charset_unchanged(self):
        """/#<job_id> 深链是桌面端已发版契约;id 字符集正则 ^[\\w-]+$ 不得收紧或放宽。"""
        self.assertRegex(self.src, r'\^\[\\w\-\]\+\$')

    def test_showlib_generation_guard(self):
        """showLib 与 load 同款代际守卫(评审 Important):showLib 是 render 调度的
        渲染器之一,await ensureSpeakers(true) 期间切到任务视图时,慢响应后到
        不得把主区覆盖回库视图——守卫必须位于 await 之后、innerHTML 之前。"""
        m = re.search(r'async function showLib\s*\(', self.src)
        self.assertTrue(m, 'showLib 缺失')
        start = self.src.index('{', m.end())
        depth = 0
        end = -1
        for i in range(start, len(self.src)):
            if self.src[i] == '{':
                depth += 1
            elif self.src[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        self.assertTrue(end > 0, 'showLib 花括号未配平')
        body = self.src[start:end]
        i_await = body.find('await ensureSpeakers(true)')
        i_guard = body.find("view.type !== 'lib'")
        i_render = body.find('main.innerHTML')
        self.assertGreaterEqual(i_await, 0, 'showLib 应强拉库数据')
        self.assertGreaterEqual(i_guard, 0, 'showLib 缺代际守卫(view.type !== \'lib\')')
        self.assertIn('loadSeq', body, 'showLib 缺 loadSeq 代际比较')
        self.assertGreaterEqual(i_render, 0)
        self.assertLess(i_await, i_guard, '守卫必须在 await 之后')
        self.assertLess(i_guard, i_render, '守卫必须在 innerHTML 渲染之前')

    def test_route_renders_via_single_render_entry(self):
        """主区渲染收口:路由处理函数进 render(view);renderEmpty/showLib/load
        三者都在 render 内被调度。"""
        self.assertRegex(self.src, r'function render\s*\(')
        body = self.src[self.src.index('function render('):]
        # 只看 render 函数之后的路由调度段是否引用三个渲染器(存在性钉子)
        for fn in ('renderEmpty', 'showLib', 'load('):
            self.assertIn(fn, body)


class LiveActionRowPins(unittest.TestCase):
    """WP2 实时字幕动作行静态钉子(Task 4,spec §6「静态钉子」层)。

    实时字幕入口从第三 Tab 降为「新任务」卡下的常驻动作行 #liveRow:
      - 旧入口(tab-live/pane-live/live-go 及其面板 CSS)零残留;
      - web 分支必须用命名 target 'livePanel'(无命名 target 的 _blank 双开标签
        是 /live 会话劫持的前端半边);
      - 动作行状态由 GET /api/live/state 驱动:独立常驻慢定时器(页面可见时
        10~15s 一次)+ visibilitychange 可见即刷,不动任务轮询(pollTimer)体系;
      - 文档同步:AGENTS.md 界面节不再描述三 Tab(宽松钉:只禁「三个 Tab」字样,
        具体措辞不钉,防脆)。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX.read_text(encoding='utf-8')
        cls.agents = AGENTS.read_text(encoding='utf-8')

    def test_named_window_target_livepanel(self):
        """web 分支 window.open 必须带命名 target 'livePanel':重复点击聚焦同一
        标签而非再开一个(第二个标签 resume 会话即劫持,spec WP2-2/WP3 前提)。"""
        self.assertIn("window.open('/live?mode=panel', 'livePanel')", self.src)
        self.assertNotIn("window.open('/live?mode=panel', '_blank')", self.src,
                         '匿名 _blank 双开标签=会话劫持入口,必须命名')

    def test_old_live_tab_zero_residue(self):
        """第三 Tab 及其面板/处理器/CSS 类名全部清场:动作行取代之。"""
        for token in ('tab-live', 'pane-live', 'live-go', 'live-entry', 'live-desc'):
            self.assertNotIn(token, self.src, '旧实时字幕 Tab 残留:' + token)

    def test_live_row_element_with_aria_live(self):
        """动作行 DOM 在位:button(键盘免费可达)+ 行内文案 aria-live 可感知
        (状态两态切换时屏幕阅读器播报)。"""
        m = re.search(r'<button[^>]*id="liveRow"[^>]*>', self.src)
        self.assertTrue(m, '缺 #liveRow 动作行')
        window = self.src[m.start():m.start() + 700]   # 行内三个子元素都在近旁
        self.assertIn('aria-live', window, '行内文案缺 aria-live(状态变化须可感知)')

    def test_live_state_api_wired(self):
        """GET /api/live/state(Task 1 契约)被 index.html 真实消费。"""
        self.assertIn('/api/live/state', self.src)

    def test_live_state_slow_timer_and_visibility(self):
        """独立常驻慢定时器:页面可见时 10~15s 一拉(独立于任务轮询 pollTimer——
        无 running 任务时 pollTimer 不存在,动作行状态不能跟着死);visibilitychange
        可见即刷。"""
        self.assertRegex(self.src, r'setInterval\(\s*liveTick\s*,\s*(?:10|11|12|13|14)\d{3}\s*\)')
        self.assertIn("addEventListener('visibilitychange'", self.src)

    def test_tick_bumps_live_state(self):
        """任务轮询 tick 顺带刷新一次字幕会话状态(running 期 2.5s 节奏,比慢定时
        更快看到红点熄灭/点亮)。"""
        m = re.search(r'async function tick\s*\([^)]*\)\s*\{', self.src)
        self.assertTrue(m, 'tick 缺失')
        body = self.src[m.end():m.end() + 400]
        self.assertIn('refreshLiveState', body, 'tick 应顺带刷新字幕会话状态')

    def test_tab_list_two_entries(self):
        """TAB_LIST 收敛为 upload/record 两项;tab-live 处理器随之绝迹(已被
        test_old_live_tab_zero_residue 覆盖,此处钉机制本身)。"""
        self.assertIn("const TAB_LIST = [['up','upload'], ['rec','record']];", self.src)

    def test_agents_md_no_three_tab_wording(self):
        """AGENTS.md 界面节须随动作行落地同步(宽松钉):不得再出现「三个 Tab」
        字样;须提及动作行元素。具体措辞不钉,防文档微调即碎。"""
        self.assertNotIn('三个 Tab', self.agents)
        self.assertIn('liveRow', self.agents)


class WP4LivePrivacyPins(unittest.TestCase):
    """WP4 翻译默认与隐私文案静态钉子(Task 5,spec WP4 全节 + WP7 表 7.8)。

    翻译默认从「英文」改「不翻译」:外发必须显式选择(隐私立身产品的默认值纪律);
    旧默认 zh>en 一次性迁移,表单恢复与 resume 两条读取路径都要覆盖;note 随语言对
    动态切换;无修正会话(started.llm===false 或 tgt==src)f1 句升定稿色(llm-off
    根类);运行中 hint 只说音频,准确,原样保留。"""

    @classmethod
    def setUpClass(cls):
        cls.html = LIVE_HTML.read_text(encoding='utf-8')
        cls.js = LIVE_JS.read_text(encoding='utf-8')

    def _ftgt_block(self):
        m = re.search(r'<select id="fTgt">(.*?)</select>', self.html, re.S)
        self.assertTrue(m, '缺 #fTgt 下拉')
        return m.group(1)

    def test_ftgt_default_no_translate(self):
        """fTgt 默认选中 value=""(不翻译):selected 落在空值项上,英文项不得再带
        (旧默认残留)。只动 selected,选项集不动。"""
        block = self._ftgt_block()
        self.assertRegex(block, r'<option value=""[^>]*\bselected')
        self.assertNotRegex(block, r'<option value="en"[^>]*\bselected',
                            '「英文」仍是默认=外发仍是默认,违背 WP4 决策')

    def test_note_dynamic_two_texts_and_instant_switch(self):
        """note 两段精确文案(WP4 照抄,含标点)在 live.js;updateLangNote 存在且
        fTgt change 即时切换。fSrc 变化同样会翻转 tgt==src 语义,须一并接线。"""
        self.assertIn('识别与说话人分离即时可用,全程在本机完成;修正与翻译事件到达时自动上屏。',
                      self.js, '不翻译侧文案缺失或被改写')
        self.assertIn('识别与说话人分离在本机完成;修正与翻译会把窗口文本(不含音频、路径、人名)发送至外部 LLM 服务。',
                      self.js, '翻译侧文案缺失或被改写')
        self.assertRegex(self.js, r'function updateLangNote\s*\(')
        self.assertIsNotNone(_js_fn(self.js, 'updateLangNote'))
        self.assertIn("$('fTgt').addEventListener('change'", self.js)
        self.assertIn("$('fSrc').addEventListener('change'", self.js)

    def test_stop_chip_llm_false_hint_untouched(self):
        """既有「未配置翻译密钥」stopChip 提示(started.llm===false 且 tgt≠src)保留。"""
        self.assertIn('ev.llm === false', self.js)
        self.assertIn('未配置翻译密钥', self.js)

    def test_legacy_default_migration_both_read_paths(self):
        """旧默认迁移:migrateLegacyLang 存在且做 removeItem('live.lang');
        langFromStorage(表单恢复)与 tryAutoResume(resume)两条读取路径都调用。"""
        self.assertRegex(self.js, r'function migrateLegacyLang\s*\(')
        self.assertIn("localStorage.removeItem('live.lang')", self.js)
        for fn in ('langFromStorage', 'tryAutoResume'):
            body = _js_fn(self.js, fn)
            self.assertIsNotNone(body, fn + ' 缺失')
            self.assertIn('migrateLegacyLang()', body, fn + ' 未走旧默认迁移')

    def test_llm_off_css_bar_and_panel(self):
        """7.8 f1 升白:llm-off 下账本侧(f1 句 .stx)与面板侧(f1 条 .txt)选择器
        都存在且生效色含 var(--lc-txt);选择器结构对齐真实 DOM 类名
        (账本行 'srow f1' > .stx,面板条 'e f1' > .txt,均由 makeBarEl/makeEntryEl 拼出)。"""
        self.assertRegex(self.html, r'\.llm-off \.blog \.srow\.f1 \.stx\{[^}]*var\(--lc-txt\)')
        self.assertRegex(self.html, r'\.llm-off \.pbody \.e\.f1 \.txt\{[^}]*var\(--lc-txt\)')
        self.assertIn("document.documentElement.classList.toggle('llm-off'", self.js)
        # 两个加类时机:started(fresh start,拿得到 llm 旗标)与 tryAutoResume(resume 无
        # started,按语言对判)。译文层 --lc-tr 不动(钉在 f1 句文本层,不含 .str/.tr)。
        self.assertRegex(self.js, r"(?s)case 'started':.*?applyLlmOff")
        resume_body = _js_fn(self.js, 'tryAutoResume')
        self.assertIsNotNone(resume_body)
        self.assertIn('applyLlmOff(', resume_body)

    def test_stage_hint_audio_only_unchanged(self):
        """:350 运行中 hint 只说音频,准确,原样保留(不许被隐私文案改波及)。"""
        self.assertIn('字幕运行中 · 拖动顶部条移动 · 所有音频仅在本机处理', self.html)

    def test_sub_privacy_copy_updated(self):
        """:262 sub 统一为新口径「音频不出本机;开启翻译时…」;旧「不经任何外部服务」
        从 live 页绝迹(hint 的「仅在本机」只指音频,允许保留,由上一钉看住)。"""
        self.assertIn('音频不出本机;开启翻译时,识别文本交由外部 LLM 修正与翻译', self.html)
        self.assertNotIn('不经任何外部服务', self.html)


class WP3LiveGuardPins(unittest.TestCase):
    """WP3 会话守卫前端半边 + 7.3/7.14 快修静态钉子(Task 6,spec WP3 前端部分 +
    WP7 表 7.3/7.14 行)。

    四件事,全部钉在 live.js/live.html 源码字面:
      1. 单实例锁:'live-session' Web Locks,beginSession 与 tryAutoResume 双入口
         都先过锁闸再 startCapture(锁不可得不碰 getUserMedia);无 locks 环境降级
         (服务端 resume 接管守卫兜底);终态释放路径在位;
      2. session_taken_over 终态:停重连/停麦/清存储/放锁/回表单提示;
      3. 7.3 beforeunload:connecting/live/stopping 三相位白名单拦截,桌面悬浮窗
         形态(window.desk)跳过——主进程已有关窗拦截;
      4. 7.14 重连终态:计数 >10 停止自动重连,chip 终态文案可点(role=button)
         手动 connect() 并清零计数。"""

    @classmethod
    def setUpClass(cls):
        cls.html = LIVE_HTML.read_text(encoding='utf-8')
        cls.js = LIVE_JS.read_text(encoding='utf-8')

    def test_single_instance_lock_dual_entry(self):
        """锁名/能力探测/ifAvailable 三要素在位;两个会话入口都先 acquireLiveLock
        再 startCapture——锁闸在麦克风之前,锁被占的路径绝不触发 getUserMedia。"""
        self.assertIn("'live-session'", self.js, '锁名字面缺失')
        self.assertIn("'locks' in navigator", self.js, '无 Web Locks 环境的降级探测缺失')
        self.assertIn('服务端守卫兜底', self.js, '降级缘由注释缺失(服务端接管语义兜底)')
        self.assertRegex(self.js, r'navigator\.locks\.request\(')
        self.assertRegex(self.js, r'ifAvailable:\s*true')
        self.assertIsNotNone(_js_fn(self.js, 'acquireLiveLock'), 'acquireLiveLock 缺失')
        for fn in ('beginSession', 'tryAutoResume'):
            body = _js_fn(self.js, fn)
            self.assertIsNotNone(body, fn + ' 缺失')
            self.assertIn('acquireLiveLock()', body, fn + ' 未先取单实例锁')
            self.assertLess(body.index('acquireLiveLock()'), body.index('startCapture()'),
                            fn + ' 锁闸必须在 startCapture 之前(占用时不触发 getUserMedia)')

    def test_lock_busy_copy_and_release_paths(self):
        """锁不可得的表单提示文案;句柄存入 state.lock 供终态释放,releaseLiveLock
        存在且终态路径(stopped/会话失效/无法开始/taken_over/停止未建会话/采集失败)
        全数调用——调用点计数给下限,防新增终态漏放锁。"""
        self.assertIn('字幕会话已在其他窗口进行', self.js, '锁被占的表单提示文案缺失')
        self.assertIsNotNone(_js_fn(self.js, 'releaseLiveLock'), 'releaseLiveLock 缺失')
        self.assertIn('state.lock = handle', self.js, '锁句柄未存入 state(终态无从释放)')
        self.assertGreaterEqual(self.js.count('releaseLiveLock()'), 6,
                                'releaseLiveLock 调用点过少:终态路径疑似漏放锁')

    def test_session_taken_over_terminal(self):
        """终态消息分发 + onTakenOver 收尾五件套:置 ended(防 onclose 再重连)、
        停麦、清 session 存储、放锁、回表单提示「会话已在其他窗口打开」。"""
        self.assertIn("case 'session_taken_over'", self.js, '消息分发缺失')
        body = _js_fn(self.js, 'onTakenOver')
        self.assertIsNotNone(body, 'onTakenOver 缺失')
        for token in ("setPhase('ended')", 'stopCapture()', 'clearSessionStorage()',
                      'releaseLiveLock()', 'clearTimeout(state.reconnectTimer)',
                      '会话已在其他窗口打开'):
            self.assertIn(token, body, 'onTakenOver 缺终态动作:' + token)

    def test_onclose_terminal_short_circuit_stays(self):
        """taken_over 后服务端随即断连接,onclose 必然跟着到——ended/idle 相位的
        重连短路守卫必须在位(否则终态又进 scheduleReconnect,A/B 双页互踢复活)。"""
        self.assertRegex(self.js,
                         r"if\s*\(\s*state\.phase === 'ended' \|\|\s*state\.phase === 'idle'\s*\)\s*return")

    def test_beforeunload_three_phase_with_desk_skip(self):
        """7.3:connecting/live/stopping 三相位白名单拦截(ended/idle 不拦);
        桌面悬浮窗形态 window.desk 跳过——主进程已有关窗拦截,双拦会弹两次。"""
        m = re.search(r"addEventListener\('beforeunload',\s*function\s*\(e\)\s*\{(.*?)\}\);\s*\n",
                      self.js, re.S)
        self.assertTrue(m, 'beforeunload 拦截缺失')
        body = m.group(1)
        self.assertIn('window.desk', body, '桌面悬浮窗形态须跳过')
        for ph in ('connecting', 'live', 'stopping'):
            self.assertIn("'" + ph + "'", body, '拦截相位缺 ' + ph)
        self.assertIn("state.phase !== 'connecting'", body, '须白名单式否定三连(ended/idle 不拦)')
        self.assertIn('preventDefault', body)
        self.assertIn("e.returnValue = ''", body)

    def test_reconnect_terminal_and_clickable_chip(self):
        """7.14:阈值 10、超限进终态;终态文案字面;chip 升级 role=button + tabindex
        可点;manualReconnect 清零计数并 connect();连接成功(onopen)清零解除终态;
        可点样式落在 live.html。"""
        self.assertIn('reconnectTries', self.js)
        self.assertRegex(self.js, r'RECONNECT_MAX_TRIES\s*=\s*10\b')
        self.assertIn('reconnectTries > RECONNECT_MAX_TRIES', self.js, '超限判断缺失')
        self.assertIn('无法连接转写服务,请确认服务已启动 · 点击重试', self.js, '终态文案缺失')
        self.assertIsNotNone(_js_fn(self.js, 'enterReconnectFinal'), 'enterReconnectFinal 缺失')
        body = _js_fn(self.js, 'manualReconnect')
        self.assertIsNotNone(body, 'manualReconnect(终态手动重试)缺失')
        self.assertIn('state.reconnectTries = 0', body)
        self.assertIn('connect()', body)
        self.assertIn("setAttribute('role', 'button'", self.js, 'chip 须升级按钮语义(可点)')
        self.assertIn("setAttribute('tabindex', '0'", self.js)
        conn = _js_fn(self.js, 'connect')
        self.assertIsNotNone(conn)
        self.assertIn('reconnectTries = 0', conn, '连接成功须清零计数并解除终态')
        self.assertRegex(self.html, r'\.chip\.clickable\{[^}]*cursor:\s*pointer',
                         '可点 chip 缺指针样式')

    def test_taken_over_state_reset(self):
        """评审 Important(静态半边,行为半边见 WP3TakenOverReopenVector):onTakenOver
        须复位页内单会话状态 sessionId/userStop/lastSeq——goBtn 已重启允许不刷新直接
        重开,三残留任一都会让重开路径坏死(误发 resume 死会话 / 误发 resume+stop /
        新会话小 seq 被去重全吞)。"""
        body = _js_fn(self.js, 'onTakenOver')
        self.assertIsNotNone(body, 'onTakenOver 缺失')
        self.assertIn('state.sessionId = null', body)
        self.assertIn('state.userStop = false', body)
        self.assertIn('state.lastSeq = 0', body)

    def test_begin_session_no_shared_writes_before_lock(self):
        """评审 Minor 1+2:锁闸之前不得写共享 localStorage(占用路径污染共享键,
        第三窗口 autoResume 会取到一次失败尝试的 startedAt);页内重开须清上一场
        残留的重连计数与终态(Important 修复后页内重入变多)。"""
        body = _js_fn(self.js, 'beginSession')
        self.assertIsNotNone(body, 'beginSession 缺失')
        self.assertLess(body.index('acquireLiveLock()'),
                        body.index("localStorage.setItem('live.title'"),
                        '共享键写入必须在锁到手之后(占用路径不得污染 localStorage)')
        self.assertIn('state.reconnectTries = 0', body, '重入未清重连计数')
        self.assertIn('disarmReconnectChip()', body, '重入未解除重连终态')

    def test_reconnect_final_bar_mirror_copy(self):
        """评审 Minor 3:barChip 终态镜像须含「点击重试」——web 叠加态默认只见
        字幕条,面板侧文案的恢复动作提示必须在镜像侧同在。"""
        self.assertIn('连接中断 · 已停止自动重连 · 点击重试', self.js)

    def test_self_audit_fixes_pinned(self):
        """自审轮三缺陷补钉(防回退):disarm 连字幕条镜像一并退场;armChipButton
        属性设置先于 __armed 守卫(disarm 后可复设)、监听器只挂一次;manualReconnect
        点击后即时切回重连中文案。"""
        disarm = _js_fn(self.js, 'disarmReconnectChip')
        self.assertIsNotNone(disarm)
        self.assertIn("'barChip'", disarm, 'disarm 须连字幕条镜像一并退场')
        arm = _js_fn(self.js, 'armChipButton')
        self.assertIsNotNone(arm)
        self.assertLess(arm.index("el.setAttribute('role', 'button'"),
                        arm.index('if (el.__armed) return'),
                        'armChipButton 属性设置须在 __armed 守卫之前(disarm 后可复设)')
        manual = _js_fn(self.js, 'manualReconnect')
        self.assertIsNotNone(manual)
        self.assertIn("'连接中断 · 重连中'", manual, '手动重试后须即时切回重连中文案')


class WP6EndCardPins(unittest.TestCase):
    """WP6 结束卡三分态静态钉子(Task 7,spec WP6 全节)。

    服务端(Task 1 契约,不得改)stopped 事件:成功带 job_id 无 reason;
    空会话 reason='empty';转正失败 reason='finalize_failed' 另带 error 简要。
    结束卡按此三分支渲染:
      - 成功:标题「文稿已生成」;endLink 升主按钮(.ego 视觉),「开始新会话」降
        ghost;web 深链 window.open('/#id', '_blank')(不再本页导航,防双工作台),
        桌面 openMainJob 分支原样保留(含 closeLiveCaption);
      - finalize_failed:标题「文稿生成失败,录音已保留」+ error 摘要 + 寻回文案
        + 「立即重试转正」按钮(POST /api/live/recover_now,防连点,原位转成功态);
      - empty:「本场没有录到语音」(spec 初稿笔误「本場」已经控制者勘误为简体「本场」)。
    三分支均不得出现旧文案「正在生成完整文稿」;旧服务端(stopped 无 reason 无
    job_id)按失败态渲染——行为级见 WP6StoppedBranchesVector。"""

    @classmethod
    def setUpClass(cls):
        cls.html = LIVE_HTML.read_text(encoding='utf-8')
        cls.js = LIVE_JS.read_text(encoding='utf-8')

    def test_three_branch_dispatch_shape(self):
        """onStopped 分派形状:job_id 优先 → reason==='empty' → 其余(含旧服务端
        无 reason 无 job_id)一律落失败态——保守给寻回文案,绝不误报「没有录到语音」。"""
        body = _js_fn(self.js, 'onStopped')
        self.assertIsNotNone(body, 'onStopped 缺失')
        self.assertIn('if (ev.job_id)', body, '成功分支判定缺失')
        self.assertIn("ev.reason === 'empty'", body, '空会话分支判定缺失')
        self.assertIn('showEndSuccess(ev.job_id)', body, '成功分支未走共用成功态渲染')
        self.assertIn('showEndFailure(', body, '兜底分支未走失败态渲染')
        self.assertLess(body.index('if (ev.job_id)'),
                        body.index("ev.reason === 'empty'"),
                        '分派顺序:job_id 必须先于 reason 判定(成功事件无 reason)')

    def test_success_branch_copy_and_primary_link(self):
        """成功态:标题「文稿已生成」;endLink 升主按钮(classList 加 primary),
        「开始新会话」降 ghost。CSS 侧 .elink.primary 复用 .ego 主按钮视觉
        (浅底深字 650)——DOM 最小改动:endLink 仍是 <a>,不加第二套结构。"""
        self.assertIn('文稿已生成', self.js)
        success = _js_fn(self.js, 'showEndSuccess')
        self.assertIsNotNone(success, 'showEndSuccess 缺失')
        self.assertIn("link.classList.add('primary')", success, 'endLink 未升主按钮形态')
        self.assertIn("$('endNew').classList.add('ghost')", success, '「开始新会话」未降 ghost')
        self.assertRegex(self.html,
                         r'\.ecard \.elink\.primary\{[^}]*background:#e9ebef[^}]*font-weight:650',
                         '.elink.primary 未复用 .ego 主按钮视觉')

    def test_failure_branch_copy_and_recover_button(self):
        """失败态:标题「文稿生成失败,录音已保留」;sub 含 error 摘要 + 寻回文案
        (spec 逐字);「立即重试转正」按钮 POST /api/live/recover_now。"""
        self.assertIn('文稿生成失败,录音已保留', self.js)
        self.assertIn('录音仍在服务端,重启转写服务后会自动转为任务(历史记录带「实时」标记)。',
                      self.js, '寻回文案缺失或被改写(spec 逐字)')
        failure = _js_fn(self.js, 'showEndFailure')
        self.assertIsNotNone(failure, 'showEndFailure 缺失')
        self.assertIn('立即重试转正', failure)
        retry = _js_fn(self.js, 'retryFinalize')
        self.assertIsNotNone(retry, 'retryFinalize(寻回重试)缺失')
        self.assertIn("fetch('/api/live/recover_now'", retry, '寻回端点未接')
        self.assertIn("method: 'POST'", retry, 'recover_now 必须 POST')

    def test_recover_retry_button_states(self):
        """寻回按钮态机:防连点(recoverBusy + disabled 双保险);有新 job → 原位转
        成功态(showEndSuccess(recovered[0]));空数组/请求失败 → 就地提示
        (endRetryNote,不弹整卡错误)并保持失败态(按钮还原可再点)。"""
        retry = _js_fn(self.js, 'retryFinalize')
        self.assertIsNotNone(retry)
        self.assertIn('state.recoverBusy', retry, '防连点旗标缺失')
        self.assertLess(retry.index('recoverBusy'), retry.index('fetch('),
                        '防连点判断必须在发起请求之前')
        self.assertIn('btn.disabled = true', retry, '进行中态须禁用按钮')
        self.assertIn('正在寻回', retry, '进行中态文案缺失')
        self.assertIn('showEndSuccess(ids[0])', retry, '寻回成功未原位转成功态')
        self.assertIn('endRetryNote', retry, '就地提示槽缺失(不许整卡报错)')
        self.assertIn('btn.disabled = false', retry, '失败后按钮须还原可再点')
        html_btn = re.search(r'<button[^>]*id="endRetry"[^>]*>', self.html)
        self.assertTrue(html_btn, 'live.html 缺 #endRetry 按钮')
        self.assertIn('hidden', html_btn.group(0), '#endRetry 默认须隐藏')
        self.assertIn('id="endRetryNote"', self.html, '就地提示元素缺失')

    def test_empty_branch_copy(self):
        """空会话态(spec 勘误后逐字):标题「本场没有录到语音」,
        sub「会话已结束,未生成文稿。」;endLink 不显示(无文稿可开)。"""
        self.assertIn('本场没有录到语音', self.js)
        self.assertIn('会话已结束,未生成文稿', self.js)

    def test_old_generating_copy_banned(self):
        """三分支均不得出现旧文案「正在生成完整文稿」(失败/空会话报喜、成功态失实)。"""
        self.assertNotIn('正在生成完整文稿', self.js)
        self.assertNotIn('正在生成完整文稿', self.html)

    def test_web_blank_deep_link_desk_branch_kept(self):
        """成功态深链:web window.open('/#' + jobId, '_blank') 开新标签(本页停在
        结束卡,不再 /live 内导航出双工作台);桌面 openMainJob 分支原样保留,
        且跳转后仍调 closeLiveCaption 隐藏悬浮窗。"""
        success = _js_fn(self.js, 'showEndSuccess')
        self.assertIsNotNone(success)
        self.assertIn("window.open('/#' + jobId, '_blank')", success,
                      'web 深链未开新标签(本页导航会造成双工作台)')
        self.assertIn('openMainJob', success, '桌面分支缺失')
        i_desk = success.index('openMainJob')
        self.assertLess(i_desk, success.index('window.open('),
                        '桌面分支应在 web 分支之前(优先级不变)')
        self.assertIn('closeLiveCaption', success, '跳转后须隐藏悬浮窗')
        self.assertIn('preventDefault', success)


# langFromStorage/migrateLegacyLang 行为向量:从 live.js 源码抠真实函数(花括号配平,
# splitSentences/applyRoute 先例),桩掉 localStorage 后跑迁移语义断言。
# 曾抓到的回归:langFromStorage 沿用 migrateLegacyLang 返回的原值 → 键清了、表单仍
# 回落 zh>en,迁移形同虚设——静态钉看不出来,必须行为级。
WP4_LANG_VECTORS_MJS = r'''
import { readFileSync } from 'node:fs';
const src = readFileSync(process.argv[2], 'utf8');
function fn(name){
  const m = src.match(new RegExp('function ' + name + '\\s*\\('));
  if (!m) throw new Error(name + ' missing');
  const start = src.indexOf('{', m.index);
  let depth = 0;
  for (let i = start; i < src.length; i++){
    if (src[i] === '{') depth++;
    else if (src[i] === '}'){ depth--; if (!depth) return src.slice(m.index, i + 1); }
  }
}
const store = {};
const LS = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const head = src.slice(src.indexOf('var LANG_OK'), src.indexOf('function langFromStorage'));
const bundle = ["var LEGACY_DEFAULT_LANG = 'zh>en';", head,
  fn('langFromStorage'), fn('migrateLegacyLang'), fn('langPairIsNoTranslate'),
  'return {langFromStorage, migrateLegacyLang, langPairIsNoTranslate};'].join('\n');
const { langFromStorage, migrateLegacyLang, langPairIsNoTranslate } =
  new Function('localStorage', '$', bundle)(LS, () => '');
let fail = 0;
const eq = (a, b, what) => {
  if (JSON.stringify(a) !== JSON.stringify(b)){
    console.error('FAIL ' + what + '  got=' + JSON.stringify(a));
    fail++;
  }
};
store['live.lang'] = 'zh>en';
eq(langFromStorage(), {src:'zh', tgt:''}, '旧默认 zh>en → 新默认不翻译');
if ('live.lang' in store) { console.error('FAIL 旧默认键未清除'); fail++; }
store['live.lang'] = 'zh>ja';
eq(langFromStorage(), {src:'zh', tgt:'ja'}, '合法值 zh>ja 不动');
if (store['live.lang'] !== 'zh>ja') { console.error('FAIL zh>ja 键被误清'); fail++; }
delete store['live.lang'];
eq(langFromStorage(), {src:'zh', tgt:''}, '无值 → 新默认不翻译');
store['live.lang'] = 'xx>yy';
eq(langFromStorage(), {src:'zh', tgt:''}, '非法值 → 新默认不翻译(不猜外发)');
eq(langPairIsNoTranslate('zh>zh'), true, 'zh>zh 判不翻译');
eq(langPairIsNoTranslate('zh>en'), false, 'zh>en 判翻译');
eq(langPairIsNoTranslate(''), false, '空串不误判');
store['live.lang'] = 'zh>en';
eq(migrateLegacyLang(), 'zh>en', 'resume 路径:迁移返回原值(本次会话展示不被改写)');
if ('live.lang' in store) { console.error('FAIL resume 路径键未清除'); fail++; }
console.log(fail === 0 ? 'WP4_LANG_VECTORS_OK' : fail + ' VECTORS FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP4LangMigrationVectors(unittest.TestCase):
    """旧默认迁移行为向量(表单路径弃用旧值走新默认、resume 路径保原值仅清键、
    合法值不动、非法值不猜外发)。静态钉只看得见「调用了」,语义对不对必须真跑。"""

    def test_vectors_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP4_LANG_VECTORS_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(LIVE_JS)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, 'WP4 迁移行为向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP4_LANG_VECTORS_OK', proc.stdout.decode('utf-8', 'replace'))
        finally:
            os.unlink(vec)


WP3_TAKEN_OVER_VECTOR_MJS = r'''
import { readFileSync } from 'node:fs';
const src = readFileSync(process.argv[2], 'utf8');
function fn(name){
  const m = src.match(new RegExp('function ' + name + '\\s*\\('));
  if (!m) throw new Error(name + ' missing');
  const start = src.indexOf('{', m.index);
  let depth = 0;
  for (let i = start; i < src.length; i++){
    if (src[i] === '{') depth++;
    else if (src[i] === '}'){ depth--; if (!depth) return src.slice(m.index, i + 1); }
  }
  throw new Error(name + ' braces');
}
// 桩面:onTakenOver 依赖链 setPhase/stopCapture/stopTimer/clearSessionStorage/
// releaseLiveLock/disarmReconnectChip 全部抠入;$ 桩按 id 缓存;state 初值模拟
// 「stopping 相位被接管」最恶劣场景(userStop=true、lastSeq=42、终态旗标亮、锁在手)。
const store = { 'live.sessionId': 'sess_old', 'live.startedAt': '123' };
const LS = { getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
const els = {};
const $ = id => (els[id] || (els[id] = { hidden: false, textContent: '', disabled: false,
  classList: { remove(){} }, removeAttribute(){}, remove(){} }));
const bundle = ['var state = {phase:"live", sessionId:"sess_old", userStop:true, lastSeq:42,',
  '  reconnectTimer:123, reconnectFinal:true, timerIv:7, capture:null, ws:{close(){}},',
  '  lock:{released:false, release(){this.released=true;}}};',
  fn('setPhase'), fn('stopCapture'), fn('stopTimer'), fn('clearSessionStorage'),
  fn('releaseLiveLock'), fn('barChip'), fn('disarmReconnectChip'), fn('onTakenOver'),
  'onTakenOver();', 'return state;'].join('\n');
const endState = new Function('localStorage', '$', bundle)(LS, $);
let fail = 0;
const ok = (cond, what) => { if (!cond){ console.error('FAIL ' + what); fail++; } };
ok(endState.sessionId === null, 'sessionId 未复位:重开会以 resume=!!sessionId 判真对死会话发 resume(落错误卡)');
ok(endState.userStop === false, 'userStop 未复位:stopping 相位被接管后重开会误发 resume+stop');
ok(endState.lastSeq === 0, 'lastSeq 未清零:新会话 seq 从小值起会被去重全吞、字幕静默');
ok(!('live.sessionId' in store) && !('live.startedAt' in store), 'session 存储未清');
ok(endState.lock === null || endState.lock.released === true, '锁未释放');
ok(endState.reconnectFinal === false, '重连终态旗标未解除');
ok(endState.reconnectTries === 0, '重连计数未清零');
ok($('setup').hidden === false && $('goBtn').disabled === false, '未回可用的表单态');
ok($('stage').hidden === true && $('endwrap').hidden === true, '舞台/结束卡未收起');
ok($('resumeNote').textContent.indexOf('会话已在其他窗口打开') >= 0, '接管提示缺失');
console.log(fail === 0 ? 'WP3_TAKEN_OVER_VECTOR_OK' : fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP3TakenOverReopenVector(unittest.TestCase):
    """评审 Important 行为向量:taken_over 终态后页内可干净重开。onTakenOver 必须
    复位 sessionId/userStop/lastSeq 并清共享存储——静态钉只看得见「赋值存在」,
    复位齐不齐(对照 autoResume 死会话分支的处理面)必须真跑。"""

    def test_vector_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP3_TAKEN_OVER_VECTOR_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(LIVE_JS)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, 'taken_over 重开向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP3_TAKEN_OVER_VECTOR_OK', proc.stdout.decode('utf-8', 'replace'))
        finally:
            os.unlink(vec)


WP6_STOPPED_VECTOR_MJS = r'''
import { readFileSync } from 'node:fs';
const src = readFileSync(process.argv[2], 'utf8');
function fn(name){
  const m = src.match(new RegExp('function ' + name + '\\s*\\('));
  if (!m) throw new Error(name + ' missing');
  const start = src.indexOf('{', m.index);
  let depth = 0;
  for (let i = start; i < src.length; i++){
    if (src[i] === '{') depth++;
    else if (src[i] === '}'){ depth--; if (!depth) return src.slice(m.index, i + 1); }
  }
  throw new Error(name + ' braces');
}
// 桩面:与 WP3TakenOverReopenVector 同款——onStopped 依赖链(setPhase/stopCapture/
// stopTimer/clearSessionStorage/releaseLiveLock/barChip/disarmReconnectChip)全数抠入,
// $ 按 id 缓存,classList 真实现(primary/ghost 断言要靠它)。
const els = {};
const makeEl = id => ({ id, hidden: false, textContent: '', href: '', disabled: false,
  onclick: null, appendChild(){}, remove(){}, removeAttribute(){}, setAttribute(){},
  classList: { _s: new Set(), add(c){ this._s.add(c); }, remove(c){ this._s.delete(c); },
    contains(c){ return this._s.has(c); } } });
const $ = id => (els[id] || (els[id] = makeEl(id)));
const resetEls = () => { for (const k in els) delete els[k]; };
const store = {};
const LS = { getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
const bundle = ['var state = {phase:"stopping", sessionId:"sess_x", userStop:true, lastSeq:9,',
  '  reconnectTimer:5, reconnectFinal:false, timerIv:3, capture:null, recoverBusy:false,',
  '  ws:{close(){}}, lock:{released:false, release(){this.released=true;}}};',
  fn('setPhase'), fn('stopCapture'), fn('stopTimer'), fn('clearSessionStorage'),
  fn('releaseLiveLock'), fn('barChip'), fn('disarmReconnectChip'),
  fn('showEndSuccess'), fn('showEndFailure'), fn('onStopped'), fn('retryFinalize'),
  'const snap = () => JSON.stringify({title:$("endTitle").textContent, sub:$("endSub").textContent,',
  '  linkHidden:$("endLink").hidden, linkPrimary:$("endLink").classList.contains("primary"),',
  '  href:$("endLink").href, newGhost:$("endNew").classList.contains("ghost"),',
  '  retryHidden:$("endRetry").hidden, retryDisabled:$("endRetry").disabled,',
  '  retryText:$("endRetry").textContent, noteHidden:$("endRetryNote").hidden,',
  '  noteText:$("endRetryNote").textContent, wrapHidden:$("endwrap").hidden,',
  '  phase:state.phase, recoverBusy:state.recoverBusy});',
  'const out = {};',
  'onStopped({job_id:"job_ok"}); out.success = snap();',
  'resetEls();',
  'onStopped({reason:"empty"}); out.empty = snap();',
  'resetEls();',
  'onStopped({reason:"finalize_failed", error:"磁盘满"}); out.failed = snap();',
  'resetEls();',
  'onStopped({}); out.legacy = snap();',            // 旧服务端:无 reason 无 job_id
  'fetch = async () => ({ok:true, json: async () => ({recovered:["job_new"]})});',
  'retryFinalize(); out.duringRetry = snap();',
  'await new Promise(r => setTimeout(r, 10));',
  'out.afterRetry = snap();',
  'fetch = async () => ({ok:true, json: async () => ({recovered:[]})});',
  'resetEls(); showEndFailure(); state.recoverBusy = false;',
  'retryFinalize();',
  'await new Promise(r => setTimeout(r, 10));',
  'out.emptyRecover = snap();',
  'fetch = async () => ({ok:false, status:500, json: async () => ({})});',
  'resetEls(); showEndFailure("磁盘满"); state.recoverBusy = false;',
  'retryFinalize();',
  'await new Promise(r => setTimeout(r, 10));',
  'out.httpFail = snap();',
  'return JSON.stringify(out);'].join('\n');
// await 不能出现在普通 Function 体:用 AsyncFunction 构造器(其余协议与 WP3 向量同款)
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const out = JSON.parse(
  await new AsyncFunction('localStorage', '$', 'fetch', 'resetEls', bundle)(LS, $, null, resetEls));
let fail = 0;
const j = (s) => JSON.parse(s);
const ok = (cond, what) => { if (!cond){ console.error('FAIL ' + what); fail++; } };
const S = j(out.success);
ok(S.title === '文稿已生成', '成功态标题:got ' + S.title);
ok(S.linkHidden === false && S.linkPrimary === true, '成功态 endLink 未升主按钮');
ok(S.newGhost === true, '成功态「开始新会话」未降 ghost');
ok(S.href === '/#job_ok', '成功态深链 href:got ' + S.href);
ok(S.retryHidden === true && S.wrapHidden === false, '成功态寻回按钮须隐藏、结束卡须亮');
ok(S.phase === 'ended', '成功态相位置 ended');
const E = j(out.empty);
ok(E.title === '本场没有录到语音', '空会话标题:got ' + E.title);
ok(E.sub.indexOf('会话已结束,未生成文稿') >= 0, '空会话 sub 缺「会话已结束,未生成文稿」');
ok(E.linkHidden === true, '空会话不得显示打开文稿');
ok(E.title.indexOf('没有录到语音') >= 0 && E.title !== '文稿生成失败,录音已保留', '空会话误落失败态文案');
const F = j(out.failed);
ok(F.title === '文稿生成失败,录音已保留', '失败态标题:got ' + F.title);
ok(F.sub.indexOf('磁盘满') >= 0, '失败态 sub 缺 error 摘要');
ok(F.sub.indexOf('录音仍在服务端,重启转写服务后会自动转为任务') >= 0, '失败态 sub 缺寻回文案');
ok(F.retryHidden === false && F.linkHidden === true, '失败态按钮显隐错');
const L = j(out.legacy);
ok(L.title === '文稿生成失败,录音已保留', '旧服务端兼容:无 reason 无 job_id 应按失败态,got ' + L.title);
ok(L.title.indexOf('没有录到语音') < 0, '旧服务端兼容:绝不误报「没有录到语音」');
ok(L.sub.indexOf('录音仍在服务端') >= 0, '旧服务端兼容:缺寻回文案');
const D = j(out.duringRetry);
ok(D.retryDisabled === true && D.retryText === '正在寻回…', '寻回进行中态缺失(防连点)');
ok(D.recoverBusy === true, '寻回进行中 recoverBusy 未置位');
const A = j(out.afterRetry);
ok(A.title === '文稿已生成' && A.href === '/#job_new', '寻回成功未原位转成功态');
ok(A.linkPrimary === true && A.retryHidden === true, '寻回成功后主按钮形态/寻回按钮显隐错');
ok(A.noteHidden === true, '寻回成功后就地提示未收起');
const ER = j(out.emptyRecover);
ok(ER.title === '文稿生成失败,录音已保留', '空寻回结果不得离开失败态:got ' + ER.title);
ok(ER.noteHidden === false && ER.noteText.indexOf('暂未寻回') >= 0, '空寻回结果缺就地提示');
ok(ER.retryDisabled === false, '空寻回结果后按钮未还原可再点');
const H = j(out.httpFail);
ok(H.title === '文稿生成失败,录音已保留', '请求失败不得离开失败态');
ok(H.noteHidden === false && H.noteText.indexOf('重试失败') >= 0, '请求失败缺就地提示');
ok(H.retryDisabled === false, '请求失败后按钮未还原可再点');
console.log(fail === 0 ? 'WP6_STOPPED_VECTOR_OK' : fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP6StoppedBranchesVector(unittest.TestCase):
    """WP6 行为向量:stopped 三分支 + 旧服务端兼容 + 寻回按钮态机,全部真跑。
    静态钉只看得见「字面在」,分派归属、原位转成功态、防连点与就地提示语义
    必须执行才知道(与 WP3TakenOverReopenVector 同款协议)。"""

    def test_vector_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP6_STOPPED_VECTOR_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(LIVE_JS)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, 'WP6 三分支向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP6_STOPPED_VECTOR_OK', proc.stdout.decode('utf-8', 'replace'))
        finally:
            os.unlink(vec)


class WP5IdentityPins(unittest.TestCase):
    """WP5 色板静态钉子(Task 8,spec §WP5):live 六色与主台 --sp*-ink
    同色相(亮化变体 --lsp0..5)、% 6 循环、灰不再参与真人分配。"""

    SP_INK = ['#1d5290', '#1f6b45', '#8f520c', '#65449f', '#14615f', '#96365a']

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX.read_text(encoding='utf-8')
        cls.lhtml = LIVE_HTML.read_text(encoding='utf-8')
        cls.ljs = LIVE_JS.read_text(encoding='utf-8')

    def test_lsp_six_vars_with_mapping_source_comments(self):
        """live.html :root 须有 --lsp0..5 六条,且每条注释写明映射源 hex
        (「同一个人跨端同色相」靠注释可追溯,改色时能找到主台对应项)。"""
        for i, ink in enumerate(self.SP_INK):
            m = re.search(r'--lsp%d:\s*#[0-9a-fA-F]{6}\s*;[^\n]*' % i, self.lhtml)
            self.assertTrue(m, 'live.html 缺 --lsp%d' % i)
            self.assertIn(ink, m.group(0),
                          '--lsp%d 注释须写映射源 %s' % (i, ink))

    def test_speaker_colors_equals_lsp_values(self):
        """live.js SPEAKER_COLORS 六值与 live.html --lsp0..5 逐一同值(同一套字面),
        且行注释带「与 index.html --sp*-ink 同源 … 两处同步」;旧三色绝迹。"""
        lsp = []
        for i in range(6):
            m = re.search(r'--lsp%d:\s*(#[0-9a-fA-F]{6})' % i, self.lhtml)
            self.assertTrue(m, '--lsp%d 缺失' % i)
            lsp.append(m.group(1).lower())
        m = re.search(r'var SPEAKER_COLORS = (\[[^\]]*\])[^\n]*', self.ljs)
        self.assertTrue(m, 'live.js 缺 SPEAKER_COLORS')
        vals = [v.lower() for v in re.findall(r'#[0-9a-fA-F]{6}', m.group(1))]
        self.assertEqual(vals, lsp, 'SPEAKER_COLORS 须与 --lsp0..5 同一套字面')
        self.assertEqual(len(vals), 6)
        line = m.group(0)
        self.assertIn('--sp*-ink 同源', line, 'SPEAKER_COLORS 行注释缺同源声明')
        self.assertIn('两处同步', line, 'SPEAKER_COLORS 行注释缺同步提醒')
        self.assertNotIn('#4C9AFF', self.ljs)
        self.assertNotIn('#98A2B3', self.ljs)

    def test_speaker_color_mod6_loop(self):
        """colorFor(speakerColor)改 % 6 循环(与主台 .c0-.c5/.s0-.s5 对齐);
        旧「末位灰回落」(length - 1)绝迹,灰不再参与真人分配。"""
        fn = _js_fn(self.ljs, 'speakerColor')
        self.assertIsNotNone(fn)
        self.assertIn('% 6', fn)
        self.assertNotIn('length - 1', fn)


WP5_COLOR_VECTOR_MJS = r'''
import { readFileSync } from 'node:fs';
const src = readFileSync(process.argv[2], 'utf8');
const vm = src.match(/var SPEAKER_COLORS = (\[[^\]]*\])/);
if (!vm) throw new Error('SPEAKER_COLORS missing');
function fn(name){
  const m = src.match(new RegExp('function ' + name + '\\s*\\('));
  if (!m) throw new Error(name + ' missing');
  const start = src.indexOf('{', m.index);
  let depth = 0;
  for (let i = start; i < src.length; i++){
    if (src[i] === '{') depth++;
    else if (src[i] === '}'){ depth--; if (!depth) return src.slice(m.index, i + 1); }
  }
  throw new Error(name + ' braces');
}
const bundle = ['var SPEAKER_COLORS = ' + vm[1] + ';', fn('speakerColor'),
  'return {speakerColor, colors: SPEAKER_COLORS};'].join('\n');
const { speakerColor, colors } = new Function(bundle)();
let fail = 0;
const ok = (cond, what) => { if (!cond){ console.error('FAIL ' + what); fail++; } };
ok(colors.length === 6, '色板须六色');
ok(new Set(colors).size === 6, '六色须互异(≥3 人不再共享一色)');
for (let i = 0; i <= 8; i++){
  ok(speakerColor(i) === colors[i % 6], 'speaker ' + i + ' 应循环取色 ' + (i % 6));
}
ok(speakerColor(null) === '', '未知说话人(null)回空串落 CSS 默认灰,灰只属于「未知」');
ok(speakerColor(undefined) === '', '未知说话人(undefined)同 null');
console.log(fail === 0 ? 'WP5_COLOR_VECTOR_OK' : fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP5ColorCycleVector(unittest.TestCase):
    """WP5 行为向量:colorFor % 6 循环真跑 0..8 断言色序(静态钉只看得见「% 6」
    字面,循环归属与未知说话人回落必须执行才知道;与 WP3/WP6 向量同款协议)。"""

    def test_vector_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP5_COLOR_VECTOR_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(LIVE_JS)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, 'WP5 色序向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP5_COLOR_VECTOR_OK', proc.stdout.decode('utf-8', 'replace'))
        finally:
            os.unlink(vec)


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class ApplyRouteVectors(unittest.TestCase):
    """applyRoute 纯函数断言向量(实际向量与提取逻辑在 test_ui_route_vectors.mjs,
    与 splitSentences 先例同款:花括号配平抠函数、ELECTRON_RUN_AS_NODE 执行)。"""

    def test_vectors_green(self):
        env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')   # 沿用先例:继承完整环境
        proc = subprocess.run([str(ELECTRON), str(VECTORS)], env=env,
                              capture_output=True, timeout=60, cwd=str(BASE))
        self.assertEqual(proc.returncode, 0, 'applyRoute 断言向量未全绿:%s%s'
                         % (proc.stdout.decode('utf-8', 'replace'),
                            proc.stderr.decode('utf-8', 'replace')[:600]))
        self.assertIn('ROUTE_VECTORS_OK', proc.stdout.decode('utf-8', 'replace'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
