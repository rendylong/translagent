# 实时字幕条按断句排布 spec(2026-10-24,方案 A 逐句流水)

> 评审记录:本文件已经 /plan-design-review 全量七维评审(2026-10-24,外部声音 codex+子代理双查),
> 修复已按推荐值合入;评审中四项决定与两项延后项待用户最终确认,见文末
> GSTACK REVIEW REPORT 的 UNRESOLVED DECISIONS。

## 1. 意图与背景

用户反馈:字幕条中字幕排版错乱;历史的字幕也要按断句来排布。

现状根因(交互原型「现状(问题)」档可复现,两位说话人模拟会话验证):

1. **整窗一坨**:pass2 / LLM 事件以整个时间窗为替换单位,一个窗口含 2~3 句话,
   `renderBar` 把最新窗口整段塞进主行 `#barText`,历史区 `.blog .e` 同样每个窗口
   一整块——句级可读性为零。
2. **任意断行**:`.bar .bmain .txt{overflow-wrap:anywhere}` 会在任意字符处换行
   (英文单词中间、数字中间都断),窄悬浮窗(420px)下尤其破碎。
3. **双轨割裂**:主行(barMain/barTrans/barPartial)与历史区(barLog)两套呈现,
   `syncBarLog` 靠隐藏当前主行条目防同句重复;主行是「窗口」级、历史也是「窗口」级,
   同一段话没有稳定的句子结构。

设计选型经交互原型四方案对比(现状 / A 逐句流水 / B 说话人分组 / C 当前句特写),
用户选定 **方案 A 逐句流水**:整个字幕条变成一个自底向上生长的「句子账本」——
一句一行、说话人切换才出圆点+名、译文小字跟句、正在说的句子是最底灰行。
主行/历史双轨整个取消。

## 2. 目标 / 非目标

**目标**
- 字幕条(悬浮窗形态 + 网页叠加态,同一 DOM)改为句子账本:一句一行,按断句排布;
- 译文按句对齐:译文用同一断句规则切分,句数一致时逐句跟随,不一致时该窗口译文
  整块回落(不猜对齐);
- 换行规则修正:句内换行用 `overflow-wrap:break-word`,删掉 `anywhere`;
- `renderBar` / `syncBarLog` 双轨删除,live.js 渲染层净简化;
- 空态、停止/结束态、灰行分层、最不利背景可读性、reduced-motion 降级全部显式定义
  (评审发现 1/2/3 补入)。

**非目标**
- 完整面板(模式 panel)排版不动(维持窗口条目式;后续如需按断句,复用
  `splitSentences` 另行提案);
- 不改 WS 协议与服务端,断句纯前端(协议里没有句边界,只能客户端切);
- 不做超长无终句(纯逗号长句)的强制切分——靠 CSS 换行,断错不如不断;
- 不动 entry.bel 增量维护机制本身(只换 bel 内部结构),不动暂停显示/重连重放逻辑;
- 不在悬浮窗内加产品名/品牌标识(评审 codex 提出,按极简 chrome 意图延后,见 NOT in scope)。

## 3. 关键事实与约束

- pass1_final 文本无标点(sherpa 流式裸文本),一行灰句即可;pass2 起 whisper 文本
  带完整标点,LLM 修正文本同样带标点——断句只在有标点的文本上生效。
- 译文是 LLM 窗口级输出(整窗一段 `translation`),协议不提供句级对齐;中文两句对
  英文两句是常态,但句数不保证相等(实测 LLM 会把一句中文译成两句英文)。
- 悬浮窗注入 CSS(`desktop/live-window.js` PAGE_CSS)已有
  `#barLog{flex:1 1 auto !important; max-height:none !important; min-height:0 !important}`、
  `#barLog{…-webkit-app-region:no-drag}`(滚动区必须 no-drag,保留)——桌面端
  **零改动,无需重打包**;悬浮窗早期「大面积空窗不可拖动」问题由页面侧
  `.bspace{-webkit-app-region:drag}` 解决(见 4.3)。
