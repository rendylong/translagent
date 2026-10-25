# 工作台设计评审修复 spec(2026-10-24)

> 来源:2026-10-24 四子 agent 设计评审(信息架构 / UX 流程 / 动效视觉 / /live 专项),
> 截图证据存 `/tmp/design-review-shots/`(7 张,1440×900,当日实机渲染)。
> 本 spec 由主 agent 汇总评审结论后撰写,所有 file:line 锚点已于 2026-10-24 当日逐条核实。
> 结论基调:**双栏骨架与账本模型保留,不推翻;修路由地基 + 8 个 P0 + 一批高价值 P1。**

## 1. 意图与背景

评审确认整体设计成立(侧栏=发起与管理、主区=内容;任务状态机五阶段可视化;句子账本
三级灰度;no-anim/reduced-motion 纪律;toast+行内二次确认),但存在一处机制性坏死与
若干数据/信任级问题:

1. **路由层名存实亡**:`index.html` 只在启动读一次 hash(1245-1249),无 `hashchange`
   监听,`setCurrent` 用 `replaceState`(832-835)。桌面端实时字幕结束卡「打开文稿」
   `open-main-job` 走 `loadURL(SERVER_URL + '/#' + id)`(desktop/main.js:568),主窗
   已开时是同文档 fragment 导航——**页面不重载,启动 hash 读取不再执行,只聚焦不跳转**
   (当日实机复现)。同源因连累:任务间无返回路径、说话人库视图刷新即丢、web 手改
   hash 无响应。
2. **视图状态无单一来源**:`setTab`(539-546)只切侧栏面板不碰主区;`libView` 与
   `current` 平行。可见症状:选中「现场录制/实时字幕」后主区仍是上传空态;说话人库
   占满主区时侧栏 Tab 仍高亮「上传文件」(`.side-foot.on`(147)是死样式,JS 从未加过,
   `libbtn.onclick`(1157-1162)只设 `libView=true`);`renderEmpty()`(836-845)重建的
   空态丢了波形图标与「实测参考」行(与初始标记 443-462 不一致);`#setrow-lock`
   (642)引用从未创建的元素(死守卫);`#lib-cnt`(439)从未被赋值。
3. **「实时字幕」是类别错位的 Tab**:上传/录制是「往本页投喂任务」,实时字幕「不在本页
   发生」——点开后主区零响应,面板内容(424-431)是一段解释「界面在别处」的散文。
4. **隐私文案失实**:`live.html:293`「全程仅在本机使用」,但翻译目标默认选中「英文」
   (290),tgt≠src 时窗口文本按 2026-10-23 授权送 MiniMax 云端。
5. **说话人色板跨端不同源**:`live.js:24` 仅 3 色(第 3 档灰,3 人以上全员灰)vs
   主台六色(`index.html:18-23`)。「颜色=人」的跨会议心智被打破。
6. **文稿页改名静默全局生效**:chip 带 gid 时 `onchange` 直接
   `PATCH /api/speakers/<gid>`(index.html:1004-1007),后端 `speaker_rename`
   重生成所有引用文稿(app.py:919-930),UI 无任何范围提示;且正文 `draw()` 只读本地
   `names`(1099),与 chip 显示的全局库名不一致。
7. **/live 双开 = 会话劫持**:`window.open('/live?mode=panel', '_blank')`(index.html:562)
   无命名 target;`tryAutoResume`(live.js:909-931)无单实例守卫;服务端 resume
   (app.py:1088-1112)对任意新连接受理并再挂一条 pump,两条 pump 竞争消费同一
   tap 队列,双路供音打乱时间轴。
8. **转正失败结束卡仍报喜**:`live_session._finish`(312-330)失败路径发无 job_id 的
   `stopped`(330),前端 `onStopped`(live.js:261-292)不区分,照样显示「录音已保存 ·
   正在生成完整文稿」;错误只在 stopChip 上停 6 秒(256)且被结束卡盖住。

## 2. 目标 / 非目标

**目标**
- 建 hash 路由层(`#` / `#<job_id>` / `#speakers`)+ 单一视图状态机,桌面深链、
  返回键、说话人库路由一次性救活;
- 实时字幕入口从 Tab 降为侧栏动作行(含会话进行中状态),Tab 条回归「投喂任务」两类;
- 修全部 8 个 P0(隐私文案、色板、全局改名、双开守卫、结束卡分态在内);
- 修高价值 P1 快修包(标题错挂、diarizing 放弃不生效、live 关页无拦截、suspect 无
  引导、删除不可逆无提示、搜索大小写、live 默认命名、f1 灰层、caret 常闪、
  reduced-motion 缺口、toast 入场、轮询全量重建、重启文案打架);
- 动效收敛为最小 token 表,主台补一次性过渡,不新增装饰动画。

**非目标(本期不做,见 §10)**
- 系统通知(17 分钟旅程闭环)、设置域统一、历史检索、自绘波形播放器、字号三档、
  说话人 chips 折叠、web /live 关闭后账本只读保留、Electron 端任何改动
  (desktop/main.js 与 live-window.js 本期零改动)。

## 3. 现状核实(2026-10-24,逐条验证)

