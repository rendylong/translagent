# 会议录制工作台 .app 与授权引导 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 产出双击即用的「会议录制工作台.app」(自绘浅色图标、自动拉起服务)与混合式系统声音授权引导(浮层内图标可拖入系统设置列表)。

**Architecture:** 源码留在 `transcriber/desktop/`,幂等构建脚本把它注入 Electron 副本的 Resources/app 并 ad-hoc 重签名;引导是主进程新开的置顶小窗(本地 HTML,不依赖服务器),拖拽经 `webContents.startDrag` 携带真实 .app 路径;网页端仅新增对 `window.desk` 新 IPC 的分支调用。

**Tech Stack:** 纯 Python 3.9(zlib 手写 PNG,无 PIL)、sips、iconutil、PlistBuddy/plistlib、codesign、Electron 45(零 npm)。

**Spec:** `docs/superpowers/specs/2026-10-23-desktop-app-icon-and-audio-permission-guide-design.md`

## Global Constraints

- 无 brew、无 node、无 npm、无 ffmpeg;一切用系统工具。
- 本目录不是 git 仓库:计划中的「Commit」一律替换为「该单元验证通过并在本文档勾选」。
- 用户可见文案全部中文短句,不出现 TCC/SCK 等术语。
- 颜色沿用工作台 tokens:浅底 #f4f5f6、近黑 #1e2126、负红 #b3261e。
- ad-hoc 签名链:`codesign --force --deep -s - <app>`;构建脚本可重复执行(幂等)。
- .app 目标路径:`/Users/rendy/财务知识/会议录制工作台.app`;bundle id `com.rendy.meeting-recorder`。

## Review Focus(实现者必测的五个隐含输入)

1. 系统设置面板 URL 在 macOS 26 不接受 → 任务 6 内实测两种 URL,均失败则退化为打开「隐私与安全性」主页并在文案中去掉「已直达」。
2. 拖图标不被列表接受 → 引导窗必须有可见的兜底链接(IPC `openAppFolder`),任务 6 断言其存在与渲染。
3. 新 .app 身份理应为 not-determined;若异常返回 denied,引导流程照样成立 → 任务 8 自检报告记录原始状态,不做假设。
4. 引导窗被系统设置盖住 → `alwaysOnTop` 必须在代码里,任务 6 以 `BrowserWindow` 配置断言。
5. 重启客户端后服务未起 → 任务 4 的「杀服务→启动 .app→/api/jobs 200」覆盖。

---

### Task 1: 图标生成器与三张小样

**Files:**
- Create: `transcriber/desktop/make_icon.py`(纯 Python 像素绘制,输出 PNG)
- Create: `~/.zcode/v2/visualizations/<session>/icon-samples.html`(小样展示页)

**Interfaces:**
- Produces: `make_icon.py <out.png> <size> [variant]`,variant ∈ `A|B|C`;A=灰底黑波形红点,B=白底黑波形红点,C=灰底波形中一条红。

- [ ] **Step 1: 写 PNG 编码器与绘制断言脚本** `make_icon.py`:圆角矩形(半径 22.5%)超采样 4x 抗锯齿,五条波形条(与工作台品牌同节奏),右下 96px 级红点(variant A/B)。
- [ ] **Step 2: 跑 `python3 make_icon.py /tmp/a.png 1024 A` 等三张**,断言 PNG 头合法、尺寸 1024、四角透明(自写解析断言,不靠眼睛)。
- [ ] **Step 3: 生成小样展示页**(三图标原尺寸 + 32px 缩略 + 引导窗线框图),落 visualizations 目录,回复中给出链接。
- [ ] **Step 4: 用户未及挑选时以 A 为默认继续**,最终报告注明换 B/C 只需重跑构建脚本。

### Task 2: .icns 合成

**Files:**
- Modify: `transcriber/desktop/make_icon.py`(加 `--icns <dir>` 模式)

**Interfaces:**
- Produces: `AppIcon.icns`(标准 iconset 全尺寸:16/32/128/256/512 及 @2x)。

- [ ] **Step 1:** 生成 1024 主图 → `sips` 缩出 10 个尺寸 → `iconutil -c icns`。
- [ ] **Step 2:** 断言 `AppIcon.icns` 存在、字节数 > 100KB、`file` 识别为 icns。

### Task 3: 构建脚本 build-app.sh

**Files:**
- Create: `transcriber/desktop/build-app.sh`

**Interfaces:**
- Consumes: Task 2 的 `AppIcon.icns`、`desktop/{main.js,preload.js,package.json,guide.html}`。
- Produces: `/Users/rendy/财务知识/会议录制工作台.app`(含 Resources/app 与 Resources/AppIcon.icns,Info.plist 已改写)。

- [ ] **Step 1:** 脚本流程:rm 旧 .app → 复制 `.tools/electron/Electron.app` → 复制 desktop 源码进 Resources/app → 嵌 icns → python plistlib 改 Info.plist(CFBundleName/DisplayName=会议录制工作台、CFBundleIdentifier=com.rendy.meeting-recorder、CFBundleIconFile=AppIcon、NSMicrophoneUsageDescription=录制现场音频用于本地转录)→ `codesign --force --deep -s -`。
- [ ] **Step 2:** 验证:`plutil -lint` 通过;`codesign --verify --deep` 0 退出;`open` 启动后 `pgrep -f 会议录制工作台` 可见进程且窗口标题正确(人工看一眼 Dock 图标)。
- [ ] **Step 3:** 幂等验证:连跑两次构建脚本,第二次产物仍通过 Step 2。