- 悬浮窗 bar 阶段窗口档 560×150(PHASE_MIN 420×110):扣除 34px 顶垫、statebar 与
  padding 后净高约 66px,约见 **3 句行**(评审勘误:原「4~5 行」算术乐观),chips
  出现时更少;维持档位不变,需要更多行拖高即可。
- 网页叠加态 `.blog` 现限高 66px(~3 行窗口块),句子行 13.5px×1.6≈22px,调整为
  132px(~6 句行),与今天「历史 3 行 + 主行区」总高相当。
- CJK 文本浏览器默认按字断行,`overflow-wrap` 只影响超长英文 token/URL。
- 半透明底+半透明白字的可读性:现状 rgba(255,255,255,.55) 文本在纯白网页垫底的
  最不利背景下对比度约 2.7:1(存量问题);本 spec 借灰行分层一并修正(见 4.3 验收值)。

## 4. 方案

### 4.1 断句函数 `splitSentences(text)`(live.js 新增,纯函数)

1. 预处理:空白串折叠为单空格、trim;空串返回 `[]`。
2. 终符集:`。!?!?…;;\n`(中日文句读 + ASCII `.!?;` + 换行)。
3. ASCII `.` 的三重守卫(命中则不切,继续累积):
   - 前后都是数字:`3.2`、`12.50` 不切;
   - 句尾词命中缩写表 `Mr/Dr/Ms/Mrs/Jr/Sr/vs/etc/e.g/i.e/Inc/Ltd` 不切(`Mr.`);
   - 下一字符是小写字母不切(`vs. the`)。
4. 连续终符(`好!!!`、`。。。`)并入前一句,不产生孤标点行/空行。
5. 尾巴无终符:自成一行(pass1 裸文本整条即此类)。
6. 省略号 `……` 视为单个终符(吸收成对字符后切分)。

### 4.2 行模型与 DOM

`#bar` 目标结构(live.html):

```html
<div class="bar" id="bar">
  <div class="grab" id="barGrab"></div>
  <div class="actions" id="barActions">(不变)</div>
  <div class="blog" id="barLog">(不再初始 hidden,进入 stage 常显)</div>
  <div class="statebar" id="barState">(不变,barChip 镜像仍挂这里)</div>
</div>
```

**删除** `#barMain`(barDot/barWho/barText)、`#barTrans`、`#barPartialLine`。

`#barLog` 运行时结构(顺序即视觉序):

```html
<div class="bspace"></div>                 <!-- 弹性空垫:短内容把句子压向底部;悬浮窗内为 drag 区 -->
<div class="e f1|p2|llm">…</div>           <!-- 每个 doc 条目一个容器,按 t0 序 -->
  ├─ <div class="srow …">一句</div>         <!-- 1..N 句行 -->
  └─ <div class="trblk">整窗译文</div>      <!-- 仅句数不一致回落时 -->
<div class="srow empty" id="barEmpty" hidden>等待语音…</div>  <!-- 空态,静态节点 -->
<div class="srow prow" id="barPartialRow">  <!-- 常驻最底:灰字 partial + caret,静态节点 -->
```

- **静态节点归属**:`#barEmpty` 与 `#barPartialRow` 是 live.html 静态节点,常驻
  `#barLog` 末尾;`insertEntry`/`fullRender` 只在 `#barEmpty` 之前插入 bel,
  **永不 remove**(评审发现:重建路径若清空 innerHTML 会瞬间废掉插入锚点)。
- **空态(评审发现 1)**:`#barEmpty`「等待语音…」灰字(.48),connecting 与 live
  阶段且 doc 与 partial 均空时显示;首个 entry 或 partial 到达即 `hidden`;
  `fullRender` 在空态时恢复显示。开场静默不再黑盒。
- 句行 `.srow` 结构:

