# 实时字幕(双层识别 + LLM 修正翻译,桌面端)设计 spec

日期:2026-10-23 · 状态:待评审(v2,按用户指令改桌面端入口并增加 LLM 层)· 作者:ZCode(与用户两轮调研后的设计稿)

## 1. 意图与背景

给现有本地转录工作台(`transcriber/`)加**实时字幕**能力,第一入口是**桌面客户端(会议录制工作台.app)**:会议进行中,置顶悬浮字幕窗秒级滚动显示字幕;数秒后同一位置被 whisper 修正版替换并标注说话人;再之后由 LLM(MiniMax-M3.1-Flash)做文本级修正与翻译;会话结束后自动进入现有离线管线出终稿。

识别架构对标商业产品(豆包/飞书,调研见对话记录)的 dual-pass——**流式小模型先上屏 + 非流式大模型二遍修正 + 声纹聚类分人**——再叠加一层商业产品同款思路的"大模型上下文理解"(LLM 修正+翻译),全部音频不出本机。

**隐私约定变更(用户 2026-10-23 明示授权)**:本目录原约定"所有音频、文稿不出本机"。本功能中:**音频与声纹数据仍严格不出本机;转录文本(经 LLM 修正/翻译的窗口文本)按用户指令送 MiniMax API**。spec §6.8 列明出境数据的确切范围;API key 不入代码、不入日志。

## 2. 目标 / 非目标 / 反目标

**目标**
- Pass1(上屏):说话后 ≤1.5 秒出字(目标 <1 秒),流式 zipformer,CPU 推理。
- Pass2(修正):滞后 ≤ 窗口 + 5 秒,whisper large-v3-turbo 修正文本 + TitaNet 声纹标说话人;修正后文本不可变(definite 语义)。
- **Pass3(LLM,2026-10-23 新增)**:对 definite 文本异步做术语级修正 + 翻译,滞后 ≤ 窗口 + 10 秒;失败不影响主链路。
- **第一入口 = 桌面客户端**:会议录制工作台.app 内置置顶悬浮字幕窗;同一 live 页面浏览器亦可打开(调试用)。
- 会话结束一键转正:live 音频直接进现有 job 管线,终稿质量与现在完全一致。
- 实时层资源占用:CPU 单核 <50%,不阻塞已有功能。

**非目标(v1 明确不做)**
- ~~桌面客户端接入 v2~~(2026-10-23 用户指令提前到 v1)。
- 实时层说话人标签(只在 Pass2 标)、实时错词表人工维护界面、多并发实时会话(单会话,与 GPU 串行惯例一致)。
- 翻译目标语言 v1 只做 中→英 单向可配(架构上语言对是参数)。

**反目标(即使技术可行也不要)**
- 实时层追求逐字准确——质量责任在终稿管线,实时层只求"跟得上、看得懂"。
- **音频、声纹 embedding、原始录音永远不出本机**(MiniMax 只收 §6.8 列明的文本)。
- 改动现有 job 管线的行为或数据形状:**现有代码改动仅两处——`run_whisper` 外加 GPU 锁(§6.6),以及 `if __name__` 入口块改委托 `import app` 单实例(2026-10-23 Task 9 实测追认:`python3 app.py` 下 `__main__` 与 `import app` 双模块实例导致 GPU_LOCK 分裂、转正 job 进孤儿队列)**;live 会话放独立目录、转正走普通上传路径(§6.2/§6.7),`process_job`、`recover_interrupted`、jobs API 一概不动。

## 3. 约束(来自 AGENTS.md 与实测)