| # | 事实 | 证据 |
|---|------|------|
| 1 | hash 仅启动读一次,无 hashchange | `index.html:1245-1249`(全文件无 `hashchange` 字样) |
| 2 | `setCurrent` 用 replaceState | `index.html:832-835` |
| 3 | 桌面深链 loadURL 只差 fragment | `desktop/main.js:562-568`(`!mainRecording` 时 `loadURL('/#'+id)`) |
| 4 | setTab 不碰主区;libbtn 无激活态 | `index.html:539-546`、`1157-1162`;`.side-foot.on`(147)死样式 |
| 5 | renderEmpty 丢图标与实测行 | `index.html:836-845` vs 初始标记 `443-462` |
| 6 | 翻译默认英文 + 失实文案 | `live.html:290`(en selected)、`293` |
| 7 | live 表单默认「2 人」,批量默认「自动」 | `live.html:272` vs `index.html:514`(`speakers ?? '0'`) |
| 8 | live 色板 3 色 | `live.js:24`;主台六色 `index.html:18-23` |
| 9 | gid 改名直接 PATCH 全局,无提示 | `index.html:1004-1007`;后端重生成 `app.py:919-930` |
| 10 | 正文标签不读全局名 | `index.html:1099`(`names[sp] || '说话人N'`);displayName(1037-1045)仅 chips 用 |
| 11 | resume 无单写者语义 | `app.py:1088-1112`(每次 resume 新挂 pump);`live_session.py:68-75`(_SESSIONS 注册表) |
| 12 | 结束卡不区分失败 | `live_session.py:312-330`(324 空会话、330 转正失败,均无 job_id);`live.js:269-292` |
| 13 | resetDrop 不清标题 | `index.html:587-593`;提交读旧值 `596-600` |
| 14 | diarizing 不可放弃 | 回调恒 True `app.py:525-530`;阶段边界检查 498/509/522/543;cancel 仅 kill whisper `749-760` |
| 15 | live 无 beforeunload | `live.js` 全文无;主台有 `index.html:738-743` |
| 16 | 删除即 rm -rf 不可逆 | `app.py:696-702`;确认文案仅「确认?/再点一次确认删除」 |
| 17 | 搜索大小写敏感 | `index.html:1100-1102`(`includes(q)`) |
| 18 | f1 恒灰(无修正会话) | `live.html:15`(--lc-f1 ≈3.2:1,注释「过渡态」);仅 p2/llm 升白 `live.html:98` |
| 19 | caret 恒闪 + steps 硬切 | `live.html:129-130` |
| 20 | reduced-motion 缺口 | `live.html:132-135` 仅覆盖 `.blog .e` 与 `.caret`(面板 `.e` 176、`.phead .pulse` 未覆盖);`index.html` 全文无 reduce 块 |
| 21 | toast 无入场;JS/CSS 时长不一致 | `index.html:480-486`(350ms remove vs CSS .3s) |
| 22 | 轮询全量重建历史 | `index.html:751-823`(`wrap.innerHTML=''` 每 2.5s) |
| 23 | 重启文案打架 | `app.py:985`「请删除后重新上传」 vs retry API(762-774)与前端「↻ 用原音频重跑」 |
| 24 | web 叠加态操作按钮 hover 才显 | `live.html:116-120`;桌面强制常显 `desktop/live-window.js:133-135` |
| 25 | 重连无终态 | `live.js:133-140`(3s 无限循环) |
| 26 | ~~sherpa 支持回调中止~~ **eng review 勘误:返回值被忽略** | `app.py:492` 透传的 callback 在 sherpa-onnx 2.0.0 中返回值不被检查(C++ 源码 `callback(k, ans.rows(), callback_arg)` 裸语句;docstring 失实)。中止机制 = 回调内抛异常(实测 2.5s 传播出 process,实例无损) |

## 4. 工作包

依赖关系(WP1 是地基,先做;WP2 依赖 WP1 的视图状态机;其余可并行):

```
WP1 路由+视图状态机 ──> WP2 实时字幕动作行
   │
   ├──> WP4 隐私与翻译(独立)
   ├──> WP5 色板与身份一致(独立)
   ├──> WP6 结束卡分态(独立)
   └──> WP7 快修包(独立)
WP3 会话守卫(前端部分依赖 WP2 的命名窗口)
```

### WP1 路由与视图状态机(index.html)

**改法**

1. 新增单一视图状态 `view = {type: 'empty'|'job'|'lib', id?}`,所有主区渲染
   (`renderEmpty` / `load()` / `showLib()`)收口到一个 `render(view)` 入口;
   `current`、`libView` 变量删除或降级为 `view` 的派生。
2. 新增路由解析 `applyRoute()`:`''`/`'#'` → empty(空态文案随侧栏当前 Tab 出
   upload/record 两变体);`'#speakers'` → lib;`#<id>` 匹配 `^[\w-]+$` → job。
   监听 `hashchange` 与 `popstate`,二者都调 `applyRoute()`(幂等:目标视图已渲染
   则不动)。
3. **单一写路径(eng-review 修订,跨模型一致)**:程序化导航一律 `location.hash = …`
   赋值(自动入历史栈 + 触发 hashchange),`setCurrent` 内的 `replaceState`(832-835)
   改为该赋值;**不引入 pushState**(不触发 hashchange,与 hash 赋值并存必然双处理
   或漏处理)。hashchange 与 popstate 都进 `applyRoute()`,靠幂等短路吸收双发。
   删除任务/清空时赋 `'#'`;任务间、任务↔库间浏览器返回键免费获得。
3b. **loadSeq 代际守卫(eng-review 建议,待确认)**:`load()` 为 async 且无 in-flight
   保护(index.html:800 现状即如此),hash 化后快速连点两个任务会慢者后到覆盖新视图。
   加模块级 `loadSeq` 计数,fetch 前快照、render 前校验,过期即弃(两行改动)。
4. `libbtn.onclick` 改 `navigate('#speakers')`;库视图激活时 `libbtn` 加 `.on`、
   三个 tab 的 `.on`/`aria-selected` 清空;离开反向恢复。`lib-cnt` 由
   `ensureSpeakers()` 的 `suspects.length` 填充(0 时隐藏),confirm/separate/改名后
   刷新。`.side-foot.on` 样式启用。
