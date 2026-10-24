# AGENTS.md

本目录是用户的财务学习与会议资料库。与用户交流一律用中文。

## 目录

| 路径 | 内容 |
| --- | --- |
| `会议/` | 会议录音(m4a)、会议纪要(`*_会议纪要.md`)、转录产物(`转录/`) |
| `transcriber/` | 本地 Web 转录工作台 + Electron 录音客户端,见下节 |
| `.tools/whisper.cpp/` | 已编译的 whisper.cpp,Metal GPU 加速,勿删 |
| `.tools/electron/` | Electron 45 预编译(GitHub release 直装,已去 quarantine),勿删 |

## 转录工作台(transcriber/)

启动:双击 `transcriber/启动转写服务.command`,或执行 `cd transcriber && python3 app.py`。地址 `http://127.0.0.1:8765`,仅监听本机。

管线:上传音频 → `afconvert` 转 16kHz 单声道 WAV → `whisper-cli`(large-v3-turbo)转录 → sherpa-onnx 说话人分离(pyannote 分割 + NeMo TitaNet-large 声纹 `speaker_embedding_titanet.onnx`;2026-10-23 实测替换原 3DSpeaker zh 模型——英文双男声场景原模型全归一人,新模型锚点 79% 且中文零退化,对比脚本与记录在 `transcriber/models/bench/`)→ 按时间重叠合并段落的说话人。

数据形状:每个任务一个目录 `transcriber/jobs/<id>/`,内含 `meta.json`(状态机,queued → converting → transcribing → diarizing → merging → done 或 error)、`source.<ext>`、`audio16k.wav`、`whisper.json`、`transcript.json`(`segments` 数组,每段 `start_ms`、`end_ms`、`speaker`、`text`)、`transcript.md`(完成任务的文稿落盘,改标题/说话人名后自动重生成;`POST /api/jobs/<id>/reveal` 用 `open -R` 在 Finder 中定位它)。改后端先读 `app.py` 顶部的数据形状注释。

**声纹跨会议匹配(2026-10-23)**:全局说话人库 `transcriber/speakers/speakers.json`(每人 gid、名字、192 维质心、每场会的簇签名锚点);任务完成后落盘 `dsegs.json` 与 `embeddings.json`,并写 `meta.json` 的 `speaker_map`(`{簇号: {gid, sim, status}}`,status ∈ auto≥0.65 / suspect 0.50~0.65 待确认 / new / confirmed)。名字解析优先级:任务级 `speaker_names` 覆盖 > 全局库名 > 「说话人N」。相关 API:`GET/PATCH/DELETE /api/speakers`、`POST /api/jobs/<id>/speaker_map`(confirm/separate)、`POST /api/jobs/<id>/cancel|retry`。匹配阈值 0.65/0.50 来自 bench 实测(同人跨会议 0.751,异人最高 0.557),宁新建勿误并。旧任务(2026-10-23 前)无 embeddings,只有重跑才能入声纹库。TitaNet extractor 的两个坑(已防御):超长输入段会崩,须切成 ≤12s;whisper 尾部时间戳可能漂移出音频时长,越界切片要跳过。

界面(2026-10-23 重构,spec 见 `transcriber/界面重设计-spec.md`):侧栏「上传文件 / 现场录制 / 实时字幕」三 Tab 平级入口(桌面默认录制,web 默认上传;2026-10-24 增实时字幕 Tab——桌面端经 desk.openLiveCaption 拉起/聚焦悬浮窗,web 端新标签页开 /live?mode=panel);转录设置(语言/说话人数/声纹开关)是全局二级设置,存 localStorage,唯一编辑入口在上传 Tab;录制开始即快照并锁定设置,停止与丢弃分离;toast + 行内二次确认,无 alert/confirm;历史按 处理中/今天/更早 分组,live 转正任务带「实时」徽标(app.job_public 的 is_live);文稿页标题可点击编辑、说话人 chips 带声纹置信徽标。桌面端录音中关窗会拦截确认(`main.js` 读 `body.recording`),Dock 角标经 `desk.diag('rec-state:1/0')` 广播。