| 约束 | 来源 |
| --- | --- |
| 无 brew/ffmpeg/node;Python 3.9;pip --user | AGENTS.md 环境约束 |
| whisper 占 GPU,现架构单 worker 串行 | app.py:175 注释 |
| whisper turbo 30s 窗稳态 1.57s(RTF≈0.05);5s 窗 3.3s 且截断句循环 | 2026-10-23 本机实测 |
| 客户端已采 16kHz/16bit 单声道 PCM | recorder.js / desktop |
| sherpa_onnx 1.13.8 含 OnlineRecognizer(已验证);flask_sock 缺失 | 2026-10-23 核查 |
| 桌面客户端 = Electron 主窗口 loadURL(127.0.0.1:8765 工作台网页) | desktop/main.js:161 |
| MiniMax API:OpenAI 兼容 `POST https://api.minimaxi.com/v1/chat/completions`(中国域,2026-10-23 实测;国际域对国内 key 401),模型 `MiniMax-M3.1-Flash`;key 在 `/Users/rendy/财务知识/env.rtf`(RTF 包装单行 sk-,**不入 spec 明文/不入代码/不入日志**,运行时解析) | 2026-10-23 用户指令 + 官方文档 + Task 8 实测 |
| **另一 session 正在优化界面交互**(2026-10-23 11:33~12:02 已改 `static/index.html`、`static/recorder.js`、`desktop/main.js`、`desktop/guide.html`、`build-app.sh`) | 用户指令 + 文件 mtime;同步纪律见 §7.4 |
| 音频/声纹不出本机;文本出境范围以 §1 隐私约定变更为准 | AGENTS.md + 用户 2026-10-23 指令 |

## 4. 方案选型(Approaches)

**识别架构**三选一:

| 方案 | 结论 | 理由 |
| --- | --- | --- |
| A. 纯 whisper 滚动窗 | 否决 | 本机实测 5s 窗推理 3.3s(截断句 token 循环),延迟质量双输;whisper 无流式模式是根因 |
| B. 纯 zipformer 流式 | 否决 | 中文准确率/标点弱于 turbo,且无修正、无分人——退化成"只有第一遍" |
| **C. 双层 dual-pass + LLM 第三层(本方案)** | **选定** | 第一遍满足延迟,第二遍满足声学质量,第三遍(LLM)满足语义/术语/翻译;终稿仍由现有管线兜底;识别组件全部现成 |

**LLM 层(Pass3)为什么放 whisper 之后而不是替代它**:LLM 直接修正 Pass1 纯文本看似省一层,但 zipformer 的错误(同音字、专名、断句)对 LLM 是高歧义输入,幻觉改写风险大;whisper 先把声学错误修掉,LLM 只处理"文本级"问题(术语一致性、标点、口语顺滑、翻译),输入质量高一个量级。这也是商业产品"ASR→LLM 上下文理解"的分层。MiniMax-M3.1-Flash 用 OpenAI 兼容端点 + Python 标准库 `urllib` 直调(零新依赖,契合无 brew 环境)。

**传输通道**:选定 **WebSocket(flask-sock + simple-websocket,pip --user 安装,唯一新增依赖)**。备选 SSE+chunked POST 被否:音频上行和字幕下行两条链路,断线恢复语义更碎;localhost 下 WS 开销无感。纯轮询否:100ms 级延迟做不到。

**第一入口**(2026-10-23 用户拍板,推翻 v1 稿我代定的"网页先行"):**桌面客户端会议录制工作台.app**。实现上仍是同一个 live 页面(§7)——Electron 悬浮窗加载它,浏览器也能开(调试);桌面端增量 = 悬浮窗管理 + 音频采集接入(main.js 改动,隔离策略见 §7.4)。

## 5. 架构总览

```
会议录制工作台.app(第一入口)
   ├─ 主窗口:现有工作台页面(不变)
   └─ 字幕悬浮窗:无边框置顶小窗,加载 live 页面,可拖动/收起
        │ 采音(getUserMedia 麦克风 / display-capture 系统声音,沿用现有 TCC 引导)
        │ WebSocket /ws/live     (二进制帧=PCM 上行;JSON=控制/字幕下行)
        ▼
app.py 新增 live 会话域(与 jobs 域隔离,仅共享模型路径)
   ├─ 音频分配线程:收包 → 环形缓冲 → 追加写 live_audio.wav → 喂 Pass1
   ├─ Pass1 线程:OnlineRecognizer(streaming zipformer 中英双语)
   │     partial(实时刷新) / final(句定稿) → WS 推字幕
   ├─ Pass2 调度:final 句累积成窗(§6.3)→ 切 live_audio.wav 片段
   │     → GPU 锁 → whisper-cli(同现有参数+上窗末句作 --prompt)
   │     → 段落级 titanet 声纹在线聚类 → WS 推修正(definite)
   ├─ Pass3 队列(异步,不阻塞字幕):definite 文本 → MiniMax-M3.1-Flash
   │     修正(术语/标点/顺滑)+ 翻译 → WS 推 llm 软更新(§6.8)
   └─ 停止:补跑最后一窗 → live_audio.wav 硬链接为普通 job 的 source → 现有管线出终稿
```

