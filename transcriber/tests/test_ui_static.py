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


WP3_LATE_EVENT_GUARD_VECTOR_MJS = r'''
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
// 桩面:服务端 take_over 先发 session_taken_over 再 gone.set(),两句之间旧 pump 可再投
// 一条带 seq 的事件——前端收到 [session_taken_over, event N]。onTakenOver 已把 lastSeq
// 归零并置相位于 ended,handleEvent 必须在 ended 相位丢弃带 seq 事件,否则上一场的
// 大 seq 写回 lastSeq,页内重开后新会话的小 seq 事件全被去重吞掉(字幕静默)。
// 这里只抠 handleEvent,renderActive/badge/setPhase 用桩替换。
const els = {};
const $ = id => (els[id] || (els[id] = { hidden: false, textContent: '' }));
const bundle = ['var state = {phase:"ended", lastSeq:0, partial:""};',
  'var badgeCalls = 0, renderCalls = 0, phaseWrites = [];',
  'function badge(){ badgeCalls++; }',
  'function renderActive(){ renderCalls++; }',
  'function setPhase(p){ phaseWrites.push(p); state.phase = p; }',
  fn('handleEvent'),
  // 场景 1:ended 相位(已被接管)收到迟到 partial(上一场大 seq=57)
  'handleEvent({type:"partial", seq:57, text:"迟到的旧场字幕"});',
  'const afterLate = {lastSeq: state.lastSeq, partial: state.partial,',
  '  badgeCalls, renderCalls};',
  // 场景 2(对照):live 相位同型事件照常入账——守卫不得误伤正常路径
  'state.phase = "live";',
  'handleEvent({type:"partial", seq:2, text:"新场字幕"});',
  'const afterLive = {lastSeq: state.lastSeq, partial: state.partial};',
  'return {afterLate, afterLive};'].join('\n');
const out = new Function('$', bundle)($);
let fail = 0;
const ok = (cond, what) => { if (!cond){ console.error('FAIL ' + what); fail++; } };
ok(out.afterLate.lastSeq === 0,
  'ended 相位迟到事件把上一场大 seq 写回 lastSeq(重开后新会话小 seq 全被去重吞掉)');
ok(out.afterLate.partial === '', 'ended 相位迟到文本不应再进字幕面板');
ok(out.afterLate.badgeCalls === 0 && out.afterLate.renderCalls === 0,
  'ended 相位迟到事件不应触发任何渲染');
ok(out.afterLive.lastSeq === 2 && out.afterLive.partial === '新场字幕',
  'live 相位同型事件被误伤:守卫只许在 ended 相位拦截');
console.log(fail === 0 ? 'WP3_LATE_EVENT_GUARD_VECTOR_OK' : fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP3LateEventGuardVector(unittest.TestCase):
    """评审 Important 行为向量:接管后迟到事件不得重污染 lastSeq。handleEvent 在
    ended 相位(如被接管)必须先丢带 seq 事件再去重/分派——静态钉只看得见「return
    存在」,真跑才能证明 lastSeq 不被旧场大 seq 写回、live 相位又不被误伤。"""

    def test_vector_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP3_LATE_EVENT_GUARD_VECTOR_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(LIVE_JS)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, '迟到事件守卫向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP3_LATE_EVENT_GUARD_VECTOR_OK', proc.stdout.decode('utf-8', 'replace'))
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
    """WP5 色板与说话人身份一致静态钉子(Task 8,spec §WP5):
    live 六色与主台 --sp*-ink 同色相(亮化变体 --lsp0..5)、% 6 循环、
    正文标签统一 displayName、chip 改名双路径确认条(不显示会议数)。"""

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

    def test_draw_body_uses_display_name(self):
        """正文说话人名与搜索过滤 nm 同走 displayName(本地覆盖 > 全局库):
        库里改名后历史任务正文标签同步,不再只认本地 speaker_names。"""
        fn = _js_fn(self.src, 'draw')
        self.assertIsNotNone(fn)
        self.assertIn('displayName(s.speaker)', fn)
        self.assertNotIn('names[String(s.speaker)]', fn)
        self.assertIn("'说话人' + (s.speaker + 1)", fn, '无名回落默认名须保留')

    def test_rename_dual_path_endpoints(self):
        """chip 改名双路径(替换旧静默 PATCH):全局走 PATCH /api/speakers/<gid>
        + toast「已全局改名,相关会议文稿已更新」+ ensureSpeakers(true);
        本任务走 PATCH /api/jobs/<id>(speaker_names)+ names[sp]=v 不动全局;
        两条任务级路径(仅此会议/非 gid)失败都须 err toast 并止写 names(评审 Minor)。"""
        fn = _js_fn(self.src, 'showRenameConfirm')
        self.assertIsNotNone(fn, '缺 showRenameConfirm(双路径确认条)')
        self.assertIn("'/api/speakers/' + encodeURIComponent(gid)", fn)
        self.assertIn('已全局改名,相关会议文稿已更新', fn)
        self.assertIn('ensureSpeakers(true)', fn)
        self.assertIn("'/api/jobs/' + j.id", fn)
        self.assertIn('speaker_names', fn)
        self.assertIn('names[sp] = v', fn)
        self.assertEqual(self.src.count("toast('改名失败:'"), 2,
                         '「仅此会议」与非 gid 路径的 PATCH 失败都须 err toast')

    def test_rename_confirm_bar_copy_no_numbers(self):
        """确认条文案「「{名}」应用到哪些会议?」+ [应用到所有会议][仅此会议] 双按钮;
        复用库页 .suggest 样式族不新建组件;不得渲染会议数数字——弱证据门下
        锚点数 ≠ 会议数,写数字必错(eng-review 勘误)。"""
        fn = _js_fn(self.src, 'showRenameConfirm')
        self.assertIn('应用到哪些会议', fn)
        self.assertIn('应用到所有会议', fn)
        self.assertIn('仅此会议', fn)
        self.assertIn(".suggest", fn)
        self.assertNotIn('场会议', fn, '确认条内不得出现会议数')

    def test_rename_confirm_dedup_and_dismiss(self):
        """同 chip 连续 onchange 只挂一条:showRenameConfirm 入口先拆旧条;
        拆条时机用 pointerdown(触发 change 的那次按压先于监听注册,同一
        interaction 尾随 click 冒泡绝不误拆——评审 Important);空白/改回原值时
        撤回在途确认条(条上提议须与输入框现值一致)。"""
        fn = _js_fn(self.src, 'showRenameConfirm')
        self.assertIn('dismissRenameBar()', fn)
        self.assertIn("addEventListener('pointerdown'", fn,
                      '拆条监听须挂 pointerdown(click 会被触发 change 的同一次点击秒拆)')
        self.assertNotIn("addEventListener('click'", fn)
        self.assertIsNotNone(re.search(r'function dismissRenameBar\s*\(', self.src))
        self.assertIn('inp.value = cur; dismissRenameBar();', self.src)
        fn_d = _js_fn(self.src, 'dismissRenameBar')
        self.assertIn('t(false)', fn_d,
                      '常规拆条不得还原输入框(再改名字换条时输入框持新 raw,'
                      '还原会吃掉用户输入);还原仅由条外 pointerdown 的 tear(true) 负责')


WP5_RENAME_VECTOR_MJS = r'''
import { readFileSync } from 'node:fs';
const html = readFileSync(process.argv[2], 'utf8');
const sm = html.match(/<script>([\s\S]*?)<\/script>/);
if (!sm) throw new Error('inline script missing');
const src = sm[1];
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
// 桩面:document 只实现 createElement/addEventListener/removeEventListener/dispatch;
// inp 档 .spkrow;displayName 恒回「老王」(当前显示名);fetch/toast/draw 全记录。
const created = [];
const makeEl = tag => ({ tag, className: '', innerHTML: '', removed: false, qs: {},
  remove(){ this.removed = true; },
  after(){}, contains(){ return false; },
  querySelector(sel){ return this.qs[sel] || (this.qs[sel] = makeEl('button')); } });
