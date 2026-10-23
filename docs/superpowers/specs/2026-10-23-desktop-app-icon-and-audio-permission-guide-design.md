# 会议录制工作台:客户端 .app 与系统声音授权引导 —— 设计文档

日期:2026-10-23
状态:待用户批准
前置:transcriber 工作台、Electron 45 壳(`transcriber/desktop/`)、录音功能已交付(2026-10-22)。

## 意图

用户要一个双击即开的正式客户端(有自己的图标与身份),以及一条首次录制系统声音时的授权引导路径。授权的难点已实测确认:当前 Electron 身份的屏幕录音权限是 `denied`,系统原生弹窗不再出现,用户若无引导会被静默失败困死。

## 目标

1. 「会议录制工作台.app」:双击打开,自动拉起转写服务,有自己的 Dock 图标与授权身份。
2. 授权引导(混合式):
   - `not-determined`(全新)→ 直接尝试捕获,让系统原生弹窗出现,不打扰。
   - `denied` 或捕获失败 → 弹出应用引导窗,用户把窗内的应用图标**直接拖进**系统设置的授权列表,开启开关,一键重启客户端。
3. 麦克风权限维持原生弹窗路径;若被拒,只显示一行错误文案加「打开设置」链接,不做完整引导。

## 非目标

- 不做安装器、不做 DMG、不上公证(notarize)。自用工具,ad-hoc 签名即可。
- 不改服务端与转录管线。
- 不做「权限被拒后的自动重试循环」;引导完成后由用户点重启。

## 已验证的事实(实测,2026-10-23)

- 音频系统已从昨日故障恢复(afplay 正常)。
- `systemPreferences.getMediaAccessStatus('screen')` 在 Electron 45 可用,本机 Electron 身份当前返回 `denied`,麦克风返回 `not-determined`。
- `sips` + `iconutil` 可用,可从像素图自制 .icns,零依赖。
- Electron 提供 `webContents.startDrag({file, icon})`(主进程),可发起携带文件路径的原生拖拽——引导窗内拖图标的机制。

## 特性 1:会议录制工作台.app

**做法(已定,单一路径)。** 构建脚本(放 `transcriber/desktop/build-app.sh`,源码仍是唯一事实)生成:

1. 复制 `.tools/electron/Electron.app` → `财务知识/会议录制工作台.app`。
2. 应用代码(`desktop/main.js`、`preload.js`、`package.json`)复制进 `Contents/Resources/app/`。
3. 改写 `Contents/Info.plist`:
   - `CFBundleName`/`CFBundleDisplayName` = 会议录制工作台
   - `CFBundleIdentifier` = `com.rendy.meeting-recorder`(独立身份,不与 Electron 混)
   - `CFBundleIconFile` = AppIcon(自绘 .icns)
   - 补 `NSMicrophoneUsageDescription`(「录制现场音频用于本地转录」)等 usage 文案,让系统弹窗说出人话。
4. `codesign --force --deep -s -` ad-hoc 重签名(改过 plist 必须重签)。
5. 构建脚本幂等:重跑覆盖同名 .app。