## 6. 关键机制

### 6.1 Pass1:流式识别(上屏层)

- 模型:`sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20`(encoder/decoder/joiner + tokens,约 300MB,下载一次到 `models/`;获取命令写进实施计划,不进 git 类产物)。
- 参数:CPU 2 线程(不与 whisper GPU 进程抢核);`rule1/2/3` 按模型默认;endpoint 配置 `rule2: 0.6s@1.0s, rule3: 1.0s@2.0s`(≥1s 说话后 0.6s 静音、或 ≥2s 说话后 1s 静音判句终)。
- 输出:partial(每次解码刷新,WS 下行 `partial`)与 final(endpoint 触发,句级时间戳)。
- 已知短板(写明,不掩盖):无标点或弱标点、中英混读强但纯中文准确率低于 turbo——这正是 Pass2 存在的理由;M0 里程碑先量化(§9)。

### 6.2 音频路径与磁盘形状

- 上行二进制帧 = s16le/16k/mono,建议 100~200ms/包(对齐火山引擎公开的工程参数)。
- **live 会话独立目录 `transcriber/live/<id>/`**,不进 jobs/ 扫描范围、不出现在任务列表——这是对现有管线的隔离边界(`recover_interrupted` 扫 jobs/,不会误杀 live 会话;jobs API 不需要认识 live)。目录内:
  - `live_meta.json` — 自有状态机:`listening → stopping(补跑最后一窗)→ finalized`,含 `last_seq`、`bytes_received`(时间轴锚点)等;
  - `live_audio.wav` — 边录边追加的标准 RIFF WAV(自写 44 字节头,定稿时回填长度;复用 `read_wav_float` 读);
  - `live_pass1.jsonl` — Pass1 final 句流(时间戳+文本),审计与 M0 评测用。
- **转正(stop)**:以 `live_audio.wav` 硬链接为 `source.wav` 创建一个**普通 job**——afconvert 对已是 16k 单声道 PCM 的 wav 是恒等转换,白跑一遍无害,换来现有管线零改动;live 目录在 job 建成后删除(音频以硬链接共享,不占双份空间)。

### 6.3 Pass2:窗口与触发(definite 语义)

- 光标 `t_cursor` 起切。触发条件(满足其一):
  1. final 句自光标累计 ≥ **20s** 且当前静音 ≥1.5s(句边界切,whisper 吃完整句);
  2. 强制:累计 ≥ **35s** 不管静音(长 uninterrupted 发言也要出修正)。
- 切片 `[t_cursor, t_end]`,边距各 +200ms 重叠;`--prompt` 注入上一窗口最后一句(语境延续,抑制断句幻觉);其余参数与 `run_whisper` 完全一致。
- 修正下发:`{type:'pass2', t0, t1, speaker, text, seq}`——前端**替换** `[t0,t1]` 区间已显示的 Pass1 定稿文本,替换后不可变。窗口内 whisper 多段时**合并为单事件**(文本拼接 + 主导说话人簇号;2026-10-23 实施定稿,与 §8 窗口粒度验收一致)。

### 6.4 说话人(Pass2 内,v1 仅在线聚类)