const DOC = {
  listeners: {},
  createElement(tag){ const el = makeEl(tag); created.push(el); return el; },
  addEventListener(type, fn){ (this.listeners[type] || (this.listeners[type] = [])).push(fn); },
  removeEventListener(type, fn){ const l = this.listeners[type] || [];
    const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); },
  dispatch(type, ev){ for (const fn of (this.listeners[type] || []).slice()) fn(ev); },
};
const INP = { value: '张三',
  closest(sel){ return sel === '.spkrow' ? { parentNode: {}, after(){} } : null; } };
const toasts = []; const fetches = []; const names = {};
const counters = { draws: 0 };
const fetchBox = { impl: async () => ({ ok: true, json: async () => ({}) }) };
const bundle = ['var renameBar = null, renameBarTear = null;',
  'var document = DOC;',
  'var esc = s => String(s == null ? "" : s);',
  'var displayName = () => "老王";',
  'var toast = (m, k) => toasts.push({ m, k });',
  'var ensureSpeakers = async () => ({});',
  'var draw = () => { counters.draws++; };',
  'var $ = () => ({ value: "" });',
  'var fetch = async (u, o) => { fetches.push({ u, o }); return await fetchBox.impl(u, o); };',
  fn('dismissRenameBar'), fn('showRenameConfirm'),
  'let fail = 0;',
  'const ok = (cond, what) => { if (!cond){ console.error("FAIL " + what); fail++; } };',
  'const tick = () => new Promise(r => setTimeout(r, 10));',
  // 事件序 1:输入框打字后点别处——按压(其 pointerdown 先于监听注册)→ blur→change
  // 挂条 → 同一 interaction 的尾随 click 冒泡到 document。评审 Important:旧码挂
  // click,条被这次点击秒拆,新名滞留输入框零落盘。
  'showRenameConfirm({ id: "job1" }, INP, "gid1", "0", "张三");',
  'const bar = created[created.length - 1];',
  'const types = Object.keys(DOC.listeners).filter(t => DOC.listeners[t].length);',
  'ok(types.length === 1 && types[0] === "pointerdown",',
  '  "拆条监听只许挂 pointerdown 一条,got [" + types.join(",") + "]");',
  'DOC.dispatch("click", { target: { id: "outside" } });',
  'ok(!bar.removed, "尾随 click 不得拆确认条(点别处提交改名被秒拆=静默丢失)");',
  'ok(INP.value === "张三", "尾随 click 不得改动输入框");',
  // 事件序 2:条外 pointerdown → 拆条 + 输入框还原当前显示名(不留未落盘假值)
  'DOC.dispatch("pointerdown", { target: { id: "outside" } });',
  'ok(bar.removed, "条外 pointerdown 拆条");',
  'ok(INP.value === "老王", "拆条还原输入框为当前显示名");',
  'ok(Object.keys(DOC.listeners).every(t => DOC.listeners[t].length === 0), "监听随拆条移除");',
  // [应用到所有会议]:拆条不还原输入框(持新名待落盘)+ 端点/toast/重绘
  'created.length = 0;',
  'showRenameConfirm({ id: "job1" }, INP, "gid1", "0", "张三");',
  'const barAll = created[created.length - 1];',
  'INP.value = "张三";',
  'barAll.qs["[data-ra=\\"all\\"]"].onclick();',
  'await tick();',
  'ok(barAll.removed, "全局应用拆条");',
  'ok(INP.value === "张三", "应用路径不还原输入框(持新名待落盘)");',
  'ok(fetches.some(f => f.u === "/api/speakers/gid1" && f.o.method === "PATCH"), "全局端点");',
  'ok(toasts.some(t => t.m === "已全局改名,相关会议文稿已更新"), "全局 toast 字面");',
  'ok(counters.draws >= 1, "全局应用后重绘");',
  // [仅此会议]:失败出 err toast + 还原 + 止写 names(评审 Minor);成功写 names + 重绘
  'created.length = 0; toasts.length = 0; fetches.length = 0; counters.draws = 0;',
  'showRenameConfirm({ id: "job1" }, INP, "gid1", "0", "张三");',
  'const barHere = created[created.length - 1];',
  'INP.value = "张三";',
  'fetchBox.impl = async () => ({ ok: false, status: 500, json: async () => ({}) });',
  'barHere.qs["[data-ra=\\"here\\"]"].onclick();',
  'await tick();',
  'ok(INP.value === "老王", "仅此会议失败:输入框还原当前显示名");',
  'ok(toasts.some(t => t.k === "err"), "仅此会议失败出 err toast");',
  'ok(!("0" in names), "仅此会议失败不得写 names");',
  'ok(counters.draws === 0, "仅此会议失败不重绘");',
  'fetchBox.impl = async () => ({ ok: true, json: async () => ({}) });',
  'barHere.qs["[data-ra=\\"here\\"]"].onclick();',
  'await tick();',
  'ok(fetches.some(f => f.u === "/api/jobs/job1" && f.o.method === "PATCH" &&',
  '  f.o.body.indexOf("speaker_names") >= 0), "本任务端点+speaker_names");',
  'ok(names["0"] === "张三", "成功写 names[sp]");',
  'ok(counters.draws >= 1, "本任务应用后重绘");',
  'return fail;'].join('\n');
