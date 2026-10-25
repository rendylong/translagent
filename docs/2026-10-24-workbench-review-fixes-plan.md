# 工作台评审修复 · 执行计划(2026-10-24)

> 权威文件:`docs/2026-10-24-workbench-review-fixes-spec.md`(含 eng-review 修订与
> GSTACK REVIEW REPORT)。本计划把 spec 的 WP1-WP7 + §6 测试拆成 10 个可独立实施
> 与评审的 Task;冲突一律以 spec 为准。实施方式:subagent-driven-development。
>
> 工作分支:`workbench-review-fixes`(worktree `.worktrees/workbench-review-fixes`),
> 从 main@<commit after spec> 切出;桌面端 desktop/ 零改动。

## Global Constraints(全程绑定)

1. 不重启/不杀 8765 端口上正在运行的服务;需要服务实例的验证一律用随机端口自起自停。
2. worktree 内没有 `transcriber/models/` 大模型二进制(未入库):涉及真模型的测试
   默认 skip;确需真模型探针时用主树绝对路径
   `/Users/apple/translagent/transcriber/models/`,且不落进常规测试路径。
3. JS 语法检查(无 node):`ELECTRON_RUN_AS_NODE=1 /Users/apple/translagent/.tools/electron/Electron.app/Contents/MacOS/Electron --check <file>`。
4. 中文注释,风格随文件;commit message 用项目一行式中文(原因+后果)。
5. 不变量(spec §5):账本三级灰度/no-anim/autoResume 语义、声纹阈值与
   speakers.json 结构、`/api/jobs` 形状、`/#<job_id>` 深链格式、单 worker 串行。
6. 音频不出本机;本计划不引入任何新外发。
7. 每个代码 Task 合入前:`python3 -m pytest transcriber/tests -q` 不新增失败
   (基线失败清单见 ledger),JS 改动过 --check,静态钉子按 spec §6 落进
   `transcriber/tests/test_ui_static.py`(随 Task 3 建立,后续 Task 追加钉子)。

## Task 1: live 后端契约(app.py + live_session.py + tests)

**文件**:`transcriber/app.py`、`transcriber/live_session.py`、`transcriber/tests/`。

1. `GET /api/live/state`:经 live_session 新导出的查询函数(持 `_SESSIONS_LOCK`)
   返回 `{'active': bool, 'status': str|null, 'title': str|null, 'started_at': int|null}`;
   `active` = 存在 status ∈ `('listening','stopping')` 的会话;多个会话取
   `started_at` 最大者;无会话返回 `{'active': false, 'status': null, 'title': null,
   'started_at': null}`。
2. **resume 接管语义**(取代拒绝):LiveSession 增加每会话连接注册
   (conn_id → {gone, ws}),ws_live 为每条连接生成 `conn_id = uuid4().hex`;
   `start` 成功与 `resume` 成功时注册并挂 pump;`resume` 到达时若已有**其他**
   conn 附着:先向旧连接 `ws.send(json.dumps({'type':'session_taken_over'},…))`
   再置其 gone 并关闭,然后新连接 backlog 重放 + 挂 pump。旧 pump 经 gone 有界
   退出(既有 EventTap 归还语义不变)。注意:注册表读写均持 `_SESSIONS_LOCK`;
   不得在持锁期间做网络 send(先在锁内取旧 ws 引用与 gone,锁外发送)。
3. `stopped` 事件带 `reason`(`live_session._finish`):空会话分支
   `{'type':'stopped','reason':'empty'}`;转正失败分支
   `{'type':'stopped','reason':'finalize_failed','error': '<简要>'}`;
   成功分支不变(带 `job_id`,无 reason)。
4. `POST /api/live/recover_now`:复用 `live_recover()` 的扫描与 finalize 逻辑,
   **排除 `_SESSIONS` 中活跃会话的目录**(运行期调用不得把正在录制的会话转正),
   返回 `{'recovered': ['<job_id>', …]}`;幂等(已终态目录跳过,沿用现有逻辑)。