- **v1 不做锚点比对**(2026-10-23 builder check 裁定:现 `models/bench/anchors.json` 是 bench 检定夹具——按数据集分组、指向具体 job 音频的时间窗、无预计算 embedding、随 job 删除而失效——不是声纹注册表,直接复用会引入一套新的注册格式与生命周期问题)。锚点注册表(独立音频/预计算 embedding/增删改)整体挪 **v1.1**。
- v1 流程:whisper 段落音频(按段落时间戳从切片 WAV 切出)→ `SpeakerEmbeddingExtractor`(titanet)→ 与**本会话已积累的簇心**余弦比对,距离 > 阈值 θ 开新簇;θ 初值 0.65,**M0 用 bench 已有双场音频(中文会议/英文双男声)对在线聚类重标定**(不能沿用锚点命中经验值)。
- 实时层标签只到"说话人N";姓名映射与最终分人都归终稿 diarization,**实时标签不落 transcript.json**——因此聚类错误不致命,θ 取保守值(宁多开簇不错绑)。
- 会话冷启动无任何先验,前 1~2 分钟分人不稳属预期,UI 显示"分人收敛中"。

### 6.5 WS 消息协议(v1)

```
C→S  {type:'start', title?, speakers?}          → 创建 live 会话,返回 session_id
C→S  二进制帧(s16le/16k/mono PCM,严格按采集顺序)
C→S  {type:'audio_flush_done'}                  → 断线补发批次结束(见 §6.7)
C→S  {type:'stop'}                              → 停止:补跑最后一窗后转正
C→S  {type:'resume', session_id, last_seq}      → 断线重连(服务端直接重放 backlog,无 resumed 应答;前端以首条带 seq 事件为已恢复信号——2026-10-23 实施定稿)
S→C  {type:'started', session_id}
S→C  {type:'resumed', backlog:[缺失下行消息]}    — ~~音频补发完成前暂停下行字幕~~ 2026-10-23 实施定稿:此消息未实现,resume 即重放 backlog;`audio_flush_done` 服务端 v1 忽略(字节计数锚定无需特殊处理,前向兼容保留)
S→C  {type:'partial', text}                     — 覆盖当前活动行
S→C  {type:'pass1_final', text, t0, t1, seq}    — 追加为待修正行
S→C  {type:'pass2', t0, t1, speaker, text, seq} — 替换区间,definite
S→C  {type:'llm', t0, t1, text, translation, seq} — Pass3 软更新该窗文本并附翻译(§6.8)
S→C  {type:'pass2_skip', t0, t1, reason}        — 纯静音窗等跳过情形
S→C  {type:'stop_ack'}                          — 已停收音频,正在补跑最后一窗
S→C  {type:'stopped', job_id} / {type:'error', msg}
```

服务端按**累计收到的 PCM 字节数**维持时间轴(每 32000 字节 = 1 秒),不依赖包时间戳——断线补发按序追加即可对齐。

### 6.6 GPU 串行(与现有 worker 互斥)

模块级 `GPU_LOCK = threading.Lock()`:
- `run_whisper`(现有)调用 whisper-cli 前后 acquire/release——**唯一一处现有代码改动**;
- Pass2 用独立的切片转录函数(参数构造复用 `run_whisper` 的 cmd 拼装逻辑,但不带 `write_meta` 副作用),同一把锁。
后果(接受):实时会话期间同时提交大 job 会让 Pass2 排队、字幕滞后拉长;UI 在实时页提示"后台有任务在转录,修正会变慢"。不做抢占。

### 6.7 停止、断线与恢复

**stop 语义(异步状态机,不阻塞 WS 线程)**:
1. 服务端收到 `{type:'stop'}` → 停收音频,回 `{type:'stop_ack'}`,状态机 `listening → stopping`;
2. **强制补跑最后一窗**(光标之后的音频,哪怕不足 20s)——实时层的价值就在"最后说的话也有修正版",丢弃与产品意图矛盾;
3. 最后一窗的 pass2 消息照常下发 → 回填 WAV 头部长度 → 转正建 job(§6.2)→ `{type:'stopped', job_id}`。
   stop 到 stopped 之间多等数秒(1 窗 whisper ≈1~2s),属预期。

**断线重连**:音频上行唯一通道就是 WS。断线期间浏览器**继续采集并在本地缓冲 16k PCM**(上限 5 分钟,超出截断丢最旧,UI 提示);重连 `resume` 后:先补发缓冲的二进制帧 → 发 `{type:'audio_flush_done'}` → 服务端恢复下行字幕。期间服务端持续把已收音频落盘,识别暂停但录音不断。