// await 不能出现在普通 Function 体:用 AsyncFunction 构造器(WP6 向量同款协议)
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const fail = await new AsyncFunction(
  'DOC', 'INP', 'toasts', 'fetches', 'names', 'counters', 'fetchBox', 'created',
  bundle)(DOC, INP, toasts, fetches, names, counters, fetchBox, created);
console.log(fail === 0 ? 'WP5_RENAME_VECTOR_OK' : fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP5RenameConfirmVector(unittest.TestCase):
    """评审 Important 行为向量:确认条事件序真跑——「点别处提交改名」的
    mousedown→blur→change→click 序列不得秒拆刚挂的确认条(拆条监听必须
    pointerdown:触发 change 的按压先于注册),条外 pointerdown 拆条并还原
    输入框;双按钮端点/toast/失败止写照断。静态钉只看得见监听类型字面,
    事件序归属必须执行才知道(与 WP3/WP6 向量同款协议)。"""

    def test_vector_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP5_RENAME_VECTOR_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(INDEX)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, 'WP5 确认条事件序向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP5_RENAME_VECTOR_OK', proc.stdout.decode('utf-8', 'replace'))
        finally:
            os.unlink(vec)


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


class WP7IndexFastFixPins(unittest.TestCase):
    """WP7 index 侧快修批静态钉子(Task 9,spec 表 7.1/7.5/7.6/7.12/7.13/7.16/
    7.17/7.18)。精确值字面钉:确认文案、时长(ms)、动效 token、签名函数与
    「无变化 return」路径,全部按 brief 原值断言。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX.read_text(encoding='utf-8')

    # ---- 7.1 上传后清标题 ----
    def test_7_1_reset_drop_clears_title(self):
        """resetDrop 必须清空 #title:上传 A(填过标题)完成后再传 B,B 标题为空。"""
        body = _js_fn(self.src, 'resetDrop')
        self.assertIsNotNone(body, 'resetDrop 缺失')
        self.assertRegex(body, r"\$\('#title'\)\.value\s*=\s*''",
                         'resetDrop 须清空 #title')

    # ---- 7.5 删除确认文案言明不可逆 ----
    def test_7_5_delete_confirm_text_unified(self):
        """历史项 .jdel 与文稿页 #del 两处 confirmText 统一为同一句不可逆警示;
        旧文案「确认?」「再点一次确认删除」绝迹(丢弃/放弃/库删除不在此列)。"""
        self.assertGreaterEqual(self.src.count('删除后音频与文稿不可恢复,确认?'), 2,
                                '两处删除确认文案都须统一')
        self.assertNotIn("armConfirm(delBtn, '确认?'", self.src)
        self.assertNotIn("'再点一次确认删除'", self.src)

    # ---- 7.6 搜索大小写 ----
    def test_7_6_search_case_folding_both_sides(self):
        """过滤双侧 toLowerCase():英文文稿搜小写要命中大写词(正文与说话人名两侧)。"""
        draw = _js_fn(self.src, 'draw')
        self.assertIsNotNone(draw, 'draw 缺失')
        self.assertIn('s.text.toLowerCase()', draw, '正文侧须折叠大小写')
        self.assertIn('nm.toLowerCase()', draw, '说话人名侧须折叠大小写')

    def test_7_6_hl_highlights_same_folding(self):
        """hl() 高亮与过滤同一折叠:gi 大小写不敏感命中,捕获组保留原文大小写。"""
        hl = _js_fn(self.src, 'hl')
        self.assertIsNotNone(hl, 'hl 缺失')
        self.assertIn("'gi'", hl, '高亮正则须带 gi 标志')

    # ---- 7.12 toast 入场与时长 ----
    def test_7_12_toast_entrance_exit_ttl(self):
        """入场 fade+translateY(-6px→0) 200ms;退场 CSS 280ms 与 JS remove 300ms
        对齐;err 类 ttl 7s、普通 4s(错误消息可读完)。"""
        self.assertIn('@keyframes toast-in{from{opacity:0;transform:translateY(-6px)}}', self.src)
        self.assertIn('toast-in 200ms', self.src, '入场 200ms')
        self.assertIn('.toast.out{opacity:0;transform:translateY(-6px);transition:opacity 280ms,transform 280ms}',
                      self.src, '退场 CSS 280ms')
        toast = _js_fn(self.src, 'toast')
        self.assertIsNotNone(toast, 'toast 缺失')
        self.assertIn('setTimeout(() => t.remove(), 300)', toast,
                      'JS remove 300ms(退场动画 280ms 跑完再摘)')
        self.assertIn("kind === 'err' ? 7000 : 4000", toast, 'err 类 7s、普通 4s')

    # ---- 7.16 动效 token 与过渡 ----
    def test_7_16_motion_tokens_in_root(self):
        """:root 增三枚动效 token,精确值字面。"""
        self.assertIn('--t-fast:120ms', self.src)
        self.assertIn('--t-med:180ms', self.src)
        self.assertIn('--t-ease:cubic-bezier(.2,0,0,1)', self.src)

    def test_7_16_popover_entrance(self):
        """浮层入场 opacity+translateY(4px) 180ms(var(--t-med)/var(--t-ease)):
        #setpop 与导出 menu 两处;hidden/details 关闭态 display:none,重开即重放。"""
        self.assertIn('@keyframes pop-in{from{opacity:0;transform:translateY(4px)}}', self.src)
        m = re.search(r'\.setpop\{[^}]*\}', self.src)
        self.assertTrue(m, '.setpop 规则缺失')
        self.assertIn('animation:pop-in var(--t-med) var(--t-ease)', m.group(0),
                      '#setpop 入场 180ms')
        m = re.search(r'details\.expd \.menu\{[^}]*\}', self.src)
        self.assertTrue(m, '导出 menu 规则缺失')
        self.assertIn('animation:pop-in var(--t-med) var(--t-ease)', m.group(0),
                      '导出 menu 入场 180ms')

    def test_7_16_view_in_fade(self):
        """主区视图一次性淡入:类由 render() 仅视图切换时挂、animationend 摘除;
        旧纯 CSS「#main > :first-child」规则绝迹——运行页 2.5s 轮询 load(true)
        整块替换 innerHTML 不得重放淡入(评审 Important 修复)。"""
        self.assertIn('#main.view-in{animation:view-in var(--t-med) var(--t-ease)}', self.src)
        self.assertIn('@keyframes view-in{from{opacity:0}}', self.src)
        self.assertNotIn('#main > :first-child{animation', self.src,
                         '纯 CSS 首子元素规则让运行页每 2.5s 闪一次(评审 Important)')

    def test_7_16_view_in_only_from_render(self):
        """playViewIn 只能由 render 调用(route 对同视图幂等短路 ⇒ render 即真切换);
        load/renderEmpty/showLib 渲染器体内一律不得触发淡入。"""
        self.assertEqual(self.src.count('playViewIn()'), 2,
                         'playViewIn 只许出现定义与 render 调用两处')
        render = _js_fn(self.src, 'render')
        self.assertIsNotNone(render, 'render 缺失')
        self.assertIn('playViewIn()', render)
        for fn in ('load', 'renderEmpty', 'showLib'):
            body = _js_fn(self.src, fn)
            self.assertIsNotNone(body, '%s 缺失' % fn)
            self.assertNotIn('playViewIn()', body, '%s 不得触发淡入' % fn)

    def test_7_16_t_fast_token_not_dead(self):
        """--t-fast 必须有真实消费者:.live-row .dot 的 background 过渡绑
        var(--t-fast)(评审 Minor:token 不留死字面)。"""
        self.assertIn('--t-fast:120ms', self.src)
        m = re.search(r'\.live-row \.dot\{[^}]*\}', self.src)
        self.assertTrue(m, '.live-row .dot 规则缺失')
        self.assertIn('transition:background var(--t-fast)', m.group(0))

    def test_7_16_progress_transition_600ms_linear(self):
        """进度条 transition .8s → 600ms linear,.bar i 与 .runbar i 两处;
        旧迟滞字面精确绝迹(不做裸 .8s 断言,防 1.8s 之类误报——评审 Minor)。"""
        self.assertEqual(len(re.findall(r'transition:width 600ms linear', self.src)), 2,
                         '两处进度条都须 600ms linear')
        self.assertNotIn('transition:width .8s', self.src, '旧迟滞 transition 残留')

    # ---- 7.13 轮询不再全量重建 ----
    def test_7_13_signature_fn_and_fields(self):
        """签名函数在位,且字段覆盖行渲染的全部输入;title 必须在签名内——
        历史项改名属结构变化走全量重建,不得被就地更新路径吞掉。"""
        self.assertRegex(self.src, r'function jobsSignature\s*\(')
        sig = _js_fn(self.src, 'jobSig')
        self.assertIsNotNone(sig, 'jobSig 单行签名函数缺失')
        for f in ('j.id', 'j.status', 'j.stage', 'j.progress', 'j.queue_pos',
                  'j.running', 'j.title'):
            self.assertIn(f, sig, '签名缺字段 %s' % f)

    def test_7_13_refresh_no_change_early_return(self):
        """refresh 先算签名;无变化直接 return(零 DOM 写,armConfirm 武装态与
        滚动位置天然保住);仅 running 字段变化走 patchRunningRows,结构变化才 rebuildHist。"""
        body = _js_fn(self.src, 'refresh')
        self.assertIsNotNone(body, 'refresh 缺失')
        i_sig = body.find('jobsSignature()')
        i_ret = body.find('if(sig === lastJobsSig) return')
        self.assertGreaterEqual(i_sig, 0, 'refresh 须先算任务列表签名')
        self.assertGreaterEqual(i_ret, 0, '无变化须直接 return')
        self.assertLess(i_sig, i_ret, '签名计算须在早退之前')
        self.assertIn('patchRunningRows()', body, '仅 running 字段变化走就地更新')
        self.assertIn('rebuildHist()', body, '结构变化才全量重建')

    def test_7_13_data_id_anchor_and_in_place_patch(self):
        """job 行渲染带 data-id 锚点;patchRunningRows 按 data-id 就地更新该行
        .sub 文本与 .bar i 进度条宽,不碰其它行。"""
        self.assertIn('d.dataset.id = j.id', self.src, 'job 行渲染须带 data-id')
        self.assertIn(".querySelectorAll('.job[data-id]')", self.src)
        patch = _js_fn(self.src, 'patchRunningRows')
        self.assertIsNotNone(patch, 'patchRunningRows 缺失')
        self.assertIn("row.querySelector('.sub')", patch)
        self.assertIn("row.querySelector('.bar i')", patch)
        self.assertIn('textContent', patch, '.sub 用 textContent 就地改,不重排')

    def test_7_13_struct_sig_gate_keeps_rename_terminal(self):
        """结构门:jobStructSig 覆盖 id/running/title/created_at/duration_s/error/
        is_live,任一变化(含改名、终态到达)都拒绝就地更新改走重建。"""
        st = _js_fn(self.src, 'jobStructSig')
        self.assertIsNotNone(st, 'jobStructSig 结构签名缺失')
        for f in ('j.id', 'j.running', 'j.title', 'j.created_at', 'j.duration_s',
                  'j.error', 'j.is_live'):
            self.assertIn(f, st, '结构签名缺字段 %s' % f)
        self.assertIn('jobStructSig(a) !== jobStructSig(b)', self.src,
                      '就地更新前须逐行比较结构签名')

    def test_7_13_signature_includes_day_key(self):
        """列表签名含 todayKey():跨午夜「今天/更早」分组陈旧在下一个轮询即自愈
        (评审 Minor 闭合)。"""
        sig = _js_fn(self.src, 'jobsSignature')
        self.assertIsNotNone(sig, 'jobsSignature 缺失')
        self.assertIn('todayKey()', sig)

    def test_7_13_tick_live_path_untouched(self):
        """Task 4 的 tick→refreshLiveState 顺带刷新不得回退。"""
        tick = _js_fn(self.src, 'tick')
        self.assertIsNotNone(tick, 'tick 缺失')
        self.assertIn('refreshLiveState()', tick)

    # ---- 7.17 逗号行首残留 ----
    def test_7_17_no_comma_after_bold(self):
        """</b>,/</b>。 残留全清(逗号移进 <b> 内),窄栏侧栏不出现行首逗号;
        spec 要求两文件同查:index.html + live.html + live.js(评审 Minor 补 live 侧)。"""
        pattern = re.compile(r'</b>\s*[,，。]')
        self.assertIsNone(pattern.search(self.src), '</b> 后不得紧跟逗号/句号')
        self.assertIsNone(pattern.search(LIVE_HTML.read_text(encoding='utf-8')),
                          'live.html 同查须清')
        self.assertIsNone(pattern.search(LIVE_JS.read_text(encoding='utf-8')),
                          'live.js 同查须清')

    # ---- 7.18 audio 控件融入 ----
    def test_7_18_audio_accent_and_preload(self):
        """播放器进度色随主题(accent-color),preload 改 metadata(打开即显示真实时长)。"""
        self.assertRegex(self.src, r'audio\{[^}]*accent-color:var\(--primary\)')
        self.assertIn('preload="metadata"', self.src)
        self.assertNotIn('preload="none"', self.src)