```html
<div class="srow f1|p2|llm">
  <i class="sdot [sdot-none]"></i>   <!-- 仅说话人切换首句显示,否则透明占位(保持缩进) -->
  <div class="stx">
    [<b class="swho">说话人N</b>]     <!-- 与 sdot 同条件同现 -->
    句文本
    [<span class="str">该句译文</span>] <!-- 句数对齐成功时,块级小字 -->
  </div>
</div>
```

- skip 条目 → `<div class="skipline">· 静音段已略过 ·</div>` 居中小字,并重置
  说话人参照(下一窗口首句必出圆点+名)。
- **译文对齐**:译文过同一 `splitSentences`;句数与原文相等 → 逐句入 `.str`;
  不等 → 整段译文 `.trblk` 追加在该条目句行之后(不猜对齐)。
- **状态样式**:f1 行灰、p2/llm 行白、partial 行灰亮——三档 alpha 见 4.3(评审
  发现 2:f1 与 partial 不得同值);说话人颜色只落在 `sdot`/`swho`,正文不按
  说话人染色(原型 A 验证:颜色集中在圆点更干净)。
- **圆点归属**:条目创建/重建时,按 doc 中前一个非 skip 条目的 speaker 决定首句
  是否出圆点;**无前驱(会话第一条)视为说话人变化,出点+名**(评审发现 4);
  后续前驱被替换导致 speaker 变化不回溯修正(失真仅限圆点显隐一次,形态切换/
  暂停恢复时 `fullRender` 全量重算兜底)。

### 4.3 CSS(live.html `<style>`)

- **色板变量化**(评审 codex 发现 1):账本色板集中为 CSS 变量,避免 rgba 散落:
  `--lc-txt:#fff`(定稿句)、`--lc-f1:rgba(255,255,255,.48)`(pass1 待修正)、
  `--lc-partial:rgba(255,255,255,.68)`(正在说)、`--lc-tr:rgba(255,255,255,.62)`
  (译文)、`--lc-faint:rgba(255,255,255,.35)`(静音分隔)等,句行样式引用变量。
- **可读性验收值**(评审发现 2 + codex 发现 4,`bar` 底 `rgba(12,14,18,.60)` →
  **`.72**`,blur 不变;最不利背景=纯白网页垫底的验算):定稿句 #fff ≈ 8.1:1、
  partial .68 ≈ 4.8:1、译文 .62 ≈ 4.3:1(次级注释,接受)、f1 .48 ≈ 3.2:1
  (过渡态有意更暗,数秒内被 p2 替换)。f1 与 partial 拉开 20 个百分点,瞥读可分。
- 字体:沿用 body 既有 `-apple-system,BlinkMacSystemFont,"PingFang SC",…` 栈
  (macOS 原生覆盖层的平台惯例,产品既有约定;不引入 Web 字体)。
- `.blog`:`display:flex; flex-direction:column; min-height:0; overflow-y:auto`,
  顶部渐隐 mask 保留;网页叠加态 `max-height:66px → 132px`;
- `.bspace{flex:1 1 0; min-height:0; -webkit-app-region:drag}`:内容不足时句子贴底、
  空白在上;溢出时收缩为 0、正常滚动;悬浮窗早期空窗整块可拖动摆放(网页叠加态
  该属性无效,不受影响;桌面端仍零改动)。
- `.blog .srow{display:grid; grid-template-columns:10px 1fr; column-gap:8px;
  margin:3px 0}`;`.sdot` 7px 圆点,**多行句时 dot 顶对齐首行**(`align-self:start;
  margin-top:6px`);`.swho` 11px rgba(255,255,255,.62);`.stx` 13.5px/1.6 且
  `overflow-wrap:break-word`;`.str` 11.5px 块级 `var(--lc-tr)`;
  `.trblk` 11.5px rgba(255,255,255,.5) 左缩进 18px;`.skipline` 10.5px 居中;
  `.prow` 文本 `var(--lc-partial)` + 原有 blink caret(caret 语义 =「识别进行中」
  指示,1.1s blink 保留)。