**服务重启**:live 目录不在 `recover_interrupted` 扫描范围(jobs/ 才扫)。启动时发现未终态的 live 目录:音频已保全 → 直接回填 WAV 头、自动转正建 job(比报错强,终稿总能拿到)。

### 6.8 Pass3:LLM 修正与翻译(MiniMax-M3.1-Flash,2026-10-23 新增)

**定位**:对 Pass2 definite 文本做**软更新**——异步、尽力而为,失败/超时永远保留 whisper 文本,不阻塞、不撤销任何已下发内容。字幕由此呈现三级状态:partial(灰)→ whisper 修正(黑,带说话人)→ LLM 修正+翻译(替换文本 + 附译文行)。

**调用方式**
- `POST https://api.minimaxi.com/v1/chat/completions`(OpenAI 兼容;**2026-10-23 实测纠偏:中国域 `api.minimaxi.com` 可用,国际域 `api.minimax.io` 对本机 key 稳定 401**),model=`MiniMax-M3.1-Flash`;Python 标准库 `urllib.request` 直调,不装 SDK(零新依赖)。
- key:启动时从 `/Users/rendy/财务知识/env.rtf` 读取(该文件是 RTF 包装的单行,解析时剥 RTF 标记提取 `sk-` 串),仅存内存;缺失时 Pass3 整层禁用并告警,Pass1/2 不受影响。key 不写入任何日志、meta 或下发明文。
- 请求:非流式,超时 8s,重试 1 次(退避 2s);逐窗串行队列(20s+ 一窗,远低于速率限制)。

**Prompt 契约(关键,防幻觉)**
- 输入:本窗 whisper 文本(带说话人标签)+ 上一窗 LLM 已修正文本(滚动上下文 ≤2 窗)+ 可选术语表(v1 预留字段,AGENTS.md 错词表风格)。
- 指令约束:只修 ASR 类错误(同音字、错词、专名、标点、口语顺滑),**不改意思、不加不删内容、不合并说话人**;输出 JSON `{"corrected": "...", "translation": "..."}`,translation 为指定目标语言(v1 默认 zh→en);拒绝修正时 corrected 原样返回。
- 温度 0;max_tokens 下限 1536(2026-10-23 实测:M3.1-Flash 是推理模型,思考 token 计入 max_tokens,低封顶会把 content 截空)。

**出境数据范围(隐私边界,写死)**:仅 `(本窗文本, 前窗已修正文本, 术语表)` 三个字符串字段 + 模型名。**不出境**:音频任何形式、时间戳之外的元数据、说话人姓名(只有"说话人N"编号)、文件路径、其他 job 内容。终端稿(transcript.md)是否调用 LLM 清理**不在本 spec 范围**(仍走现有人工流程)。

**失败语义**:超时/HTTP 错误/JSON 解析失败 → 本窗标记 `unrefined`,UI 显示 whisper 版本文本(无译文);连续 5 窗失败 → Pass3 自动暂停并提示(网络/额度问题),会话其余部分照常。

## 7. 前端:live 页面与桌面端接入

### 7.1 live 页面(`static/live.html` + `live.js`,新建文件,零冲突)

- 三级显示:活动行(Pass1 partial,灰色)→ 待修正行(Pass1 final)→ definite 区(Pass2 文本+说话人着色;LLM 到达后文本软替换并附译文行);
- Pass2/Pass3 替换时旧文本淡出、新文本淡入(修正跳动是设计行为,UX 缓冲);
- 控制:开始/停止(**按钮文案就叫「停止」**(2026-10-23 用户定),内部语义即转正,完成后显示跳转 job 链接)、说话人数预设(传给终稿 job)、语言对设置;
- 采样:**不复用 recorder.js**——它录制期缓存的是 AudioContext 实际捕获率(16k 构造失败时 48k),只在 `stop()` 才统一降采样(builder check 核实);live.js 必须在采集路径上流式降采样到 16k(16k AudioContext 构造失败时,按捕获率逐块重采样后上行),断线缓冲的也是 16k 数据。