5. `renderEmpty` 合并为唯一实现,补齐波形 SVG 与「实测参考」行(与 443-462 一致);
   空态两变体:upload Tab → 现文案;record Tab → 「按下开始录制,录音会出现在这里」。
6. 清理 `#setrow-lock` 死守卫(642)。
7. 兼容性:`/#<job_id>` 深链格式不变(桌面端已发版行为);`desktop/main.js` 零改动,
   fragment 导航触发的 hashchange 自然生效。

**验收**
1. 主窗已开时,悬浮窗结束卡「打开文稿」→ 主窗聚焦**且**切换到该任务文稿(实机)。
2. 主窗录音中时点击「打开文稿」→ 仅聚焦不跳转(现状保留,`!mainRecording` 语义)。
3. 历史连续打开两个任务后按浏览器返回键,回到前一个任务;返回到 `#` 显示空态。
4. `#speakers` 直开/刷新仍在库视图;侧栏底部按钮高亮、Tab 全不选中;`lib-cnt`
   显示待确认数(有 suspects 时)。
5. 选中「现场录制」后删除当前任务 → 主区显示录制变体空态(不再出现「上传一场…」)。
6. `renderEmpty` 渲染结果与初始标记含相同元素集合(SVG、h1、p、acts、meas)。

### WP2 实时字幕入口:Tab → 侧栏动作行(index.html + app.py)

**决策记录**:采纳 IA 评审推荐方案 A(动作行),否决「保留 Tab + 主区联动」——
动作行形态与「不在本页发生」的行为一致,且消除 Tab 契约破坏。

**改法**

1. 移除 `tab-live`(347-357)与 `pane-live`(424-431),`TAB_LIST` 收敛为两 Tab。
   `setTab('live')`、`live-go`(553-563)相应删除。
2. 「新任务」卡下方新增动作行 `#liveRow`:左侧红点(空闲灰/进行中红+呼吸)+ 文案
   (空闲:「实时字幕 · 开悬浮窗或新标签页」;进行中:「字幕进行中 · 已开 HH:MM」),
   整行可点。行为与现 `live-go` 一致:桌面 `desk.openLiveCaption()`;web
   `window.open('/live?mode=panel', 'livePanel')`(**命名 target**,防双开标签)。
3. 新增 `GET /api/live/state`:遍历 `_SESSIONS`(经 `live_session` 导出的查询函数,
   如 `active_sessions()`,须持 `_SESSIONS_LOCK`),返回 `{'active': bool,
   'status': str|null, 'title': str|null, 'started_at': int|null}`——active 涵盖
   `listening` 与 `stopping`(转正排空期间 GPU/管线仍被占,红点不应提前熄灭);
   多个活跃会话取 `started_at` 最新者。动作行用**独立的常驻慢定时器**驱动
   (页面可见时 10~15s 一次;eng-review 勘误:现状无任务时不轮询
   ——`pollTimer` 仅在有 running 任务时创建,index.html:820-822,「随 refresh
   轮询」的前提不存在);任务轮询 tick 时顺带刷新一次。
4. 空态不再有 live 变体(WP1-5 只有 upload/record 两变体)。

**验收**
1. 侧栏无第三 Tab;动作行在两 Tab 下均可见可点。
2. web 点击只开一个 `livePanel` 标签,重复点击聚焦同一标签。
3. 有进行中会话时动作行红点亮起并显示开始时间;会话结束回到空闲态。
4. `/api/live/state` 无会话返回 `{'active': false, 'title': null, 'started_at': null}`。

### WP3 /live 会话守卫(live.js + live.html + app.py + live_session.py)

**决策记录**:resume 冲突采用**拒绝新连接**而非接管——双开多为误操作,保留先到者
更安全;接管需通知旧连接停麦,复杂度不成比例。

**改法**

3. **服务端单写者 = 接管语义(eng-review 修订,取代原「拒绝新连接」)**:原方案会
   杀死它想保护的重连路径——`pump_events` 退出条件是 gone 置位或连续 3 次 send 失败
   (live_session.py:139-160),非干净断连(掉 Wi-Fi/合盖)时旧 pump 可长期占着附着
   标志,同一页面 3 秒自动重连(EventTap 机制本身就是为 ≤0.5s 重连设计的,
   live_session.py:96-101)会被当成「第二窗口」拒绝,断线缓冲+resume 整体失效。
   改为:`LiveSession` 维护每会话连接注册(conn_id → pump/gone),resume 到达时:
   - 已附着连接存在 → 置其 gone(旧 pump 按序归还事件后退出),并先向旧连接发送
     `{'type':'session_taken_over'}` 再关闭;新连接完成 backlog 重放并挂 pump;
   - 前端收到 `session_taken_over` 为**终态**:停止自动重连、停麦、清 session 存储、
     回表单态显示「会话已在其他窗口打开」(与 userStop 同级,防止 A/B 双页 3 秒
     互踢的死循环);
   - 同页快速重连天然被接管语义治愈:新连接即当前接管者,无需区分「同一客户端
     重连」与「第二窗口」。
2. 前端 `window.open` 命名 target(WP2-2)已挡大部分双开;`tryAutoResume` 前加
   `navigator.locks.request('live-session', {ifAvailable:true}, …)` 占锁(Web Locks,
   Electron 45 与现代浏览器均支持;锁不可得 → 不恢复,表单出「字幕会话已在其他
   窗口进行」);`start()` 正常路径同样先取锁,会话结束/出错释放。锁不可用环境
   (极老浏览器)降级为无锁(服务端守卫兜底)。