**图标(浅色底,已定)。** macOS Big Sur 风格圆角矩形(半径约 22.5%),浅灰底(#f4f5f6),近黑(#1e2126)波形条(与工作台品牌同款五条波形),右下角一枚红色(#b3261e)录音圆点作为唯一彩色——录音是这件产品的身份动作。生成 1024px 原图后用 `sips` 出全套尺寸,`iconutil` 合成 .icns。**实现前先出 2-3 张小样给用户挑。**

**自启动服务。** main.js 启动时若 8765 未监听,以 detached 子进程拉起 `python3 <transcriber>/app.py`,复用现有 30 秒轮询等待。路径按 .app 所在目录推导(顶层财务知识目录),不用硬编码绝对路径。

**位置与移动。** .app 生成在 `/Users/rendy/财务知识/会议录制工作台.app`(与资料目录同级,易找)。ad-hoc 签名下移动位置可用,改内容需重跑构建脚本。

## 特性 2:系统声音授权引导(混合式)

**触发点。** 网页端点「开始录音」且录音源含系统声音时,renderer 经 IPC 问主进程权限状态:

| 状态 | 行为 |
| --- | --- |
| `granted` | 直接录制 |
| `not-determined` | 直接录制(首次调用自动弹系统原生弹窗);失败再进引导 |
| `denied` / `restricted` / 录制失败 | 打开引导窗 |

**引导窗(新组件)。** 一个约 360×420 的置顶小窗(`alwaysOnTop`,可拖动),与系统设置并排可用:

- 标题:「允许录制系统声音」
- 三步图文(步骤圆点+短句,浅色底同工作台风格):
  1. 「已为你打开系统设置」(自动执行 `open "x-apple.systempreferences:..."` 到「屏幕与系统录音录制」面板;面板 URL 以 macOS 26 实测为准,备选 `com.apple.preference.security?Privacy_ScreenCapture`)
  2. 「把这个图标拖进列表」——窗内一枚大号应用图标(96px,即 .icns 渲染),按下即发起 `webContents.startDrag({file: <.app 路径>, icon: <128px nativeImage>})`;拖进设置面板的列表即完成添加
  3. 「打开开关,然后重启客户端」——一个「重启客户端」按钮(`app.relaunch()` + `app.exit(0)`),按钮下 12px 灰字解释「权限在启动时读取,重启后生效」
- 备选路径:步骤 2 旁边一枚小字链接「拖拽不生效?打开所在文件夹手动添加」(`open -R <.app>`),兜底 macOS 面板变体不接受 drop 的情况。
- 关闭引导窗不中断主窗口;再次点录制会重新唤起。

**IPC 面(preload 扩展,contextBridge 暴露到 window.desk)。**
- `getScreenPermission() → 'granted'|'not-determined'|'denied'|'restricted'`
- `openPermissionGuide()`(主进程:开设置面板 + 开引导窗)
- `startDragApp()`(引导窗内图标按下时调用)
- `relaunchApp()`
- `openAppFolder()`(兜底链接)

**文案原则。** 全中文短句;不解释 TCC、SCK 等术语;只说用户要做的事。

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 拖图标进设置列表不被接受(面板变体只认 Finder 拖拽) | 备选链接打开文件夹手动添加;实现期以真机拖一次验证 |
| 设置面板 URL 在 macOS 26 变化 | 构建时实测两种 URL,都不行则退化为打开「隐私与安全性」主页 |
| .app 换身份后旧的 Electron denied 记录残留 | 无影响:新 bundle id 是新身份,首次为 not-determined,走原生弹窗 |
| 拖拽过程中引导窗被设置窗口盖住 | 引导窗 alwaysOnTop |
| 重启后服务未起 | main.js 原有 30 秒轮询 + 自动拉起服务 |

## 验收标准

1. 双击 .app:窗口出现、Dock 显示正确图标、转写服务自动就绪。
2. 全新状态下选「系统声音」点录制:系统原生弹窗出现(自动化可测:自检脚本断言状态迁移)。
3. denied 状态下点录制:设置面板与引导窗同时出现;**拖图标进列表并开启开关**(此项需人工在真机执行一次,拖拽无法自动化);点「重启客户端」后自检 `--self-test` 的 systemAudio.bytes > 0。
4. 麦克风首次使用弹原生询问;拒绝后错误文案含「打开设置」链接。
5. 引导窗与主窗在中途可随意关闭,无死锁状态。

## 留给实现的选择

- 图标波形条的具体高矮节奏(以小样定)。
- 引导窗步骤插图画不画(可用纯排版 + 大图标替代,减少工作量)。
- 自检脚本扩展:增加 `--self-test` 里对 startDrag IPC 的存在性断言(不做真实拖拽)。