5. 测试:新增 `test_live_state_api.py`(state 形状含无会话/单会话/stopping);
   `test_ws_live.py` 扩展(第二连接 resume → 旧连接收 `session_taken_over` 且
   不再收事件、新连接收 backlog;同连接重复 resume 幂等);
   `test_live_session.py` 扩展(stopped.reason 三分支,沿用现有 finalizer mock)。
   先跑基线并记录(见 Global Constraints 7)。

## Task 2: 管线快修(app.py + tests)

1. **7.2 diarizing 可放弃(回调内抛异常)**:`on_diar_progress`
   (app.py:525-530)改为:
   ```python
   def on_diar_progress(done, total):
       write_meta(job_id, progress=86 + int(done * 12 / max(total, 1)))
       if read_meta(job_id).get('cancel_requested'):
           raise RuntimeError('已放弃')
       return True
   ```
   (已实测:sherpa-onnx 2.0.0 忽略返回值;raise 可首次回调即中止且实例无损。
   保留「TypeError 回落路径放弃仍在阶段边界生效」注释。)
2. **7.19 cancel 防误杀**:`job_cancel`(app.py:755-757)kill 前加与
   `recover_interrupted`(982-984)同款校验:`ps -p <pid> -o comm=` 含 'whisper'
   才 kill。
3. **7.15 文案**:app.py:985 `'服务重启导致中断,请删除后重新上传'` 改
   `'服务重启导致中断,可用原音频重跑'`。
4. 测试:worker 层 mock `run_diarization`(首次回调即 raise `RuntimeError('已放弃')`)
   → 断言任务终态 error 含「已放弃」且 cancel_requested 收敛;cancel 对非 whisper
   进程 pid 不发 kill(断言 subprocess.run 调用);文案断言。真模型探针测试默认
   `@unittest.skip`(worktree 无 models)。

## Task 3: WP1 路由与视图状态机(index.html)

按 spec WP1-1..7 + 3b 实施。要点:单一 `view = {type:'empty'|'job'|'lib', id?}`;
`applyRoute()` 纯函数(输入 location.hash 解析,不碰 DOM,便于测试)+ `render(view)`
收口;`hashchange` 与 `popstate` 都进 applyRoute,幂等短路(目标已渲染则不动);
程序化导航一律 `location.hash = …` 赋值(setCurrent 改造,**不引入 pushState**);
`loadSeq` 代际守卫;`renderEmpty` 合一(upload/record 两变体,补齐波形 SVG 与
「实测参考」行,与 index.html:443-462 一致);`libbtn` 激活态(`.on`)+ 清三 Tab
高亮 + `lib-cnt` 填 `ensureSpeakers()` 的 `suspects.length`(0 隐藏);
`#setrow-lock` 死守卫清理。**applyRoute 抽纯函数并加断言向量**:`#/`→empty、
`#<合法id>`→job、`#speakers`→lib、`#中文`/`#超长`/`#未知格式`→empty;提取方式参照
项目 splitSentences 先例(ELECTRON_RUN_AS_NODE 跑断言)。静态钉子:建立
`transcriber/tests/test_ui_static.py`(hashchange 监听存在、无 pushState 字样、
renderEmpty 含 meas、`.side-foot.on` 被 JS 使用等)。

## Task 4: WP2 前端动作行 + 文档同步(index.html + AGENTS.md + 界面重设计-spec.md)

1. 删除 `tab-live`(347-357)与 `pane-live`(424-431),`TAB_LIST` 收敛为
   `[['up','upload'],['rec','record']]`;删除 `live-go`(553-563)。
2. 「新任务」卡下方新增 `#liveRow` 动作行:状态点(空闲灰/进行中红 + 呼吸动画)
   + 文案(空闲「实时字幕 · 开悬浮窗或新标签页」/进行中「字幕进行中 · 已开 HH:MM」),
   整行可点:桌面 `window.desk.openLiveCaption()`;web
   `window.open('/live?mode=panel', 'livePanel')`(**命名 target**)。