3. 守卫与既有自愈的回归合并:test_ws_live 既有 autoResume/死会话用例必须全绿
   (重连路径不被守卫破坏是本项的一票否决验收)。

**验收**
1. 同一会话第二条连接 resume → 旧连接收到 `session_taken_over` 后收尾(停麦、回
   表单态、不再重连),新连接接管 backlog 与 pump(双页 smoke:接管后事件只到
   新连接,旧连接无续投)。
2. **同页断线重连不被误伤**:模拟客户端断开(不置 gone 的非干净断开)后 3s 重连
   resume 成功,事件按 seq 续投不丢失(EventTap 归还队列语义不回退)。
3. 同标签重复打开 `/live?mode=panel` 聚焦原标签而非新开会话。
4. `navigator.locks` 被占时,新开的 /live 显示占用提示,不触发 getUserMedia。
5. 既有 autoResume 自愈(死会话回表单+提示)行为不回退(test_ws_live 既有用例全绿)。

### WP4 隐私与翻译(live.html + live.js + index.html)

**决策记录**:翻译默认从「英文」改为「不翻译」——外发必须显式选择,这是隐私立身
产品的默认值纪律。

**改法**

1. `fTgt` 默认选中 `value=""`(不翻译)(live.html:290)。
2. note 动态化(替换 live.html:293 静态文案):
   - tgt==src 或未选:「识别与说话人分离即时可用,全程在本机完成;修正与翻译事件
     到达时自动上屏。」
   - tgt≠src:「识别与说话人分离在本机完成;**修正与翻译会把窗口文本(不含音频、
     路径、人名)发送至外部 LLM 服务**。」
   - `fTgt.onchange` 即时切换;开始后 `started.llm===false` 且 tgt≠src 的既有
     stopChip 提示(live.js:165-168)保留。
3. 统一两处入口文案口径:`live.html:262` sub 与 index 实时字幕入口(移入动作行文案)
   统一为「音频不出本机;开启翻译时,识别文本交由外部 LLM 修正与翻译」。
   `live.html:350` 运行中 hint(仅说音频)保持不变。
4. **旧默认值迁移(eng-review 补)**:存量机器的 localStorage 里可能存着旧默认
   `zh>en`(live.js:817-819 `langFromStorage` 与 :917 resume 路径都会回落/读取它),
   只改 HTML selected 对老用户不生效。一次性迁移:读到的 `live.lang === 'zh>en'`
   (恰为旧默认)时清除该键,让新默认「不翻译」生效——显式选过 zh>en 的用户被
   重置一次,可接受(隐私收益优先);其余合法值不动。

**验收**
1. 打开 /live 默认「不翻译」,note 无外发字样。
2. 选「英文」后 note 立即出现外发说明;改回「不翻译」立即消失。
3. 全文检索「仅在本机/不经任何外部服务」不再出现于会开启翻译的表单语境。

### WP5 色板与说话人身份一致(live.js + index.html)

**改法**

1. `SPEAKER_COLORS`(live.js:24)改六色,取主台 `--sp0-ink..--sp5-ink` 的字面值
   (`#1d5290,#1f6b45,#8f520c,#65449f,#14615f,#96365a`),行注释「与 index.html
   --sp*-ink 同源,改动须两处同步」;`colorFor`(live.js:485-486)回落逻辑改为
   `speaker % 6`,≥6 人循环(主台 `.c0-.c5`、`.s0-.s5` 亦按 `% 6`,行为对齐)。
   深色底上六色为深色 ink,可读性不足——因此 live 侧实际使用的是这六个色相的
   **亮化变体**(按原 3 色的明度风格提亮,如 `#4C9AFF→#6db3ff` 系),六条值写入
   live.html `:root` 作 `--lsp0..5`,与 index 的色相一一对应并注释映射关系;
   「同一个人跨端同色相」是硬要求,明度允许按底色分档。
2. 正文标签统一走 `displayName()`:draw()(1099)改
   `displayName(s.speaker) || ('说话人' + (s.speaker + 1))`,搜索过滤的 `nm` 同源;
   全局改名后正文与 chips 一致(chips 本就先本地后全局,改名仅全局时正文随库更新)。
3. chip 改名双路径(替换 1004-1007 的静默 PATCH):gid 且新名 ≠ 当前显示名时,
   在 `.spkrow` 下出一条行内确认条(复用 `.suggest` 样式族,不新建组件):
   「「{名}」应用到哪些会议?」`[应用到所有会议]` + `[仅此会议]`。
   **不显示会议数**(eng-review 勘误:库页虽把 `n_anchors` 当「N 场会议」渲染
   (showLib),但弱证据门(<3 段不加锚)下锚点数 ≠ 会议数,写数字必错;数字
   修正需要后端新增跨任务扫描,超出本期)。前者走现 PATCH /api/speakers + toast
   「已全局改名,相关会议文稿已更新」+ `ensureSpeakers(true)`;后者走现任务级
   `PATCH /api/jobs/<id>`(speaker_names)+ `names[sp]=v`,不动全局。

**验收**
1. 同一说话人在 /live 与文稿中颜色同色相;3 人以上会议在 /live 中不再共享灰色。
2. 库里改名「龙海」后,其历史任务正文标签同步显示「龙海」。
3. 文稿页改 gid 说话人名字必出行内确认条;选「仅此会议」后库与其它会议不变;
   选「所有会议」后,本篇**无本地覆盖名**的说话人正文即时更新(有本地覆盖名时
   仍显示本地名——「任务级覆盖 > 全局库」的既有优先级,属设计而非缺陷)。