### 7.2 桌面端接入(会议录制工作台.app)

- **字幕悬浮窗**:main 进程新增 `desktop/live-window.js` 模块——无边框、置顶(alwaysOnTop)、可拖动、窄条形小窗,`loadURL(SERVER_URL + '/live')`;主窗口工作台页加"实时字幕"入口(见 §7.4 接入策略)。悬浮窗内即 live 页面,功能完整(开始/停止/设置);收起 = 隐藏窗口(会话不受影响)。
- **音频采集**:悬浮窗内 `getUserMedia`(麦克风);系统声音(对方声音)走 `getDisplayMedia`,复用现有客户端的 TCC 授权引导链路(AGENTS.md 两个硬坑已解:`display-capture` 权限类型、TCC 责任进程归属——验证必须 `open .app --args --self-test`)。双源混流与现有录制一致;**系统声音链路已有客户端验证基础,v1 桌面端直接支持**。
- **构建**:`desktop/build-app.sh` 重新打包(幂等;稳定自签证书 rendy-local-dev 已就位);改动后跑 `--self-test` 确认权限链路无回归。
- 浏览器打开 `http://127.0.0.1:8765/live` 等价可用(调试入口;浏览器无系统声音权限时仅麦克风)。

### 7.3 与 UI 优化 session 的协作边界(2026-10-23 起并行开发)

另一 session 正在改 `static/index.html`、`static/recorder.js`、`desktop/main.js`、`desktop/guide.html`、`build-app.sh`(当日 11:33~12:02 已有改动)——与本项目几乎全部共享文件重叠。纪律:

| 文件 | 所有权 | 本项目策略 |
| --- | --- | --- |
| `static/live.html`、`static/live.js` | 本项目新建 | 零冲突 |
| `desktop/live-window.js` | 本项目新建 | 零冲突 |
| `app.py` | 本项目(live 域 + GPU 锁一行) | UI session 不碰后端 |
| `static/index.html` | UI session(《界面重设计-spec》:上传/现场录制双 Tab,桌面端默认录制 Tab) | **不改其结构**;实时字幕入口融入其信息架构——候选落点 = 录制 Tab 内的"实时字幕"开关(S3 录制面板),与其 session 对齐后落;悬浮窗独立于主窗口,不依赖其布局 |
| `static/recorder.js` | UI session | 不改;live.js 独立实现(仅参考其采集方式) |
| `desktop/main.js` | UI session(正在改) | **延迟接入**:live-window.js 设计为"一行 require + 一个菜单项"的最小补丁,等 main.js 当前批次稳定后再打;避免同文件双写冲突 |
| `desktop/guide.html`、`build-app.sh` | UI session | 不碰;悬浮窗不需要新权限,引导无新增 |

- 同步检查点:每次动共享文件前先 `ls -lt` + diff 对方最新改动;两 session 的里程碑排序(§9)已把桌面端接入放最后,天然错峰。
- 若合并仍冲突:回退到"live 页面先以浏览器入口交付,桌面悬浮窗随后补"——后端与页面不受影响。

## 8. 验收标准

| 项 | 标准 | 测法 |
| --- | --- | --- |
| Pass1 延迟 | partial 首包 ≤1.5s(目标 <1s) | 回放模式打点 |
| Pass1 质量 | 中文会 CER 相对 whisper 终稿 ≤2.5×(能看懂即可) | M0 基准脚本,进 models/bench/ 惯例 |
| Pass2 滞后 | 20s 窗场景 ≤25s | 回放打点 |
| 修正质量 | definite 区文本 = 该窗 whisper 独立转写结果(按构造成立,抽检) | 对拍 |
| Pass3 滞后 | LLM 软更新 ≤ 窗口 + 10s(正常网络) | 回放打点 |
| Pass3 无损降级 | 断网/无 key/超时下,字幕主链路(Pass1/2)表现与无 Pass3 完全一致 | 断网回放 |
| 翻译可用率 | 有译文窗占比 ≥95%(正常网络,排除纯静音窗) | 统计 |
| LLM 不改意 | 抽检 20 窗,无"改意思/加内容/删内容"案例(prompt 契约遵守) | 人工抽检 |
| 桌面端入口 | 悬浮窗全流程可用;`--self-test` 通过(麦克风+系统声音采样字节 >0);build-app.sh 重打包后 TCC 授权不失效(稳定证书) | 真机 |
| 分人 | 双人会在线聚类标签与终稿 diarization 一致率 ≥80%(窗口粒度;前 2 分钟冷启动不计入) | 对拍 |
| 稳定性 | 2 小时回放无崩溃、内存平稳(环形缓冲上限可配,默认 5 分钟) | 长跑 |
| 终稿 | live 会话转正产物与同一音频直接上传的产物一致(afconvert 恒等转换不引入差异) | 对拍 |