3. 独立常驻慢定时器(页面可见时 10~15s,`visibilitychange` 时立即刷一次)+
   任务轮询 tick 顺带刷新;数据源 `GET /api/live/state`(Task 1 契约)。
4. 文档同步:AGENTS.md「界面」节与 `transcriber/界面重设计-spec.md` 补记
   (三 Tab → 两 Tab + 实时字幕动作行)。空态 upload/record 变体随 Task 3 的
   renderEmpty,不再有 live 变体。

## Task 5: WP4 翻译默认与隐私文案(live.html + live.js)

1. `fTgt` 默认 `value=""` 选中(live.html:290)。
2. note 动态化(替换 293 静态行):tgt==src →「识别与说话人分离即时可用,全程在
   本机完成;修正与翻译事件到达时自动上屏。」;tgt≠src →「识别与说话人分离在本机
   完成;修正与翻译会把窗口文本(不含音频、路径、人名)发送至外部 LLM 服务。」;
   `fTgt.onchange` 即时切换;既有 `llm===false` stopChip 提示(live.js:165-168)保留。
3. **旧默认迁移**:`live.lang === 'zh>en'`(恰为旧默认)时 `localStorage.removeItem`
   让新默认生效(langFromStorage 与 tryAutoResume 两处读取路径都要覆盖)。
4. `live.html:262` sub 改「音频不出本机;开启翻译时,识别文本交由外部 LLM 修正与
   翻译」;`:350` hint 不变。
5. **7.8 f1 升白**:`started.llm===false` 或 tgt==src 的会话,根节点加 `llm-off` 类;
   CSS 增 `.llm-off` 下账本与面板两侧 f1 句 `color: var(--lc-txt)`(译文层
   `--lc-tr` 不变)。

## Task 6: WP3 前端守卫 + 重连终态(live.js + live.html)

1. **单实例锁**:`start()` 与 `tryAutoResume()` 先
   `navigator.locks.request('live-session', {ifAvailable:true}, …)` 占锁并持有至
   会话结束/出错;锁不可得 → 回表单态显示「字幕会话已在其他窗口进行」且**不触发
   getUserMedia**;`'locks' in navigator` 为 false 时降级无锁(服务端守卫兜底)。
2. **`session_taken_over` 终态**:收到即停止自动重连(同 userStop 路径)、
   `stopCapture()`、清 session 存储、回表单态显示「会话已在其他窗口打开」。
3. **7.3 beforeunload**:`phase ∈ {'connecting','live','stopping'}` 时注册拦截
   (ended/idle 不拦;`window.desk` 桌面悬浮窗形态跳过——主进程已有关窗拦截)。
4. **7.14 重连终态**:`reconnectTries` 计数,>10 次停止自动重连;connChip 文案改
   「无法连接转写服务,请确认服务已启动 · 点击重试」,点击 chip 手动 `connect()`
   并清零计数。

## Task 7: WP6 结束卡三分态(live_session 已备,前端 live.js + live.html)

1. `onStopped` 按 `reason`/`job_id` 三分支:成功(job_id)→ 标题「文稿已生成」,
   **主按钮 = endLink 换 `.ego` 主样式**,「开始新会话」降 `.ego.ghost`;web 端
   endLink 改 `window.open('/#' + ev.job_id, '_blank')`(桌面 openMainJob 路径不变);
   `finalize_failed` → 「文稿生成失败,录音已保留」+ error 摘要 + 「录音仍在服务端,
   重启转写服务后会自动转为任务(历史记录带「实时」标记)」+「立即重试转正」按钮;
   `empty` → 「本場没有录到语音」+「会话已结束,未生成文稿」。
2. 「立即重试转正」调 `POST /api/live/recover_now`:成功且有新 job → 原位转成功态
   (深链可用);无/失败 → 就地提示,保持失败态。
3. 三分支均不再出现「正在生成完整文稿」字样;三分态结构进
   `live_page_selftest.py` DOM 钉子。

## Task 8: WP5 色板与说话人身份一致(live.html + live.js + index.html)