### WP6 结束卡分态与寻回(live_session.py + live.js + live.html)

**改法**

1. 后端 `stopped` 事件带 `reason`(live_session.py `_finish`):
   - 空会话(324):`{'type':'stopped','reason':'empty'}`;
   - 转正失败(330):`{'type':'stopped','reason':'finalize_failed','error': 简要}`;
   - 成功(327):带 `job_id`,无 reason(不变)。
2. `onStopped`(live.js:269-292)按三分支渲染:
   - job_id(成功):标题「文稿已生成」;**主按钮 = 打文稿**(endLink 换 `.ego` 主
     样式),「开始新会话」降为 `.ego.ghost`;web 端 endLink 改
     `window.open('/#'+id, '_blank')`(避免 live 标签页内导航造成双工作台;
     桌面端 openMainJob 路径不变);
   - finalize_failed:标题「文稿生成失败,录音已保留」;sub 含 error 摘要 +
     「录音仍在服务端,重启转写服务后会自动转为任务(历史记录带「实时」标记)」;
   - empty:标题「本場没有录到语音」;sub「会话已结束,未生成文稿」。
3. 失败/空会话分支不再显示「正在生成完整文稿」字样。
4. **立即转正重试(eng-review 新增)**:失败寻回现文案是「重启转写服务后会自动
   转为任务」——`live_recover()`(app.py:1003)确实只在启动时跑,让用户为寻回
   音频重启整个服务太重。新增 `POST /api/live/recover_now`:复用 `live_recover`
   的目录扫描与 finalize 逻辑,**但排除 `_SESSIONS` 中活跃会话的目录**(运行期
   调用与启动期不同:必须避免把正在录制的会话目录拿去转正),返回新建 job 列表;
   失败态结束卡加「立即重试转正」按钮,成功后原位转成功态(带 job_id 深链)。
   幂等性沿用 live_recover 对已终态目录的跳过逻辑。

**验收**
1. 人为注入 finalize 失败(mocks finalizer,沿用 test_live_session 既有夹具)→
   结束卡为失败态且 sub 含寻回文案;`reason` 字段在 ws 事件可断言。
2. 空会话停止 → 「没有录到语音」态。
3. 成功态主按钮为「打开文稿」;web 点击开新标签,原 /live 停在结束卡。

### WP7 快修包(逐条:现状 → 改法 → 验收)