**实时字幕悬浮窗可缩放与交互修复(2026-10-24)**:macOS 对 `transparent:true` 窗口禁用原生边缘缩放(Electron 官方限制),`live-window.js` 注入自绘右下手柄(#nativeResizeGrip,pointer 拖动经 preload 的 `desk.resizeLive(w,h)` → 主进程 setSize;move/up 同时挂手柄与 document 兜底,setPointerCapture 容错),尺寸按阶段记忆在 `live-window.json` 的 `sizes`,各阶段有最小尺寸钳制(PHASE_MIN)。悬浮窗内:字幕条铺满窗口(inset 6px,拖高即见更多历史)、表单卡/结束卡也是拖动区(`.card`/`.ecard` drag,交互控件 no-drag)、三个操作按钮常显于条内右上角(网页叠加态仍 hover 显示);字幕条新增可滚动历史区 `#barLog`(live.js 里 entry.bel 与面板 entry.el 同源维护,当前主行在 log 中隐藏,syncBarLog)。**停止后的完整转录记录**:live.js 结束卡「打开文稿」深链 `/#<job_id>`(工作台启动读 hash 定位任务);桌面端悬浮窗内点击走 `desk.openMainJob` 聚焦主窗跳转(主窗录音中只聚焦防打断,状态由 rec-state 广播的 mainRecording 维护)。**陈旧会话自愈(2026-10-24)**:打开 /live 时自动恢复撞上死会话(服务重启过,WS 回「会话不存在或已失效」)不再死路错误卡——清掉内存与 localStorage 的 session、断开 WS、回到开始表单并出「上次会话已因服务重启结束」提示(state.autoResume 标记区分「页面自动恢复」与「用户会话中断」两类;onclose 在 idle/ended 阶段不重连,否则 idle 重连会误发 start 开出无人供音的幽灵会话)。

**实时字幕语言(2026-10-24)**:识别语言 zh/en/ja 与翻译目标(不翻译/中/英/日)独立选择(live 表单两个下拉,序列化仍是 `'src>tgt'`,不翻译=tgt==src,LLM 对同语种指示 translation 输出空串);ja 的 pass1 用多语言流式模型 `models/streaming-zipformer-ja`(8 语种,encoder/joiner 仅 int8),按 `start.language` 的 src 在 ws_live 选目录;pass1 模型文件名按目录 glob 解析(优先非 int8→int8→回落硬编码);whisper(-l ja)与 LLM(_LANG_NAME 含 ja)天然支持。spec 见 `docs/2026-10-24-live-captions-ja-and-target-language-spec.md`。

**结束卡与翻译可用性提示(2026-10-24)**:结束卡新增「关闭」按钮(desktop 经 `desk.closeLiveCaption` → `hide-live` IPC 隐藏悬浮窗,web 回工作台);「打开文稿」跳主窗后悬浮窗自动隐藏。`started` 事件携带 `llm: bool`(LiveSession.llm_enabled,来自 env.rtf 是否有 key):前端在选了翻译目标但 `llm===false` 时出「未配置翻译密钥」提示,不再静默无翻译。`_parse_reply` 按 lang_pair 校验 translation:src==tgt(不翻译)时空串合法,跨语种空串仍拒——修复不翻译组合下修正文本被当坏回复丢弃。

**实时字幕管线两层化(2026-10-24)**:Pass1 流式整句(rule2=1.2/rule3=20 官方默认,断句碎片化根因修复见 spec)→ final 内联 TitaNet 分人(speaker 随 pass1_final 即时下发)→ LLM 合并批修正+翻译(相邻 final 间隔 ≤2.5s 并批、单说话人批剥行首标签、停止时在飞批排空后才 stopped)。

**Pass2 摘除(2026-10-24)**:whisper 窗口重转与 GPU 争用移除(实时管线 GPU 零占用,GPU_LOCK 只剩离线 job 互斥),字幕条两级化(识别 → 翻译,修正徽标已删),`live_pass2.py` 仅剩 `read_wav_slice`(分人切片用),终稿仍走离线全量管线零变化。spec 见 `transcriber/docs/2026-10-24-live-captions-drop-pass2-and-segmentation-spec.md`。

实测耗时(2026-10,86 分钟双说话人录音):whisper 5 分钟(GPU),说话人识别约 12 分钟(CPU,约 0.13 倍速)。单 worker 串行处理任务。

## 录音(web + 桌面客户端)

网页端(`static/index.html` + `static/recorder.js`)支持麦克风现场录音,采成 16kHz 单声道 PCM WAV 后直接 POST `/api/jobs`,后端零改动。录音状态机在 `recorder.js` 顶部注释。

**正式客户端**:`财务知识/会议录制工作台.app`(双击即用,Dock 有自绘图标,自动拉起转写服务)。源码在 `transcriber/desktop/`,改完跑 `desktop/build-app.sh` 重新生成(幂等)。图标由 `desktop/make_icon.py` 纯 numpy 绘制(浅底黑波形红点,变体 A/B/C)。

**系统声音授权引导**:点录制时预检 `getMediaAccessStatus('screen')`;denied/受限或捕获失败 → 置顶引导窗(guide.html):自动打开「屏幕与系统录音录制」面板,窗内图标 `webContents.startDrag({file: <.app>})` 发起原生拖拽(等价从 Finder 拖),拖入列表开开关,一键重启(`app.relaunch`,权限只在启动时读取)。麦克风走系统原生弹窗。

**两个硬坑(2026-10-23 实测)**:
1. **`getDisplayMedia` 的权限类型是 `display-capture`,不是 `media`**——`setPermissionRequestHandler` 只放行 `media` 会被秒拒(NotAllowedError "Permission denied",handler 根本不会被调用,极易误诊为 TCC 问题)。两个都要放行。另:`getDisplayMedia` 需要真实用户激活,executeJavaScript 的 gesture 标志不够,自测用 `webContents.sendInputEvent` 注入真实点击;bundle 内含 Resources/app 时内层二进制忽略路径参数直接跑打包应用。诊断配方:`--enable-logging --v=1` 后 grep `PERMISSION_DENIED`;运行期问题读 `~/Library/Application Support/meeting-recorder/main.log`(记录每次预检结果/handler 调用/引导唤起)。
2. **TCC 责任进程归属**:终端 shell 拉起的进程,TCC 屏幕录音查的是终端的授权;双击/`open`(launchd)拉起才查 app 自己。**验证用户路径必须用 `open .app --args --self-test`**,从 shell 直接跑会得到假 granted。ad-hoc 签名重建即换身份,授权失效;已改用稳定自签证书 `rendy-local-dev`(证书与私钥在 `~/.local-codesign/`,已导入登录钥匙串并信任,build-app.sh 自动使用,两次构建 designated requirement 一致)。Electron 39+ 系统声音另需 plist 的 `NSAudioCaptureUsageDescription`(基座自带)。

- 自检命令:`会议录制工作台.app/Contents/MacOS/Electron --self-test`(报告写进包内 Resources/app/selftest-report.json;报告含 screenPermission、userActivation、rawDisplayMedia 原始错误、system/mic 采样字节数)。
- 2026-10-22 本机 CoreAudio 曾整机瘫痪(afplay 报 -66681,输入输出皆无帧,`sudo killall coreaudiod` 或重启可解)。若任何应用都录不到声音,先用 `afplay` 一个 wav 判别是不是系统问题,别怀疑代码。

## 命令行转录(不经工作台)

```bash
cd /Users/rendy/财务知识/.tools/whisper.cpp
afconvert -f WAVE -d LEI16@16000 -c 1 输入.m4a 输出.wav
./build/bin/whisper-cli -m models/ggml-large-v3-turbo.bin -f 输出.wav -l zh \
  --prompt "以下是一场中文普通话会议的逐字记录,使用简体中文。" \
  -otxt -olrc -of 输出前缀 -pp
```

整理成文稿时参照 `会议/转录/_cleanup.py`。该脚本做三件事:删重复循环和静音幻觉句、替换系统性错词、按议题分段。

## 中文识别错词表

whisper 转中文口语音频会稳定错一批词。清理时优先检查:

| 识别成 | 实际是 |
| --- | --- |
| 英国 | 因果(部分段落整段错) |
| 快递、快议 | 会计 |
| 兔 | Tool |
| Hardis、查理斯 | Harness |
| ASFO | AI CFO |
| 大元、大远 | 大语言 |
| A站 | Agent |
| 代理机长 | 代理记账 |
| 省计 | 审计 |
| 孙式 | 复式 |
| 新年识 | 先验认知 |
| 架国、架架 | 架构 |

人名一律不可靠,按读音转写。静音处会出现「点赞订阅」类幻觉句,直接删。

## 环境约束

- brew、ffmpeg、node **允许按需安装**(2026-10-23 用户解除原"无 brew"约束;当前尚未安装,音频转码默认仍用系统 `afconvert`,新工具落地后同步更新本节)。
- 无 node 时的 JS 语法检查:`ELECTRON_RUN_AS_NODE=1 .tools/electron/Electron.app/Contents/MacOS/Electron --check <file>`。
- 本机 `say` 合成测试音频:新式神经声音(Eddy/Flo/Grandma 等)`-o` 落盘只有 0.016s 空音频,只有经典声音(Tingting、英文 Samantha)能正常落盘。
- Python 3.9。`pip --user` 装的包在 `~/Library/Python/3.9/bin`,含 cmake,需加入 PATH。
- 本地编译任何 C/C++ 代码前必须 `export SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk`。默认 SDK 27.0 与 CommandLineTools 链接器不兼容,报 `ld: tapi error: unknown architecture`。
- `afconvert` 输出的 WAV 格式标签是 65534(WAVE_FORMAT_EXTENSIBLE),标准库 `wave` 读不了。`app.py` 的 `read_wav_float` 是自写的 RIFF 解析,勿换回 `wave`。
- 重启服务时注意:进程命令行显示为 `Python app.py` 而非 `python3 app.py`。用 `lsof -nP -i :8765` 找 PID 再 kill,别用 `pkill -f "python3 app.py"`(匹配不上)。

## 约定

- 音频、声纹数据不出本机。例外(2026-10-23 用户明示授权):实时字幕 Pass3 可将**转录文本的窗口级片段**(仅窗口文本+有限上下文,不含音频/路径/说话人姓名)送 MiniMax API 做修正与翻译;key 在根目录 `.env`(`MINIMAX_API_KEY=sk-…`,2026-10-24 起优先;旧 `env.rtf` 仍兼容,`parse_key` 级联 .env → env.rtf;两者均已 gitignore,运行时解析,不入库不打印)。除此之外不调用任何外部转录或上传服务。注意:本仓库 git 历史含早期已删文件的记录(声纹库 speakers.json 曾被跟踪,现已 `git rm --cached`);若将来要 push/外发仓库,先做历史审查。
- 交付文档用中文。纪要中的时间节点、数字、人名须提醒用户对照原声核实。
- 项目记忆里有本目录的工具链笔记(`meeting-transcription-pipeline`),与本文件互补,先读其一即可。