- **动效克制**(评审 F6 + codex 发现 5):仅 `insertEntry` 新增条目带 0.18s 入场
  淡入;pass2/llm 的 `replaceWith` 重建路径**不带入场动画**(容器加 no-anim 类),
  防止窗口重排时全部历史句行集体眨眼;`@media (prefers-reduced-motion: reduce)`
  下入场动画与 caret 闪烁全部停用(caret 恒显,partial 静态)。
- 删除 `.bmain` / `.btrans` / `.bpartial` / `fallback` 相关样式,**以及旧
  `.blog .e .txt/.who/.tr/.dot/.skiptxt` 系**(评审 F7:防死代码误导后人)。
- 措辞勘误:现有 `.e .txt{…overflow-wrap:anywhere}` 是全局选择器(目前同时作用于
  旧字幕条历史);新类名 `.stx` 不受其影响,面板侧不动。

### 4.4 live.js 改动清单

1. 新增 `splitSentences` 与 `makeBarEl(entry)`(字幕条侧条目容器:句行 + trblk/
   skipline);`makeEntryEl` 保留但仅服务面板 `el`。
2. `insertEntry`:`entry.bel = makeBarEl(entry)`,插入目标由「next.bel 或 null(末尾)」
   改为「next.bel 或 `#barEmpty`(静态空态行之前,partialRow 之前)」;新增条目带
   入场动画,重建不带动画;`autoScroll($('barLog'))` 不变;删除 `$('barLog').hidden`
   切换(常显)。
3. 删除 `renderBar()`、`syncBarLog()`;`renderActive()` 扩展为同时更新
   `#barPartialRow` 的 partial 文本与 `#barEmpty` 显隐(暂停时 early-return 语义不变)。
4. `llmReplace` 软替换路径:重建 bel(`makeBarEl` → `replaceWith`,no-anim),
   句数/译文变化自然重排;`removeEntry`/`replaceWindow` 机制不变。
5. `fullRender()`:按 doc 序在 `#barEmpty` 前重建各 bel(bspace 与两个静态行不动),
   随后 `renderActive()`。
6. **停止/结束语义(评审发现 3)**:stopping 阶段尾部冲刷的 pass1_final 照常追加
   灰句(不丢已说内容);`stopped` 后 endwrap 遮盖、账本冻结不再更新;endwrap 可
   关闭,关闭后账本只读保留(与现状行为等价,仅补规格)。
7. `barChip` / `setChip` / 暂停显示 / 断线重连 / resume 重放路径全部不动
   (bel 同源增量,seq 去重后逐条重建)。

### 4.5 桌面端

零改动:`desktop/live-window.js`、preload、build-app.sh 均不触碰,无需重打包。
PAGE_CSS 既有注入(`#barLog{flex:1 1 auto}` + no-drag、顶部 34px 按钮区、缩放手柄)
即为目标形态;悬浮窗尺寸记忆/档位不变。`.bspace` 的 drag 区在页面 CSS 内定义,
不违反本条。

### 4.6 交互状态表(评审 Pass 2 补全)

```
特性        | 加载(connecting)      | 空                | 错误                 | 成功/进行            | 过渡
------------|------------------------|--------------------|----------------------|----------------------|--------------------------------
句子账本    | barEmpty「等待语音…」  | 同左(长静默)      | start 失败/会话失效  | 句行持续追加贴底跟随 | f1 灰(.48)→p2 白→llm 白+译文
译文        | —                      | —                  | → endwrap 错误卡(不变)| 逐句 .str 跟随      | 句数不等 → .trblk 整块(预期行为)
partial 行  | 空灰行 + caret 常闪    | caret(识别中)     | LLM 熔断: chip 提示, | 实时文本             | —
            |                        |                    | 句子停留 p2 态       |                      |
说话人标记  | 无(pass1 无 speaker)  | —                  | —                    | 切换首句出点+名      | skip 后重置;无前驱=出点
停止/结束   | —                      | —                  | 转正失败→stopped 无  | stopping 照常追加;  | —
            |                        |                    | job_id,音频留 live   | stopped 冻结+endwrap |
重连        | connChip+barChip(不变) | —                  | —                    | resume 重放重建 bel  | —
```