| # | 项 | 现状(锚点) | 改法 | 验收 |
|---|----|------------|------|------|
| 7.1 | 上传后清标题 | `index.html:587-593` 不清 `#title` | `resetDrop()` 增 `$('#title').value=''` | 上传 A(填标题)完成后再传 B,B 标题为空,默认用文件名 |
| 7.2 | diarizing 可放弃 | 回调恒 True(`app.py:525-530`);**eng review 实测(2026-10-24):sherpa-onnx 2.0.0 忽略 progress 回调返回值**——True/0 各 35 次回调均完整跑完(master 源码核实:返回值从未被检查;安装版 docstring「Return a non-zero value to abort」失实) | **回调内抛异常中止**(实测:回调 raise `RuntimeError('已放弃')` 可在首次回调处(~2.5s/60s 音频)传播出 `sd.process`,实例复用无损):`on_diar_progress` 改为 `if read_meta(job_id).get('cancel_requested'): raise RuntimeError('已放弃')`;worker 外层 `except Exception` 收敛 error='已放弃',errBrief 既有映射(index.html:850「已手动放弃,可用原音频重跑」)。TypeError 回落路径(旧 sherpa 无 callback 参数)维持阶段边界放弃,注释说明 | 注入 cancel_requested 后 diar 秒级中止,任务终态 error「已放弃」;运行页文案加「说话人识别阶段放弃将在数秒内生效」;回归测试 = mock run_diarization 首次回调即 raise(快)+ 可选真模型探针(慢,默认 skip) |
| 7.3 | live 关页拦截 | `live.js` 无 beforeunload | `phase ∈ {connecting, live, stopping}` 注册 beforeunload(ended/idle 不拦,复用主台 738-743 模式);桌面悬浮窗形态(`window.desk`)跳过(关窗已有 main.js 拦截) | web 进行中关标签弹原生确认;结束/表单态关标签不弹 |
| 7.4 | suspect 角标 | `#lib-cnt`(439)从未赋值 | WP1-4 已含;此处验收:产生 suspect 的任务完成后角标 +1,确认/另存后归零 | 同左 |
| 7.5 | 删除确认文案言明不可逆 | 「确认?」/「再点一次确认删除」 | 历史项(809)与文稿页 `#del` 的 confirmText 统一改「删除后音频与文稿不可恢复,确认?」 | 文案可见;两击确认流程不变 |
| 7.6 | 搜索大小写 | `includes(q)`(1100-1102) | 双侧 `toLowerCase()`(含 `hl()` 高亮匹配同一折叠) | 英文文稿搜小写命中大写词,高亮位置正确 |
| 7.7 | live 默认命名 | 标题空 →「实时会议」无区分度(live.js:915 附近) | 开始时标题为空则生成「实时会议 M月d日 HH:MM」(与录音任务 660 对齐),随 start 传入 | 不填标题开始 → 历史/文稿显示带时间标题;手填标题优先 |
| 7.8 | f1 无修正会话升白 | `--lc-f1` 3.2:1 恒灰(`live.html:15`) | `started.llm===false` 或 lang tgt==src 时根节点加 `llm-off` 类;CSS 增 `.llm-off` 下 f1 句 `color:var(--lc-txt)`(账本与面板两侧;译文层 `--lc-tr` 不变) | 不翻译/无 key 会话中,pass1 句上屏即为定稿白;有 key 且开启翻译的会话维持灰→白渐进 |
| 7.9 | caret 条件闪烁 | 恒闪 + steps(129-130) | 仅 partial 非空时给行加 `.typing`;`.caret` 默认 `opacity:0`,`.typing .caret` 平滑呼吸 keyframes(`50%{opacity:.15}`,ease);reduced-motion 下 typing 时恒显 | 静默期无闪烁;说话中平滑呼吸;reduce 下静态可见 |
| 7.10 | reduced-motion 兜底 | 主台无 reduce 块;live 漏面板 `.e`、`.phead .pulse`(132-135) | 两文件各加通用兜底(`animation-duration:.01ms!important; animation-iteration-count:1!important; transition-duration:.01ms!important`);live 现有细粒度块保留(caret 语义靠 7.9 的静态可见兜住) | 系统「减弱动态效果」开启后:rec-blink/pulse/caret 全静态,进度条与浮层瞬时至 |
| 7.11 | 网页叠加态按钮常显 | hover 才显(`live.html:116-120`) | 删除 hover 门控,`.bar .actions` 常显(桌面 PAGE_CSS 的 `!important` 覆盖成为冗余可留) | 网页叠加字幕条右上角三按钮直接可见可点 |
| 7.12 | toast 入场与时长 | 无入场;350ms remove vs CSS .3s(480-486) | 入场 `fade + translateY(-6px→0)` 200ms;退场 CSS 280ms 对齐 JS 300ms;err 类 ttl 7s,普通 4s | toast 出现有过渡;错误消息可读完 |
| 7.13 | 轮询不再全量重建 | 每 2.5s `innerHTML=''`(751-823) | refresh 前算任务列表签名(status/stage/progress/queue_pos/current);无变化直接返回;仅 running 项字段变化 → 按 `data-id` 就地更新该行 `.sub` 与进度条宽度;结构变化(增删/分组迁移/终态)才全量重建 | 处理中任务跑动时:「确认?」武装态不被打断,列表滚动位置不动;任务终态到达时列表正常重排 |
| 7.14 | 重连终态 | 3s 无限重连(133-140) | `reconnectTries` 计数,>10 次 → 停止自动重连;chip 改「无法连接转写服务,请确认服务已启动 · 点击重试」,chip 可点手动 `connect()`(计数归零) | 杀掉服务 30s 后 chip 停在终态文案;重启服务点 chip 恢复 |
| 7.15 | 重启文案统一 | `app.py:985`「请删除后重新上传」 | 改「服务重启导致中断,可用原音频重跑」 | 重启后失败任务的详情页提示与重跑按钮口径一致 |
| 7.16 | 动效 token 与过渡 | 全硬切;5 种时长散落 | `:root` 增 `--t-fast:120ms; --t-med:180ms; --t-ease:cubic-bezier(.2,0,0,1)`;浮层(`#setpop`、导出 menu)入场 `opacity+translateY(4px)` 180ms;主区视图容器一次性 `.view-in`(fade 180ms);进度条 transition `.8s`→`600ms linear`(132/179 两处) | 打开设置弹层有入场;主区切任务有轻淡入;进度条更新不迟滞 |
| 7.17 | 逗号行首 | `</b>,` 边界断行(index 实时字幕文案,live 同查) | 逗号移入 `<b>` 内;两文件 grep `</b>,|</b>。` 全清 | 窄栏(≤360px 侧栏宽)下无行首逗号 |
| 7.18 | audio 控件融入 | `preload="none"`(945)、无 accent | `audio{accent-color:var(--primary)}`;`preload` 改 `metadata` | 播放器进度色与主题一致;打开文稿即显示真实时长 |
| 7.19 | cancel 误杀风险(eng-review 新增) | `job_cancel` 对 `worker_pid` 无条件 `kill`(app.py:755-757),不像 `recover_interrupted` 有 `ps -o comm=` 含 'whisper' 的校验(982-984);whisper 退出后 pid 复用可误杀无辜进程。7.2 让 diarizing 放弃真正可用后,该路径使用频率上升 | kill 前加与 recover_interrupted 同款的 comm 校验 | cancel 只杀 whisper 进程;单测:mock pid 为非 whisper 进程时不 kill |

## 5. 不变量(不要动)

- 句子账本三级灰度模型、no-anim 重建防眨眼机制、autoResume 死会话自愈语义;
- 声纹阈值(0.65/0.50/0.70×3)与 `canonicalize_clusters` 管线;`speakers.json` 结构;
- 任务状态机与 `/api/jobs` API 形状(仅新增 `GET /api/live/state` 与 `stopped.reason`);
- `/#<job_id>` 深链格式;单 worker 串行;音频不出本机约束(本 spec 无任何新外发,
  翻译外发沿用 2026-10-23 授权且默认关闭)。

## 6. 测试计划

| 层 | 内容 | 落点 |
|----|------|------|
| pytest(后端) | `stopped.reason` 三分支;resume 拒绝第二连接;`/api/live/state`;diar 中止(worker 层:mock `run_diarization` 首次回调即 raise `RuntimeError('已放弃')` 断言任务收敛 error + errBrief 映射;可选真模型探针测试默认 skip);重启文案 | `transcriber/tests/test_live_session.py`、`test_ws_live.py` 扩展;新增 `test_live_state_api.py` |
| 静态钉子(JS 源) | hashchange 监听存在;`window.open(...,'livePanel')` 命名 target;`fTgt` 默认 selected 为空;note 动态函数存在;`SPEAKER_COLORS` 六条且注释含「同源」;`llm-off` CSS 与类切换;`navigator.locks` 守卫;live beforeunload;两文件 reduce 兜底块;`</b>,` 零命中 | 新增 `transcriber/tests/test_ui_static.py`(对 index.html/live.html/live.js 源码断言,沿用评审钉子风格) |
| 页面自检(DOM) | 结束卡三分态结构;`llm-off` 类切换;.typing caret 类;动作行 DOM 与状态点 | `transcriber/tests/live_page_selftest.py` 扩展 |
| 手工回归 | 桌面深链(主窗开/录音中两态);双开守卫(两标签同开);翻译默认与 note 动态;改名双路径;返回键路径;reduce 动效实机;1440×900 与 360px 窄侧栏截图对照 | 人工清单,完成后在本文档勾销 |