class WP7LiveFastFixPins(unittest.TestCase):
    """WP7 live 侧快修批静态钉子(Task 10,spec 表 7.9/7.10/7.11/7.17):
    caret 条件闪烁(typing 三态)、两文件 reduced-motion 通用兜底、
    字幕条操作按钮常显。live.html/live.js 旧行号漂移,按内容字面钉。"""

    @classmethod
    def setUpClass(cls):
        cls.html = LIVE_HTML.read_text(encoding='utf-8')
        cls.js = LIVE_JS.read_text(encoding='utf-8')
        cls.idx = INDEX.read_text(encoding='utf-8')

    FALLBACK = (r'@media \(prefers-reduced-motion: reduce\)\s*\{\s*'
                r'\*,\*::before,\*::after\{\s*'
                r'animation-duration:\.01ms !important;\s*'
                r'animation-iteration-count:1 !important;\s*'
                r'transition-duration:\.01ms !important;\s*\}\s*\}')

    # ---- 7.9 caret 条件闪烁 ----
    def test_7_9_typing_class_toggled_at_single_update_point(self):
        """renderActive 是 partial 的单一更新点(partial 事件与 fullRender 恢复都走它),
        空/非空都须走到:两个 partial 行(#activeLine/#barPartialRow)按 has 同步
        toggle .typing——partial 非空 caret 呼吸,空则隐藏(静默期无闪烁)。"""
        body = _js_fn(self.js, 'renderActive')
        self.assertIsNotNone(body, 'renderActive 缺失')
        self.assertIn("$('activeLine').classList.toggle('typing', has)", body,
                      '面板 partial 行未按 has 同步 .typing')
        self.assertIn("$('barPartialRow').classList.toggle('typing', has)", body,
                      '账本 partial 行未按 has 同步 .typing')
        i_text = body.find("$('barPartial').textContent")
        i_toggle_bar = body.find("$('barPartialRow').classList.toggle('typing', has)")
        self.assertGreater(i_toggle_bar, i_text,
                           '.typing 应挂在账本行文本写入的同一更新序列里(不另开第二条路径)')

    def test_7_9_caret_default_hidden_breath_when_typing(self):
        """.caret 默认 opacity:0 且无动画(旧恒闪绝迹);.typing .caret 才亮出并
        平滑呼吸:keyframes 只写 50%(0%/100% 落元素指定值 opacity:1),ease 曲线,
        不用 steps 硬切。"""
        m = re.search(r'\.caret\{[^}]*\}', self.html)
        self.assertTrue(m, '.caret 规则缺失')
        self.assertIn('opacity:0', m.group(0), 'caret 默认须隐藏(静默期无闪烁)')
        self.assertNotIn('animation', m.group(0), 'caret 默认不得带动画(旧恒闪)')
        self.assertIn('.typing .caret{opacity:1;animation:caretBreath 1.1s ease infinite}',
                      self.html, '.typing .caret 平滑呼吸规则缺失')
        self.assertIn('@keyframes caretBreath{50%{opacity:.15}}', self.html,
                      '呼吸 keyframes 缺失或被改值')
        self.assertNotIn('steps(', self.html, 'caret 不得再用 steps 硬切')
        self.assertNotIn('animation:blink', self.html)
        self.assertNotIn('@keyframes blink', self.html, '旧恒闪 blink keyframes 须删')

    def test_7_9_reduce_typing_caret_static_visible(self):
        """reduced-motion 细粒度块与 7.9 语义整合:typing 时 caret 静态恒显
        (animation:none + opacity:1),非 typing 走默认 opacity:0 隐藏——
        旧「.caret{animation:none}」的恒显语义随 7.9 一并改写。"""
        marker = '@media (prefers-reduced-motion: reduce){'
        i_fine = self.html.find(marker)
        self.assertGreaterEqual(i_fine, 0, '细粒度 reduce 块缺失')
        # 只取首个(细粒度)块体:花括号配平,天然不含后面的通用兜底块
        start = self.html.index('{', i_fine + len(marker) - 1)
        depth = 0
        end = -1
        for i in range(start, len(self.html)):
            if self.html[i] == '{':
                depth += 1
            elif self.html[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        self.assertTrue(end > 0, '细粒度 reduce 块花括号未配平')
        body = self.html[start:end + 1]
        self.assertNotIn('*,*::before', body, '提取到的应是细粒度块而非兜底块')
        self.assertIn('.blog .e{animation:none}', body, '细粒度块:入场淡入停用须保留')
        self.assertIn('.typing .caret{animation:none;opacity:1}', body,
                      'reduce 下 typing 时 caret 须静态恒显')
        # 旧整类恒显规则(裸 .caret{animation:none})不得回潮——默认已 opacity:0 隐藏,
        # 整类恒显会让非 typing 的 caret 也常驻
        self.assertNotIn('.caret{animation:none}', body)

    # ---- 7.10 reduced-motion 通用兜底 ----
    def test_7_10_fallback_block_in_both_files(self):
        """两文件各加通用兜底:全部元素动画/过渡压到 .01ms 一次
        (index.html 新增;live.html 保留细粒度块)。"""
        self.assertRegex(self.html, self.FALLBACK, 'live.html 缺通用兜底块')
        self.assertRegex(self.idx, self.FALLBACK, 'index.html 缺通用兜底块(Task 9 未做,本任务补)')

    def test_7_10_fallback_after_fine_grained_block(self):
        """live.html 兜底须排在既有细粒度块之后(注释写明兜底+细粒度例外关系):
        兜底靠 !important 压时长,细粒度块管「哪些动画彻底关掉/静态恒显」的语义例外。"""
        m = re.search(self.FALLBACK, self.html)
        i_fb = m.start() if m else -1
        i_fine = self.html.find('.blog .e{animation:none}')
        self.assertGreaterEqual(i_fine, 0, '细粒度块缺失')
        self.assertGreater(i_fb, i_fine, '兜底块须在细粒度块之后')

    def test_7_10_index_rec_blink_present_for_fallback_to_tame(self):
        """index.html 兜底与 rec-blink 的语义兼容前提:rec-blink 无限呼吸动画仍在位
        (兜底把它变 1 次即静态——结束回落规则值 opacity:1,红点静态可见,正确);
        动画若已不存在此钉即失去对象,须随实现同步重审。"""
        self.assertEqual(self.idx.count('animation:rec-blink 1.6s ease-in-out infinite'), 2,
                         'rec-blink 两处消费点(录制红点/live-row 状态点)须原样保留')

    # ---- 7.11 字幕条操作按钮常显 ----
    def test_7_11_bar_actions_always_visible(self):
        """.bar .actions 删 hover 门控:块内不得再有 opacity/pointer-events 门控,
        .bar:hover .actions 规则整条删除;按钮样式族本身原样保留。"""
        m = re.search(r'\.bar \.actions\{[^}]*\}', self.html)
        self.assertTrue(m, '.bar .actions 规则缺失')
        block = m.group(0)
        self.assertNotIn('opacity', block, '操作按钮 hover 门控(opacity:0)须删,常显')
        self.assertNotIn('pointer-events', block, '操作按钮 pointer-events:none 须删')
        self.assertNotRegex(self.html, r'\.bar:hover \.actions', 'hover 恢复规则须整条删除')
        self.assertIn('.bar .actions button{', self.html, '按钮样式族须保留')

    # ---- 7.17 逗号行首(live 侧) ----
    def test_7_17_live_side_zero_hits(self):
        """WP7LiveFastFixPins 侧独立复钉(WP7IndexFastFixPins.test_7_17 已三文件同查,
        此处按任务书再验 live.html/live.js 零命中,防单钉失效无二道防线)。"""
        pattern = re.compile(r'</b>\s*[,，。]')
        self.assertIsNone(pattern.search(self.html), 'live.html </b> 后不得紧跟逗号/句号')
        self.assertIsNone(pattern.search(self.js), 'live.js </b> 后不得紧跟逗号/句号')

    # ---- 评审追加:页内重开的跨场账本残留 ----
    def test_begin_session_resets_ledger_before_lock(self):
        """beginSession 须在锁闸之前调 resetLedger()(与 reconnectTries/disarm 同族的
        页内重开残留复位):onStopped/onTakenOver 都不清 doc/partial,而 goBtn 不刷新
        直接重开是受支持路径——不清则上一场 partial 连呼吸 caret 与旧 doc 条目经
        beginSession→enterLiveUI→setMode→fullRender 带进新会话开场。"""
        body = _js_fn(self.js, 'beginSession')
        self.assertIsNotNone(body, 'beginSession 缺失')
        self.assertIn('resetLedger()', body, 'beginSession 缺账本复位')
        self.assertLess(body.index('resetLedger()'), body.index('acquireLiveLock()'),
                        '账本复位须在锁闸之前(占用路径同样不得携带旧账本)')
        fn = _js_fn(self.js, 'resetLedger')
        self.assertIsNotNone(fn, 'resetLedger 缺失')
        # 摘 DOM 在清数组之前(fullRender 只遍历 doc:清了数组不摘 el/bel,旧行照样滞留)
        self.assertLess(fn.index('e.el.remove()'), fn.index('state.doc = []'),
                        '须先逐条摘 el/bel 再清数组')
        self.assertIn('state.partial', fn, 'partial 须复位(旧文本连呼吸 caret 不得带进新场)')
        self.assertIn('state.entryId', fn, '账本计数器须归零(每场从 0 起)')
        self.assertIn('state.paused', fn, 'paused 须复位:它门控 setMode→fullRender 与 '
                      'renderActive,不复位则本次全部复位被跳过(暂停中被接管的路径)')


WP7_REOPEN_LEDGER_VECTOR_MJS = r'''
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
// 桩面(与 WP3TakenOverReopenVector 同族):真跑 beginSession→enterLiveUI→setMode→
// fullRender→renderActive 全链,锁/采集/连接/setPhase/startTimer/disarm 桩掉;
// $ 按 id 缓存,classList 带真 toggle(7.9 .typing 断言要靠它)。
// 预置「上一场最恶劣残留」:两条带 el/bel 的旧 doc 条目、旧 partial、entryId=5、
// 暂停中(paused=true + pauseChip 在场 + 按钮文案「恢复显示」)。
const els = {};
const removed = [];
const makeEl = id => ({ id, hidden: false, textContent: '', value: '', disabled: false,
  scrollHeight: 100, scrollTop: 0, clientHeight: 100,
  remove(){ this.removed = true; removed.push(id); },
  insertBefore(){}, appendChild(){},
  classList: { _s: new Set(), add(c){ this._s.add(c); }, remove(c){ this._s.delete(c); },
    contains(c){ return this._s.has(c); },
    toggle(c, f){ if (f) this._s.add(c); else this._s.delete(c); } } });
const $ = id => (els[id] || (els[id] = makeEl(id)));
const store = {};
const LS = { getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
// 上一场残留
const stale = { el: makeEl('stale.el'), bel: makeEl('stale.bel') };
els['pauseChip'] = makeEl('pauseChip');
$('barPause').textContent = '恢复显示';
// 上一场渲染态预置(防空真通过):空态已藏、partial 已写、typing 已挂(7.9 呼吸中)
$('barEmpty').hidden = true; $('plistEmpty').hidden = true;
$('barPartial').textContent = '上一场partial';
$('barPartialRow').classList.add('typing'); $('activeLine').classList.add('typing');
const bundle = ['var state = {phase:"idle", autoResume:false, sessionId:null, lastSeq:0,',
  '  userStop:false, reconnectTries:9, reconnectTimer:0, reconnectFinal:true, lock:null,',
  '  doc:[{el:stale.el, bel:stale.bel},{el:stale.el, bel:stale.bel}], partial:"上一场partial",',
  '  entryId:5, paused:true, title:"", speakers:0, lang:"zh>zh", startedAt:0, mode:"bar",',
  '  capture:null, pending:[], pendingBytes:0, droppedSec:0, batch:[], batchN:0};',
  'var removedBox = removed;',
  'var acquireLiveLock = () => Promise.resolve({release(){}});',
  'var startCapture = () => Promise.resolve();',
  'var connect = () => {};',
  'var setPhase = function(p){ state.phase = p; };',
  'var disarmReconnectChip = function(){};',
  'var startTimer = function(){}; var stopTimer = function(){};',
  fn('currentLangSpec'), fn('autoScroll'), fn('renderActive'), fn('fullRender'),
  fn('setMode'), fn('enterLiveUI'), fn('resetLedger'), fn('beginSession'),
  '$("fTitle").value = "新一场"; $("fSpeakers").value = "2";',
  '$("fSrc").value = "zh"; $("fTgt").value = "";',
  'beginSession();',
  'await new Promise(r => setTimeout(r, 20));',   // 锁 promise → startCapture promise 两拍放行
  'return state;'].join('\n');
const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor;
const endState = await new AsyncFunction('els', 'removed', 'localStorage', 'stale', '$',
  bundle)(els, removed, LS, stale, $);
let fail = 0;
const ok = (cond, what) => { if (!cond){ console.error('FAIL ' + what); fail++; } };
ok(stale.el.removed && stale.bel.removed, '旧场条目的 el/bel 未摘除(清数组不摘 DOM,旧行滞留账本)');
ok(endState.doc.length === 0, 'doc 未清空:旧条目带进新场(insertEntry 排序/去重全按每场 t0,跨场必错)');
ok(endState.partial === '', 'partial 未清空:旧文本连呼吸 caret 带进新会话开场');
ok(endState.entryId === 0, 'entryId 未归零');
ok(endState.paused === false, 'paused 未复位:它门控 setMode→fullRender,本次全部复位会被跳过');
ok(els['pauseChip'].removed, 'pauseChip 滞留 barState');
ok(els['barPause'].textContent === '暂停显示', '暂停按钮文案未还原');
ok(els['barPartial'].textContent === '', '新场开场 renderActive 未把 partial 行写空');
ok(!els['barPartialRow'].classList.contains('typing') && !els['activeLine'].classList.contains('typing'),
  '.typing 未随空 partial 摘除(7.9 呼吸 caret 会在新场开场空转)');
ok(els['barEmpty'].hidden === false, '空态「等待语音…」未回归');
ok(els['plistEmpty'].hidden === false, '面板空态未回归');
ok(els['activeLine'].hidden === true, '面板 partial 行未隐藏');
ok(els['panelTitle'].textContent === '新一场', '新场标题未上面板');
ok(endState.phase === 'connecting' && endState.reconnectTries === 0,
  'beginSession 既有残留复位(reconnectTries)不得回退');
console.log(fail === 0 ? 'WP7_REOPEN_LEDGER_VECTOR_OK' : fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);
'''


@unittest.skipIf(electron_missing(), 'Electron 二进制不存在(.tools/electron),跳过断言向量')
class WP7ReopenLedgerVector(unittest.TestCase):
    """评审追加行为向量:页内重开(被接管/结束→表单→goBtn,不刷新)后账本与 partial
    干净——beginSession→enterLiveUI→setMode→fullRender 全链真跑,断言旧场 el/bel 已摘、
    doc/partial/entryId/paused 复位、空态回归、.typing 摘除。静态钉只看得见「调用了
    resetLedger」,复位齐不齐(尤其摘 DOM 与 paused 门控)必须执行才知道。"""

    def test_vector_green(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as f:
            f.write(WP7_REOPEN_LEDGER_VECTOR_MJS)
            vec = f.name
        try:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
            proc = subprocess.run([str(ELECTRON), vec, str(LIVE_JS)], env=env,
                                  capture_output=True, timeout=60, cwd=str(BASE))
            self.assertEqual(proc.returncode, 0, '页内重开账本复位向量未全绿:%s%s'
                             % (proc.stdout.decode('utf-8', 'replace'),
                                proc.stderr.decode('utf-8', 'replace')[:600]))
            self.assertIn('WP7_REOPEN_LEDGER_VECTOR_OK', proc.stdout.decode('utf-8', 'replace'))
        finally:
            os.unlink(vec)



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