1. `live.html :root` 增 `--lsp0..--lsp5`(与 `--sp*-ink` 六色相一一对应的亮化变体,
   注释逐条写映射源,如「--lsp0 ↔ index --sp0-ink #1d5290 亮化」);live.js
   `SPEAKER_COLORS` 改用六值,`colorFor`(485-486)改 `speaker % 6` 循环;行注释
   「与 index.html --sp*-ink 同源,改动须两处同步」。
2. 正文标签统一 `displayName()`:`draw()`(index.html:1099)改
   `displayName(s.speaker) || ('说话人' + (s.speaker + 1))`,搜索过滤 `nm` 同源。
3. chip 改名双路径(替换 1004-1007):gid 且新名 ≠ 当前显示名 → `.spkrow` 下出
   行内确认条(复用 `.suggest` 样式族,不新建组件,**不显示数字**):
   「「{名}」应用到哪些会议?」`[应用到所有会议]` / `[仅此会议]`。
   所有会议 → `PATCH /api/speakers/<gid>` + toast「已全局改名,相关会议文稿已更新」
   + `ensureSpeakers(true)`;仅此会议 → `PATCH /api/jobs/<id>`(speaker_names)+
   `names[sp]=v`。取消/空白不动。
4. 验收注意:全局改名后本篇正文更新以**无本地覆盖名**为前提(有本地覆盖名仍显示
   本地名,是既有优先级,非缺陷)。

## Task 9: WP7 index 侧快修批(index.html)

7.1 `resetDrop()` 增 `$('#title').value=''`;7.5 历史项(809)与文稿页 `#del` 的
confirmText 统一「删除后音频与文稿不可恢复,确认?」;7.6 搜索双侧 `toLowerCase()`
(`hl()` 高亮同一折叠);7.12 toast 入场 `fade+translateY(-6px→0)` 200ms、退场 CSS
280ms 对齐 JS 300ms、err 类 ttl 7s;7.13 refresh 前算任务列表签名
(status/stage/progress/queue_pos/current),无变化直接返回,仅 running 字段变化 →
按 `data-id` 就地更新该行 `.sub` 与进度条宽,结构变化才全量重建;7.16 `:root` 增
`--t-fast:120ms; --t-med:180ms; --t-ease:cubic-bezier(.2,0,0,1)`,`#setpop` 与导出
menu 入场 `opacity+translateY(4px)` 180ms,主区容器一次性 `.view-in`(fade 180ms),
进度条 transition `.8s`→`600ms linear`(132/179 两处);7.17 index 残留 `</b>,`
清(逗号移入 `<b>`);7.18 `audio{accent-color:var(--primary)}` + `preload="metadata"`。
7.13 是对既有 refresh 的行为改写:验收必须覆盖「确认?武装态不被打断、滚动位置
保持、终态到达正常重排」(手工项,列入手工清单)。

## Task 10: WP7 live 侧快修批(live.html + live.js + index.html reduce 块)

7.9 caret 仅 partial 非空时给行加 `.typing`;`.caret` 默认 `opacity:0`,
`.typing .caret` 平滑呼吸(`@keyframes caretBreath{50%{opacity:.15}}`,ease);
reduced-motion 下 typing 时恒显。7.10 两文件各加通用兜底
`@media (prefers-reduced-motion: reduce){*,*::before,*::after{animation-duration:.01ms !important; animation-iteration-count:1 !important; transition-duration:.01ms !important}}`
(index.html 新增;live.html 保留现有细粒度块并确认与兜底不冲突)。7.11 删除
`.bar .actions` 的 hover 门控(live.html:116-120),按钮常显(pointer-events:auto)。
7.17 live.html 侧 `</b>,`/`</b>。` 全清。静态钉子随 Task 3 建立的
test_ui_static.py 追加。

## 验收总闸

- spec §6 测试计划全绿;§12 手工回归清单(深链/双开/确认态/滚动/返回键)留待
  人工,最终评审通过后向用户交付清单。
- 最终整分支评审(superpowers:requesting-code-review 的 code-reviewer 模板)。