基线:改前先跑 `python3 -m pytest transcriber/tests/ -x -q` 记录基线(含已知预存失败,
见项目记忆),改后不得新增失败。

## 7. 回滚

纯前端为主(index.html/live.html/live.js 单文件可整体 revert);后端仅三处小改
(`stopped.reason`、resume 拒绝、`/api/live/state`),均为加法,回滚即删。
`stopped.reason` 对旧前端是多余字段,向后兼容;`/api/live/state` 无消费者时无副作用。

## 8. 工作量

| 包 | 内容 | 估时 |
|----|------|------|
| WP1 | 路由+状态机+renderEmpty 合并+lib 激活态 | 0.5~1 天 |
| WP2 | 动作行+/api/live/state | 2~3h |
| WP3 | locks 守卫+服务端拒绝+恢复提示 | 3~4h |
| WP4 | 默认值+动态 note+文案 | 1~2h |
| WP5 | 色板映射+displayName+改名双路径 | 3~4h |
| WP6 | reason 三分支+结束卡布局 | 2~3h |
| WP7 | 18 条快修 | 1 天 |
| 测试 | pytest+钉子+自检+手工清单 | 0.5 天 |

合计约 3~4 天;WP1+P0 子集(4/5/6/8)先行可半天内消除用户已会踩的断链。

## 9. 文件清单

| 文件 | 改动 |
|------|------|
| `transcriber/static/index.html` | WP1/2/4/5/7(路由、状态机、动作行、改名双路径、快修多数) |
| `transcriber/static/live.html` | WP4/6/7(默认值、note、结束卡样式、caret/reduce/按钮常显、token) |
| `transcriber/static/live.js` | WP3/5/6/7(locks 守卫、色板、onStopped 分态、重连终态、beforeunload、命名) |
| `transcriber/app.py` | WP2/3/7(`/api/live/state`、resume 接管、`recover_now`、diar raise、cancel comm 校验、重启文案) |
| `transcriber/live_session.py` | WP3/6(连接注册与接管、stopped.reason) |
| `AGENTS.md`、`transcriber/界面重设计-spec.md` | WP2 落地后同步「界面」节:三 Tab → 两 Tab + 实时字幕动作行(eng-review 补:本仓库约定改完同步文档,防漂移) |
| `transcriber/tests/*` | §6 各文件 |
| `transcriber/desktop/*` | **零改动**(仅回归) |

## 10. Out of scope(中期,另行提案)

系统通知与旅程闭环(完成/失败推送+suspect 引导条)、设置域统一
(`transcribe-settings` vs `live.*`)、历史检索、自绘波形播放器、字幕字号三档与
悬浮窗联动、说话人 chips 折叠(+K)、web /live 关闭后账本只读保留、暂停按钮进
panel 形态、麦克风电平条与断源检测、`.blog` 之外的主台暗色主题议题。

## 11. 本 spec 的显式决策(实施时不再问)

| 决策 | 选择 | 理由 |
|------|------|------|
| D1 实时字幕入口 | 动作行(非 Tab+联动) | 形态与行为一致;评审两案中推荐案 |
| D2 resume 冲突 | ~~拒绝新连接~~ **接管 + `session_taken_over` 终态(eng-review 修订)** | 原案会被非干净断连下的旧 pump 附着卡死,杀死 EventTap 设计的重连路径(§WP3-1 引证);接管 + 显式终态消息同时治愈重连与防双页互踢 |
| D3 翻译默认 | 不翻译 | 外发须显式;隐私默认值纪律 |
| D4 gid 改名 | 行内双路径确认 | 保留全局改名价值,补足知情;不改为纯任务级(会破坏既有跨会议习惯) |
| D5 结束卡分态 | 后端 stopped 带 reason | 前端不猜(空会话 vs 转正失败不可靠区分) |
| D6 色板同源 | live 侧六色相映射 + 明度分档 | 「同人同色相」硬要求,深底可读性靠明度档 |

## 12. 工程评审补记(2026-10-24 /plan-eng-review)

评审方式:四节评审(架构/代码质量/测试/性能)+ 外部声音(codex CLI 运行时故障,
按协议回退为同族子代理复核)+ 本地实证探针。已合入的修订:7.2 修法证伪重写
(sherpa 回调返回值被忽略,改为回调内 raise,探针双证)、D2 接管语义(OV1/架构
共识)、WP1 单写路径(OV2/架构共识,删 pushState)、WP2 独立慢定时器与 stopping
状态(OV5)、WP4 旧默认值迁移(OV3)、WP5 去计数与验收前提(OV4)、WP6
recover_now(OV6)、7.19 cancel comm 校验(OV7)、§9 文档同步(OV10)。

**跨模型张力(cross-model tension)**:
- D4 改名确认形态:设计评审要求双路径知情,外部声音认为单人本机工具用现成
  armConfirm 单确认即可、双路径条是过度设计。现 spec 保留双路径但明确「复用
  `.suggest` 样式族、不新建组件」;若采纳简化案,「仅此会议」能力随之消失。
  **待用户拍板。**