## 5. 边界与取舍

- **句数不等的译文**:回落整块,不猜句级对齐;同屏混排「跟句小字」与「组尾整块」
  两种形态是**预期行为**而非 bug(评审 F9:标注防实现者自行「修复」)。
- **pass1 裸文本**:一句(无标点)一行,pass2 窗口替换后原位重排为带标点句行,
  灰→白的跃迁即「修正上屏」的既有语义,只是粒度从窗口变到句子。
- **圆点失真**:前驱条目被替换后,后驱条目圆点不回溯(见 4.2),`fullRender` 兜底。
- **纯逗号长句**:不断句,整行靠 `break-word` 换行;宁长勿错。
- **网页叠加态高度**:132px 定值(~6 行),不做设置项。
- **账本 DOM 行数不设上限**(评审 Pass 7 决策):与现状一致不清理;量级验算 1 小时
  会话 ≈ 400~600 句 + 译文 ≈ 1000~1500 个节点,Chromium 滚动容器可承受;如长会话
  实测卡顿,再立软上限(丢最旧)——观察点,不是现在的需求。

## 6. 测试(live_page_selftest.py)

1. **改写 `test_bar_log_history`**:`entry.bel`、`autoScroll($('barLog')` 保留;
   新增钉子 `'splitSentences'`、`'makeBarEl'`、`'barPartialRow'` 在 js,
   `'id="barMain"'` 不在 html,`'.blog .srow'`、`'bspace'` 在 html,
   `'syncBarLog'`、`'renderBar'` 不在 js(双轨删除的回归钉子)。
2. **新增静态钉子(评审补)**:`'id="barEmpty"'` 在 html+js(空态)、
   `'prefers-reduced-motion'` 在 html、`.bspace` 所在行含 `-webkit-app-region:drag`、
   `'--lc-'` 色板变量在 html、no-anim 类存在。
3. **新增 SentenceSplit 用例**:从 live.js 源码提取 `splitSentences`(花括号配平,
   不靠 eval 整文件),经本机 `.tools/electron` 的 node 模式执行,断言向量:
   - 中文:`好的,先看数据。营收 320 万,涨 12%。` → 2 句;
   - 英文:`Revenue was 3.2M, up 12% QoQ. Margins dipped.` → 2 句(小数不切);
   - 缩写:`See Mr. Smith. He agreed.` → 2 句(`Mr.` 不切);
   - 连续终符:`好!!!知道了。` → 2 句(非 4 句);
   - 无终符尾巴 / 空串 / 纯标点;
   - 日文混排(。!? 全半角)。
   Electron 二进制不存在时该用例 skip(与协议段端口占用跳过同模式)。
4. `test_floating_window_injection` 的 `#barLog{flex:1 1 auto` 钉子原样保留
   (桌面端零改动的回归验证)。
5. 手测清单:悬浮窗 420px 窄宽中英混排换行;早期空窗拖动(bspace drag);空态文案
   出现/消失;网页叠加态 132px;f1/partial 灰行可分;窗口重排不眨眼;reduced-motion
   下无动画;暂停显示/恢复 fullRender;断线重连 resume 全量重放;静音分隔行与
   说话人圆点归属;停止后尾部灰句照常上屏。

## 7. 交付物

- `transcriber/static/live.html`(bar DOM/CSS)
- `transcriber/static/live.js`(splitSentences/makeBarEl/渲染层)
- `transcriber/tests/live_page_selftest.py`(钉子改写 + SentenceSplit)
- 桌面端无改动,无需 build-app.sh

## What already exists(评审:复用既有设计词汇)