## 9. 里程碑(桌面端接入排最后,与 UI session 错峰)

- **M0 模型与基线(先证可行再动 app.py)**:下载流式 zipformer;用回放脚本(吃已有 job 的 audio16k.wav,不碰录音链路)测:延迟打点、CER(vs whisper 终稿)、中文+英文双男声两个场景、在线聚类 θ 阈值标定。产出 bench 脚本与数据进 `models/bench/`。**CER 若 >3× 终稿,回此 spec 重议模型(备选:sherpa-onnx streaming paraformer-zh)。**
- **M1 Pass1 上屏**:flask-sock 依赖、/ws/live、live.html 最小版(仅 partial/final,无修正)。回放+真人双验证(浏览器入口调试)。
- **M2 Pass2 修正+分人**:窗口调度、GPU 锁、titanet 在线聚类、pass2 消息。
- **M3 Pass3 LLM 修正+翻译**(新增):env.rtf key 解析、逐窗串行队列、llm 消息、熔断降级;断网回放验证降级无损。
- **M4 转正与体验**:stop→job、resume、重启恢复、UI 打磨。
- **M5 桌面端接入**:live-window.js 悬浮窗 + main.js 最小补丁 + 重打包 + `--self-test`;**安排在 UI session 当前批次落地之后**(§7.3)。
- (v1.1:锚点注册表;翻译多语言对;术语表配置界面。)

## 10. 风险与开放问题

| 风险 | 缓解 |
| --- | --- |
| zipformer 中文质量不达标 | M0 闸门 + paraformer 备选;架构与模型解耦(Pass1 是可换实现) |
| 长会话漂移(切片累计误差) | WAV 长度由写入字节数直接决定,时间戳单调;M3 长跑验证 |
| Pass2 排队致字幕长期不修正 | GPU 锁竞争透明化提示;35s 强制窗保底 |
| flask-sock 与 Flask 3.1.3/Python 3.9 兼容 | 纯 Python 库,M1 首步装包即验;失败降级 SSE+POST(协议不变,只换载体) |
| 在线聚类冷启动不稳/簇数漂移 | 前 2 分钟 UI 标注"分人收敛中";标签不落终稿,终稿 diarization 兜底;θ 保守 |
| 断线久致浏览器缓冲溢出 | 缓冲上限 5 分钟,超出截断丢最旧并提示(丢的是未上行音频,时间轴仍由字节累计锚定) |
| LLM 幻觉改写(改意思/删内容) | prompt 硬约束 + 温度 0 + §8 抽检验收;LLM 文本只是软更新,UI 可对照 whisper 版 |
| MiniMax 限流/额度/网络抖动 | 逐窗串行 + 8s 超时 + 连续 5 窗失败熔断;Pass3 整层可禁用,主链路不受影响 |
| API key 泄露 | 仅内存持有;不打印不落盘;env.rtf 在 财务知识/ 根目录,不进 desktop 打包产物(build 只收 transcriber/desktop) |
| 与 UI session 合并冲突(main.js/index.html/recorder.js) | §7.3 所有权表 + 新建文件零冲突策略 + 桌面端接入排最后 + 一行式最小补丁 |
| 开放:锚点注册表格式与 UX | v1.1;v1 无锚点,纯会话内在线聚类 |

## 11. 决策记录(我代定的,均可推翻)