- WP1 验证强度:外部声音指出最大重构只有字符串钉子 + 手工回归,0.5~1 天估时
  偏乐观(同族意见见测试 GAP1)。缓解:3b loadSeq + applyRoute 抽纯函数断言
  (下方未决项 3/5),估时上调至 1~1.5 天。

**What already exists(复用清单,未重建)**:armConfirm/toast(7.5/7.12)、
`.suggest` 样式族(WP5 确认条)、`ensureSpeakers` 缓存(lib-cnt)、
`PATCH /api/speakers` 与 `PATCH /api/jobs`(WP5 两路)、`live_recover()`
(WP6 recover_now)、`test_ws_live` 夹具(WP3 回归)、EventTap 重放语义
(WP3 接管不重做)。

**NOT in scope(评审后维持)**:§10 全部十项不变;新增两条被评审考虑过并明确
推迟——①说话人库会议数真实统计(需跨任务扫描 speaker_map);②wp1 主区渲染
的 DOM 级自动化基建(index 页面自检)。

**故障模式与覆盖**(新路径各一条生产失败场景):
| 路径 | 失败场景 | 测试 | 处理 | 用户可见 |
|------|---------|------|------|---------|
| applyRoute | 坏 hash(中文/超长) | 静态钉子+纯函数断言(建议) | 正则不匹配落空态 | 空态,非错误 |
| load 并发 | 快速连点两任务 | 无(待确认 3b) | loadSeq(若采纳) | 低危:旧文稿闪现 |
| resume 接管 | 旧 pump 迟迟不退 | ws 集成测试(计划内) | gone 置位有界退出 | 旧页终态提示 |
| recover_now | 目录被并发 finalize | 复用 live_recover 幂等 | 活跃会话目录排除 | toast 结果 |
| diar raise | sherpa 内部吞异常 | mock raise 测试(计划内) | worker except 收敛 | 「已放弃」 |

**并行实施(worktree lanes)**:
```
Lane C(先,API 契约): app.py/live_session.py —— /api/live/state、接管、
                        recover_now、7.2、7.19
Lane A(依赖 C 的 state): index.html —— WP1 → WP2 前端 → WP5 chips → WP7 index 项
Lane B(独立): live.html/live.js —— WP4 → WP6 → WP3 前端 → WP5 色板
冲突旗标:A 与 C 经 /api/live/state 契约耦合(C 先合);B 与 C 在 WP3 相接
(事件字段),合并顺序 C→(A∥B)。同文件多包:A/B 各自内部严格串行。
```

**实施任务**(JSONL 已落 `~/.gstack/projects/translagent/tasks-eng-review-*.jsonl`,
10 条 T1-T10):T1 WP1 路由状态机 / T2 7.2 raise / T3 WP6 分态 / T4 WP4 翻译
默认 / T5 WP2 动作行 / T6 WP3 守卫 / T7 WP5 身份一致 / T8 其余快修 / T9
测试补强(applyRoute 纯函数+钉子+pytest)/ T10 手工回归清单。

**完成度总结**:Step 0 维持原 scope;架构 2 项、代码质量 2 项、测试 4 GAP、
性能 0 项;外部声音 10 条(9 条核实采纳,1 条部分采纳);1 条 P0 级事实纠错
(7.2)与 1 条 P0 级设计回归(WP3 拒绝语义)在进入实施前被拦下。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | 未运行(可选) |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | fallback | codex CLI 运行时故障(workspace routing),按协议回退同族子代理完成外部声音 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_open → 修订合入 | 架构 2 + 质量 2 + 测试 4 GAP + 性能 0;外部声音 10 条;2 项 P0 级问题(7.2 修法证伪、WP3 拒绝语义回归)已按证据修订 |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | 未运行(设计评审已于 spec 成文前由四子 agent 完成) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | 未运行 |

- **CODEX**: codex exec 不可用(workspace routing discovery failed,重连 5 次耗尽);外部声音由 Claude 子代理(同模型族、新鲜上下文)完成,10 条发现见 §12,9 条核实采纳。
- **CROSS-MODEL**: 路由单写路径(我方架构-1 = 外部声音 OV2,一致);WP3 守卫(外部声音反对原拒绝方案并给出重连路径反例,核实成立,已改接管);D4 改名确认形态(两轮意见相左,留给用户)。
- **VERDICT:** ENG REVIEW 修订已全部合入本 spec——除下方未决项外可开工;建议实施顺序 C→(A∥B)(§12 lanes)。

**UNRESOLVED DECISIONS:**
- **D4′ 改名确认形态**:双路径行内条(现 spec,保留「仅此会议」能力)vs 外部声音简化案(现成 armConfirm 单确认,零新组件)。推荐:维持双路径(能力完整、复用样式、增量小)。
- **WP1-3b loadSeq 竞态守卫**:推荐纳入(两行改动,消除连点覆盖)。
- **T9 applyRoute 抽纯函数 + electron node 断言向量**:推荐纳入(项目对 splitSentences 有同款先例;主台零行为测试是最大重构的裸奔风险)。
- **WP6 recover_now 新端点**:超出原 spec 范围,推荐纳入(失败寻回不应要求重启整个服务)。
- **TODOS.md 建库**:仓库现无 TODOS.md;三个候选 TODO(系统通知旅程闭环、设置域统一、说话人库会议数真实统计)是否落册待定。
- **估时**:WP1 上调至 1~1.5 天(验证强度补齐后),总盘 3~4 天 → 3.5~4.5 天。