- 说话人三色 `SPEAKER_COLORS`(#4C9AFF/#FFB02E/#98A2B3)、`.chip` 状态胶囊、blink
  caret、`.blog` 顶部渐隐 mask——全部沿用,不新造视觉词汇。
- `entry.bel` 增量维护机制、`autoScroll` ≤90px 跟随规则、`fullRender` 重建兜底、
  `barChip` 状态镜像——机制保留,只换 bel 内部结构。
- 交互原型(方案 A 档)是布局视觉的事实基准,数值以本 spec 4.3 为准(评审后
  灰行 alpha 与底色有更新)。

## NOT in scope(评审:显式延后项)

- **悬浮窗产品名/品牌标识**(codex 提出「首屏辨产品 NO」):个人工具+极简 chrome
  是产品意图,悬浮窗顶部已有操作按钮占位;延后,不为本 spec 范围。
- **DESIGN.md 设计系统文档**:仓库无设计系统文档,视觉约定散在 live.html;建议
  另立 TODO 沉淀(见实施任务 T4),不阻塞本 spec。
- 面板按断句排布、超长逗号句强制断句、账本行数软上限——见 §2/§5。

## Implementation Tasks

- [ ] **T1 (P1, human: ~4h / CC: ~30min)** — live.js/live.html — 账本化主体实现
  - Surfaced by: spec §4 全部(原型选型 + 评审发现 1/3/4)
  - Files: `transcriber/static/live.js`、`transcriber/static/live.html`
  - Verify: `cd transcriber && python3 tests/live_page_selftest.py`(静态段全绿)
- [ ] **T2 (P1, human: ~1h / CC: ~10min)** — tests — 钉子改写 + SentenceSplit 向量用例
  - Surfaced by: spec §6(评审 Pass 2/6 补钉)
  - Files: `transcriber/tests/live_page_selftest.py`
  - Verify: 同上;Electron node 模式跑真 `splitSentences`
- [ ] **T3 (P2, human: ~1h / CC: ~10min)** — live.html — 视觉规格落地:色板变量化、
  灰行分层(.48/.68/.62)、底色 .72、bspace drag、no-anim、reduced-motion、旧 CSS 清理
  - Surfaced by: 评审发现 2、codex 发现 1/4/5、F2/F6/F7
  - Files: `transcriber/static/live.html`
  - Verify: §6.5 手测清单前六项
- [ ] **T4 (P3, human: ~2h / CC: ~20min)** — docs — DESIGN.md 设计系统沉淀(待确认)
  - Surfaced by: 评审 Pass 5(无设计系统文档)
  - Files: `transcriber/DESIGN.md`(新建)
  - Verify: 后续 UI 改动以它校准

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | issues_open | score: 7/10 → 9/10, 9 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** APP UI 分类 0 硬拒绝;5 发现(色板变量/字体/品牌/对比度/caret)已合入 4、延后 1。
- **CROSS-MODEL:** codex 与子代理在「动效缺目的」上共识(已修:重建不眨眼+reduced-motion);子代理独有发现 F1/F2/F4/F5(空态/拖动死区/锚点/首条圆点)全部合入;litmus 7 项 4 过 1 败 1 不适用 1 分歧(品牌)。
- **VERDICT:** DESIGN CLEARED(7→9,余待确认项不影响实施启动)— eng review required。

**UNRESOLVED DECISIONS:**
- 发现1 空态:已按 1A「等待语音…」文案写入,待确认(替代:1B 文案+呼吸红点)
- 发现2 灰行分层:已按 2A 数值分层+底色 .72 写入,待确认(替代:2B 竖条不动色 / 2C 微调)
- 发现3 停止语义:已按 3A 照常追加+冻结保留写入,待确认(替代:3B 停止即冻结)
- 发现4 首条圆点:已按 4A 无前驱=变化写入,待确认(替代:4B 首条不出点)
- 账本行数上限:已按「不设上限」写入,待确认
- DESIGN.md 立项(T4)与悬浮窗品牌标识(NOT in scope):延后待用户表态
