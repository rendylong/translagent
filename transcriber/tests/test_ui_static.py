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
VECTORS = BASE / 'tests' / 'test_ui_route_vectors.mjs'


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