1. 入口选网页实时页(用户未及拍板;桌面端 v2)。
2. Pass1 模型选 bilingual zh-en zipformer 2023-02-20(M0 验证,不达标换 paraformer)。
3. 传输选 WebSocket + flask-sock 唯一新依赖。
4. 窗口 20s/35s、静音 1.5s、切片边距 200ms——标定量,M0/M2 校准。
5. Pass2 说话人按 whisper 段落粒度(非切片粒度),实时标签不进终稿;**v1 只做会话内在线聚类(θ=0.65 起步),锚点注册表挪 v1.1**(builder check 修订:原稿 §6.4/§10 自相矛盾,且 anchors.json 是 bench 夹具非注册表)。
6. **live 会话独立目录 `live/<id>/`,转正以硬链接走普通上传路径**;现有代码唯一改动 = GPU 锁(builder check 修订:原方案混入 jobs/ 需要改 `recover_interrupted`/`write_meta`/列表渲染等至少 5 处,违背反目标)。
7. stop 为异步状态机:停收音频 → 强制补跑最后一窗 → 转正(builder check 修订:原稿未定义在飞窗口的处置)。
8. 断线语义:浏览器缓冲 16k PCM(上限 5 分钟),重连补发二进制帧 + `audio_flush_done` 信号;服务端按累计字节锚定时间轴(builder check 修订:原稿"继续 POST"与协议不符)。
9. **入口 = 桌面客户端**(2026-10-23 用户拍板,推翻 v1 稿我代定的"网页先行");live 页面浏览器仍可开(调试),桌面端以悬浮窗承载。
10. **Pass3 = MiniMax-M3.1-Flash**(2026-10-23 用户指令):urllib 直调 OpenAI 兼容端点(零新依赖);软更新语义、逐窗串行、失败熔断;**仅窗口文本+有限上下文出境**(隐私约定变更已记录于 §1,音频/声纹仍禁出境);key 运行时从 env.rtf 解析,不入任何产物。
11. 翻译 v1 只做 zh→en(语言对为参数);术语表为 v1 预留字段。
12. 桌面端 main.js 采用"独立模块 live-window.js + 一行接入"最小补丁,时间上排在 UI session 当前批次之后(§7.3 错峰)。
13. UI 文案(2026-10-23 用户定):停止按钮就叫「停止」,结束提示为「录音已保存 · 正在生成完整文稿」;"转正"仅作为本文档术语,不出现在任何界面文案。悬浮窗双形态(半透明置顶字幕条 ↔ 完整面板)线框已过用户初确认(2026-10-23,字幕条文案细节见线框),其余五个确认点(信息量/译文位置/配色/hover 控制/切换关系)按线框默认落地,用户可后续再调。

## 12. 评审与变更记录

- **2026-10-23 builder check(v1 稿)**(独立子代理按"将要实施此 spec 的工程师"视角审读,用户要求的 plan-eng-review 技能不存在,按 brainstorming 技能内置的 builder check 环节执行):核实 spec"现状"声明全部属实(仅 recorder.js 降采样时机一处出入,已修正);提出 4 个阻塞性问题(锚点矛盾、断线音频语义、改动授权边界、stop 在飞窗口),全部按 §11 决策 5~8 修订入稿。
- **2026-10-23 v2 变更(用户三点指令)**:① 入口改桌面端(决策 9);② 新增 MiniMax-M3.1-Flash 修正+翻译层(§6.8,决策 10~11),核实官方 API 为 OpenAI 兼容端点、模型名 `MiniMax-M3.1-Flash`,env.rtf 实为 RTF 包装单行 key(spec 不录明文,运行时剥壳解析);③ UI 优化 session 并行改动确认(mtime 证据)并建立 §7.3 协作边界。隐私约定变更(文本出境)已显式记录于 §1。
- **2026-10-23 v2 补充**:读到 UI session 的《界面重设计-spec》(双 Tab 信息架构、桌面端默认录制 Tab、D6 声纹 UI)——§7.3 的 index.html 行已更新为"入口融入其录制 Tab";其 D6 说话人库与本 spec v1.1 锚点注册表是同一事物的两端,合并设计推迟到两者都进实施时对齐,避免两套声纹 UI。