### Task 4: main.js 服务自启动

**Files:**
- Modify: `transcriber/desktop/main.js`

**Interfaces:**
- Produces: `ensureServer()`(探测 8765 失败则 detached spawn python3,候选路径=[appRoot/../transcriber/app.py, appRoot/../../transcriber/app.py, 硬编码绝对路径],首个存在者),在 onReady 最前调用。

- [ ] **Step 1:** 实现 `ensureServer()` 并接入 boot 流程(轮询逻辑复用现有 waitServer)。
- [ ] **Step 2:** 验证:`kill` 现有服务 → `open .app` → 30 秒内 `curl 127.0.0.1:8765/api/jobs` 200。

### Task 5: 权限 IPC 与 preload 扩展

**Files:**
- Modify: `transcriber/desktop/main.js`、`transcriber/desktop/preload.js`

**Interfaces:**
- Produces(window.desk 新成员,全部经 ipcRenderer.invoke):
  `getScreenPermission():Promise<'granted'|'not-determined'|'denied'|'restricted'>`
  `openPermissionGuide():Promise<void>`(开设置面板 + 开引导窗)
  `startDragApp():Promise<void>`(webContents.startDrag,{file:<appRoot>, icon:128px nativeImage})
  `relaunchApp():Promise<void>`(app.relaunch + app.exit(0))
  `openAppFolder():Promise<void>`(open -R <appRoot>)
  appRoot = `path.dirname×3(process.execPath)`。

- [ ] **Step 1:** 主进程 ipcMain.handle 五个通道 + preload 暴露。
- [ ] **Step 2:** self-test 脚本内加断言:五个 API 存在且 `getScreenPermission()` 返回四值之一(页面内 evaluate)。

### Task 6: 引导窗 guide.html 与设置面板直达

**Files:**
- Create: `transcriber/desktop/guide.html`(浅色、工作台 tokens 风格,含大图标 96px、三步文案、重启按钮、兜底小链接)
- Modify: `transcriber/desktop/main.js`(创建置顶小窗 360×440,`alwaysOnTop`,loadFile guide.html)

**Interfaces:**
- Consumes: Task 5 的 `startDragApp/relaunchApp/openAppFolder`(guide 内经 window.desk 调用,preload 同样注入引导窗)。
- Produces: 面板 URL 常量 `PANE_URL`(实测后定),退化常量 `FALLBACK_URL`。

- [ ] **Step 1:** 实测两种 URL:`x-apple.systempreferences:com.apple.ScreenCapture-Settings.extension` 与 `com.apple.preference.security?Privacy_ScreenCapture`,`open` 后人工确认落在「屏幕与系统录音录制」;都不行则用隐私主页并把文案第 1 步改为「打开 系统设置 → 隐私与安全性 → 屏幕与系统录音录制」。
- [ ] **Step 2:** guide.html 实现(文案:①已为你打开系统设置 ②把这个图标拖进列表 ③打开开关后点重启客户端;按钮下方 12px 灰字「权限在启动时读取,重启后生效」;兜底链接「拖拽不生效?打开所在文件夹手动添加」)。图标 mousedown 触发 `desk.startDragApp()`。
- [ ] **Step 3:** main.js `openGuideWindow()`(单例,重复调用聚焦)+ `openPermissionGuide` 串起开面板与开窗。代码审查断言 `alwaysOnTop: true` 存在。
- [ ] **Step 4:** 验证(dev 模式):页面 console 调 `desk.openPermissionGuide()` → 引导窗出现 + 设置面板打开(进程 `pgrep System Settings` + 人工确认);按住引导窗图标拖动到桌面 → 松手出现「复制」还是无效都不算失败,仅记录(macOS 对拖 .app 到桌面的行为是移动,验证时改拖到废纸篓取消)。真实拖入列表验证留给用户三步清单。

### Task 7: 网页端接线

**Files:**
- Modify: `transcriber/static/index.html`(录音 onclick 开头分支)

**Interfaces:**
- Consumes: `window.desk.getScreenPermission/openPermissionGuide`。

- [ ] **Step 1:** 开始录音时:录音源含 system 且 `window.desk` 存在 → `const st = await desk.getScreenPermission()`;`granted|not-determined` → 照常 start();`denied|restricted` → `desk.openPermissionGuide()` 并 return。start() 抛错且错误信息含「系统声音」时同样唤起引导。
- [ ] **Step 2:** 验证:Electron 内 evaluate 模拟:status=denied(mock desk API)→ 断言 openPermissionGuide 被调且未 start;status=granted → 走 start(以 recorder error path 断言未进引导)。浏览器(无 window.desk)行为与现状完全一致(现有自检回归)。

### Task 8: 全链路验证与交付

- [ ] **Step 1:** 重建 .app(含全部新代码)→ codesign/plutil 复验。
- [ ] **Step 2:** `--self-test` 真机跑:报告含 screenPermission 原始状态、五个 IPC 存在、systemAudio/mic 尝试结果。
- [ ] **Step 3:** 麦克风真录 3 秒(用户在场点允许)→ self-test mic.bytes > 0;系统声音待用户授权后复跑。
- [ ] **Step 4:** 报告附「用户人工三步」:拖图标入列表并开开关 → 点引导窗「重启客户端」 → 复跑自检命令看 systemAudio.bytes > 0。

### Task 9: 文档与记忆

- [ ] AGENTS.md 增补 .app 构建与引导旅程一段;项目记忆同步一句。
