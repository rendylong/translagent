# 实时字幕(三层识别 + 桌面字幕条)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给本地会议转录工作台加实时字幕:流式 zipformer 秒级上屏 → whisper+titanet 窗口修正分人 → MiniMax LLM 修正翻译,桌面端悬浮字幕条呈现,停止后自动生成正式文稿。

**Architecture:** app.py 新增独立 live 会话域(WebSocket `/ws/live`,目录 `transcriber/live/<id>/`),三层流水线各自独立模块;现有 jobs 管线唯一改动是 `run_whisper` 外加 GPU 锁。桌面端为 Electron 悬浮窗加载 `/live` 页面,浏览器同地址可调试。

**Tech Stack:** Python 3.9 + Flask 3.1 + flask-sock(新依赖)、sherpa-onnx 1.13.8(OnlineRecognizer,已有)、whisper-cli 子进程(已有)、titanet 声纹(已有)、MiniMax-M3.1-Flash(urllib 直调)、原生 JS(无构建链)、Electron(desktop/,最后接入)。

**Spec:** `transcriber/docs/2026-10-23-live-captions-design.md`(本计划从 spec 出发,执行者两份都读;界面文案以 spec §11 决策 13 为准)

## Global Constraints

- 本目录**非 git 仓库**:任务不含 commit;若中途 `git init`,按任务边界补提交即可。
- 音频/声纹数据不出本机;**仅** Pass3 的窗口文本+≤2 窗上下文+术语表可发 MiniMax(spec §6.8 白名单)。
- MiniMax key 只在运行时从 `/Users/rendy/财务知识/env.rtf` 解析(RTF 壳内 `sk-` 串),不写死、不打印、不入日志与任何落盘文件。
- 现有代码唯一允许的改动:`app.py` 的 `run_whisper` 加 GPU 锁,以及 app.py 末尾注册 live 路由/恢复钩子的**追加式**代码;不改 `process_job`/`recover_interrupted`/jobs API。
- 新增运行依赖仅 `flask-sock`(pip --user);开发用 `websocket-client`(pip --user)。bin 路径 `~/Library/Python/3.9/bin`。
- 采样规格全链路 s16le/16kHz/单声道;live WAV 为标准 44 字节头(自写,勿用 `wave` 读 afconvert 产物,`app.py:read_wav_float` 是既有解析器)。
- UI 文案:停止按钮叫「停止」;「转正」一词不出现在任何界面。
- 与 UI 优化 session 并行开发:不碰 `static/index.html`、`static/recorder.js`、`desktop/main.js` 的既有内容(Task 10 除外,且需其批次先落地);只新建文件。
- Pass1 模型:`sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20`,存 `transcriber/models/`;Task 1 的 CER 闸门(≤3× whisper 终稿)不通过则停,回报 spec 换模型,不进 Task 2。

## Review Focus(五个最可能坑的输入类,测试已钉入所属任务)

1. **WS 断线补发后时间轴错位**——补发/重复字节不得移动已确认时间轴(服务端按累计字节锚定)→ Task 4 测试。
2. **超长会话(≥2h)WAV 头与切片偏移漂移**——finalize 回填长度、任意偏移切片读回样本正确 → Task 2/5 测试。
3. **LLM 超时/非 JSON/越权改写**——一律降级保留 whisper 文本,连续 5 窗熔断,主链路无感 → Task 8 测试(mock 三类故障)。
4. **GPU 锁竞争**(实时会话中提交大 job)——Pass2 排队等锁但 Pass1 持续出字,无死锁 → Task 5 测试。
5. **尾窗与静音窗**(stop 时光标后不足 20s;窗口全静音)——补跑尾窗不丢、静音窗发 `pass2_skip` 不崩 → Task 5/9 测试。

---

### Task 1: Pass1 模型与回放基线(M0 闸门)

**Files:**
- Create: `transcriber/models/streaming-zipformer-zh-en/`(解压产物)
- Create: `transcriber/models/bench/bench_streaming_asr.py`
- Test: 脚本自校验模式(无 unittest;bench 惯例)

**Interfaces:**
- Produces: 模型目录(含 encoder/decoder/joiner.onnx、tokens.txt);`bench_streaming_asr.py --job <id> --out <json>` 产出 `{partial_lag_ms_p50, cer_ratio, finals:[{t0_ms,t1_ms,text}]}`——Task 3 消费同一模型目录与 endpoint 参数。

- [ ] **Step 1: 下载解压模型**

Run: `cd transcriber/models && curl -L -o sz.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20.tar.bz2 && tar xjf sz.tar.bz2 && rm sz.tar.bz2 && mv sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20 streaming-zipformer-zh-en`
Expected: 目录内有 `encoder-epoch-99-avg-1.onnx`、`decoder-epoch-99-avg-1.onnx`、`joiner-epoch-99-avg-1.onnx`、`tokens.txt`。

- [ ] **Step 2: 写回放基准脚本**

`bench_streaming_asr.py`:加载 `jobs/<id>/audio16k.wav`(`app.py:read_wav_float` 同款 RIFF 解析,可 import app 或复制该函数);构造 `sherpa_onnx.OnlineRecognizer`(字段名以包内 `help(sherpa_onnx.OnlineRecognizerConfig)` 为准:transducer 三件套 + tokens + `num_threads=2` + `enable_endpoint=True`,endpoint `rule2_min_trailing_silence=0.6`、`rule3_min_utterance_length=1.0`,spec §6.1);按 200ms 块 `accept_waveform`,`is_endpoint` 时 `decode` 出 final;记录每块墙钟时间与文本首现时间差为 partial 延迟;最终句拼接 vs `jobs/<id>/transcript.json` 的 segments(时间重叠区间),字符级编辑距离得 CER,除以 whisper 终稿自身 CER(用 transcript.json 与经 `--temperature 0` 重转写对拍?不可行——简化:CER_ratio = Pass1_CER / 0.15,0.15 为 whisper 中文会议经验基线常数,在脚本头注释说明)。
校验输出落 `models/bench/stream_<jobid>.json`。

- [ ] **Step 3: 两个场景跑基线**

Run: 一次中文 job(如 `jobs/` 下任一中文会议)+ 一次英文双男声 job(bench anchors 用的那场)。
Expected: 中文 `cer_ratio ≤ 3.0` 且 `partial_lag_ms_p50 ≤ 1500`。**不达标:停止,报告数值,回 spec §9 重议模型(备选 streaming paraformer-zh),不开始 Task 2。**

- [ ] **Step 4: θ 初标定(顺带)**

脚本加 `--theta` 模式:对 final 句音频段提 titanet embedding(`sherpa_onnx.SpeakerEmbeddingExtractor`,模型 `models/speaker_embedding_titanet.onnx`),同/异人余弦分布打印分位——给 Task 6 的 θ 默认值提供依据,写入 json。

---

### Task 2: live 域目录与追加式 WAV 写入器

**Files:**
- Create: `transcriber/live_wav.py`
- Test: `transcriber/tests/test_live_wav.py`

**Interfaces:**
- Produces: `class AppendWav: __init__(self, path, sample_rate=16000) # 打开即写 44 字节标准头(长度 0)`;`append(self, pcm_bytes: bytes) -> int # 返回累计字节;偶数长度截断保护`;`finalize(self) -> None # 回填 RIFF/data 长度`;`duration_s(self) -> float # bytes/32000`;模块级 `write_slice(path, samples: np.float32, sample_rate=16000) -> None`(Task 5 切片用,标准头一次成型)。

- [ ] **Step 1: 失败测试** — `test_live_wav.py`:`test_roundtrip`(追加 32000×3 字节 → finalize → 用 `app.read_wav_float` 读回长度 3.0s 且样本值正确)、`test_odd_bytes_truncated`(追加 3 字节,duration_s 仍按偶数)、`test_header_before_finalize`(未 finalize 时文件长 44+数据,头内长度字段为 0,`duration_s` 仍由累计字节数给)。
- [ ] **Step 2: 跑测确认失败** — `cd transcriber && python3 -m unittest tests.test_live_wav -v`,Expected: FAIL(模块不存在)。
- [ ] **Step 3: 实现** — 头结构 `b'RIFF'+<36+len>+b'WAVEfmt '+<16,1,1,16000,32000,2,16>+b'data'+<len>`(LE);`finalize` 用 `seek` 回填两处长度。纯 `struct`+`open`,无依赖。
- [ ] **Step 4: 跑测通过** — 同 Step 2,Expected: 3 项 PASS。
- [ ] **Step 5: 2h 偏移验证** — 测试 `test_two_hour_header`:追加 32000×7200×2 字节(假数据可稀疏生成)后 finalize,断言头两处长度 = 数据字节。Run 同上。

---

### Task 3: Pass1 流式引擎(可复用线程封装)

**Files:**
- Create: `transcriber/live_pass1.py`
- Test: `transcriber/tests/test_live_pass1.py`

**Interfaces:**
- Produces: `class Pass1Engine: __init__(self, model_dir, on_partial: Callable[[str],None], on_final: Callable[[str,int,int],None], sample_rate=16000) # 2 线程,endpoint 同 Task 1`;`feed(self, pcm: np.float32) -> None # 追加驱动,非阻塞(内部自转线程)`;`finals(self) -> list[dict] # {text,t0_ms,t1_ms} 累积,线程安全`;`stop(self) -> None`。时间戳来自 result.timestamps 首/尾(探针确认字段名,Task 1 的 bench 已验证)。
- Consumes: Task 1 模型目录与 recognizer 参数。

- [ ] **Step 1: 失败测试** — 用 bench 中文 job 音频前 60s:`test_partial_then_final`(喂完后 5s 内收到 ≥1 次 partial 与 ≥1 次 final,final 的 t1_ms > t0_ms ≥ 0);`test_finals_grow_monotonic`(finals 的 t0_ms 单调不减)。
- [ ] **Step 2: 跑测失败** — `python3 -m unittest tests.test_live_pass1 -v`,Expected: FAIL。
- [ ] **Step 3: 实现** — 内部 `queue.Queue` 收 PCM,工作线程循环 `accept_waveform/input_finished/decode`,`is_endpoint()` 或 `is_ready_retrieve` 时回调;线程 daemon。
- [ ] **Step 4: 跑测通过** — Expected: PASS(慢测,音频 60s 真跑约 6-15s)。

---

### Task 4: WS 会话域(app.py 追加 + LiveSession)

**Files:**
- Create: `transcriber/live_session.py`
- Modify: `transcriber/app.py`(仅末尾追加:import + flask_sock 注册 + 启动钩子;见 Step 4)
- Test: `transcriber/tests/test_live_session.py`

**Interfaces:**
- Produces: `class LiveSession: __init__(self, sessions_root, model_dir)`;`start(self, title, speakers) -> str # session_id=时间戳+uuid4[:4],建 live/<id>/{live_meta.json,live_audio.wav(AppendWav),live_pass1.jsonl},起 Pass1Engine`;`feed_bytes(self, b: bytes) -> None # 字节计数锚定时间轴;单写者线程内调用`;`handle_stop(self) -> None # 状态 listening→stopping(实际转正在 Task 9 补)`;
  下行事件由 `events` 队列给 WS 层:`{'type':'partial'|'pass1_final'|'pass2'|'llm'|'pass2_skip'|'stop_ack'|'stopped'|'error', 'seq':int, ...}`(seq 单调,Task 5/8 往同一队列放)。
- Produces(WS 层,app.py 追加):`/ws/live` 收 `{type:'start'}`→started;二进制帧→feed_bytes;`{type:'stop'}`;`{type:'resume',session_id,last_seq}`→回放 backlog 后续推。
- Consumes: Task 2 `AppendWav`、Task 3 `Pass1Engine`。

- [ ] **Step 0: 装依赖** — `python3 -m pip install --user flask-sock websocket-client`;`python3 -c "import flask_sock, websocket; print('ok')"`。
- [ ] **Step 1: 失败测试** — `test_live_session.py`:`test_byte_anchor`(feed 32000×2 字节→duration_s≈2.0;重复喂同批字节 duration 仍单增不回退——模拟补发重放,服务端直接忽略重复 seq 不做去重,靠累计语义);`test_events_seq_monotonic`(喂含语音的 10s bench 音频,partial 事件 seq 严格递增);`test_meta_lifecycle`(start→live_meta.json status=='listening'、stop→'stopping')。
- [ ] **Step 2: 跑测失败** — Expected: FAIL。
- [ ] **Step 3: 实现 LiveSession** — 单音频写线程(收字节→AppendWav.append→Pass1Engine.feed→finals 落 jsonl→事件入队);`resume` 的 backlog 从 events 环形缓存(上限 1000)按 last_seq 重放。
- [ ] **Step 4: app.py 末尾追加** — `sock = Sock(app)` + `@sock.route('/ws/live')` 处理器(纯转发到 LiveSession,不含业务逻辑)+ `if __name__` 块前追加"启动时扫描 live/ 目录的恢复钩子调用(Task 9 实现,先留 `live_recover()` 空转)"。**不改既有函数。**
- [ ] **Step 5: 端到端冒烟** — 测试脚本 `tests/ws_smoke.py`(websocket-client):连 `ws://127.0.0.1:8765/ws/live`,start→发 bench 音频 16k 字节流(200ms/包)→断言 30s 内收到 ≥5 个 partial→发 stop→收 stop_ack;`python3 tests/ws_smoke.py` Expected: ok(需临时起 `python3 app.py`,先 `lsof -nP -i :8765` 确认端口空闲)。

---

### Task 5: Pass2 窗口调度 + whisper 切片 + GPU 锁

**Files:**
- Create: `transcriber/live_pass2.py`
- Modify: `transcriber/app.py:177`(run_whisper 内 subprocess.Popen 前后加 `GPU_LOCK.acquire()/release()`,锁定义放 app.py 顶部——**这是全局约束允许的唯一既有函数改动**)
- Test: `transcriber/tests/test_live_pass2.py`

**Interfaces:**
- Produces: `@dataclass WindowPlan: windows: list[tuple[int,int]] # (t0_ms,t1_ms);skip: list[tuple[int,int]];new_cursor_ms: int`
- Produces: `plan_windows(finals, cursor_ms, now_ms, silence_ms, cfg=Pass2Cfg(min_s=20, force_s=35, quiet_s=1.5, pad_ms=200)) -> WindowPlan`(纯函数,决策表见测试)
- Produces: `class Pass2Runner: __init__(self, wav_path, session_events, language='zh', prev_tail='')`;`run_window(self, t0_ms, t1_ms) -> list[dict] # 切片→GPU_LOCK→whisper-cli→段落 dict,并 emit pass2/pass2_skip`;whisper cmd 与 `app.run_whisper` 参数一致(`-oj` 临时目录读 json,`--prompt` = prev_tail,cwd=WHISPER_DIR;**在注释中标注"参数与 app.run_whisper 保持同步,勿单边改"**,不 import app 的函数以守住"不改既有代码"约束)。
- Consumes: Task 2 `write_slice`、app.py `GPU_LOCK`。

- [ ] **Step 1: 失败测试(决策表)** — `test_plan_windows`:①finals 累计 25s 且静音 2s → 1 窗 [cursor-200, t_end+200];②累计 38s 静音 0 → 1 窗(force);③累计 12s 静音 10s → 无窗;④区间纯静音(finals 空但音频有)由 Runner 判定 → `pass2_skip`;⑤尾窗(<20s)由调用方传 `force_tail=True` 补窗。
- [ ] **Step 2: 跑测失败** — Expected: FAIL。
- [ ] **Step 3: 实现 plan_windows + Pass2Runner** — 切片:read_wav_float 读 live WAV 区间(±pad)→`write_slice`→whisper-cli→解析 json(同 app.run_whisper 的 offsets 结构)。
- [ ] **Step 4: GPU 锁测试** — `test_gpu_lock_serializes`:两线程各调 `Pass2Runner.run_window`(或直接 acquire),断言互斥(第二个开始时间 ≥ 第一个结束);`test_pass1_not_blocked`:持锁状态下 `Pass1Engine.feed` 立即返回(<50ms)。
- [ ] **Step 5: app.py 加锁 + 回归** — run_whisper 改动后 `python3 -m unittest discover tests -v` 全绿;并手动确认:跑一次普通上传 job 仍正常(锁不改变单 worker 行为)。
- [ ] **Step 6: 端到端小样** — `tests/replay_two_pass.py`:回放 bench 中文 job 前 90s 走 Pass1+Pass2,断言收到 pass2 事件且其文本与 transcript.json 对应区间 CER < Pass1(修正有效)。

---

### Task 6: titanet 在线聚类分人

**Files:**
- Create: `transcriber/live_speaker.py`
- Test: `transcriber/tests/test_live_speaker.py`

**Interfaces:**
- Produces: `class OnlineSpeakerCluster: __init__(self, theta=Task1标定值, min_turn_s=1.0)`;`assign(self, seg_pcm: np.float32) -> int # 段音频→titanet embedding→比簇心;>θ 开新簇;不足 min_turn_s 归上一簇`;`n_speakers -> int`。Pass2Runner 集成:每 whisper 段落切出音频→assign→事件带 `speaker`。
- Consumes: `models/speaker_embedding_titanet.onnx`(路径常量与 app.py 一致)。

- [ ] **Step 1: 失败测试** — 用 bench `anchors.json` 的双说话人窗口音频:交替喂 A/B 各 3 段 → `n_speakers == 2` 且段→簇映射与 anchors 标签一致率 5/6 以上;首段必开簇。
- [ ] **Step 2: 跑测失败** — Expected: FAIL。
- [ ] **Step 3: 实现** — 复用 Task 1 Step 4 的 embedding 提取代码;簇心=均值,前 3 段内允许重并。
- [ ] **Step 4: 跑测通过 + 接入 Pass2Runner**(每段 speaker 字段),重跑 Task 5 Step 6 回放断言 pass2 事件含 speaker∈{0,1}。

---

### Task 7: live 前端页面(字幕条 + 完整面板)

**Files:**
- Create: `transcriber/static/live.html`、`transcriber/static/live.js`
- Test: 手测清单(无 JS 测试设施;服务端用 Task 4 冒烟环境)

**Interfaces:**
- Produces: 页面含双形态(线框定稿:半透明深色字幕条=1 定稿+译文+1 partial 灰行;完整面板=历史+状态徽标+「停止」),右上按钮互切;WS 客户端按 spec §6.5 协议;`resampleTo16k` 流式降采样(16k AudioContext 失败时按捕获率逐块线性重采样);断线缓冲上限 5 分钟,重连补发 + `audio_flush_done`。
- Consumes: `/ws/live` 协议(Task 4/5/8 的事件类型)。

- [ ] **Step 1: live.html + live.js 骨架** — 无构建链,风格对齐现有 static(内联少量 CSS);表单:title/speakers/语言对;开始→getUserMedia(麦克风优先)→WS。
- [ ] **Step 2: 三级渲染逻辑** — 消息驱动:partial 覆盖活动行;pass1_final 追加;pass2 按 [t0,t1] 替换并染色(说话人色 A #4C9AFF / B #FFB02E / N 灰);llm 软替换+译文行;pass2_skip 折叠。淡入过渡 200ms。
- [ ] **Step 3: 降采样与断线缓冲** — 实现流式重采样 + `hidden` 缓冲队列;`onclose` 自动 3s 重连 resume。
- [ ] **Step 4: 手测清单(逐项打勾)** — ①说话 2s 内出灰字;②~25s 后该句被替换并带说话人色;③LLM 文本/译文再后到达;④刷新页面 → resume 后字幕接续不丢窗;⑤拔网线 30s → 恢复后无时间轴跳变;⑥「停止」→ 显示"录音已保存 · 正在生成完整文稿"(Task 9 前允许显示占位状态)。
- [ ] **Step 5: 真人 15 分钟冒烟** — 麦克风对 speakers=2 对话,记录三级滞后体感与异常。

---

### Task 8: Pass3 LLM 修正翻译(MiniMax-M3.1-Flash)

**Files:**
- Create: `transcriber/live_llm.py`
- Test: `transcriber/tests/test_live_llm.py`

**Interfaces:**
- Produces: `parse_key(path='/Users/rendy/财务知识/env.rtf') -> str | None # 剥 RTF 控制字,提取首个 sk- 开头可打印串;纯文本文件也能读`。
- Produces: `class LLMRefiner: __init__(self, api_key, lang_pair=('zh','en'), timeout_s=8)`;`refine(self, text, prev_refined='') -> dict|None # {'corrected','translation'};失败 None`;`fused -> bool # 连续 5 次失败熔断`;内部 `urllib.request` POST `https://api.minimax.io/v1/chat/completions`,model `MiniMax-M3.1-Flash`,temperature 0,prompt 契约按 spec §6.8(只修 ASR 错误/不改意思/JSON 输出);LiveSession 侧串行队列消费 pass2 事件后 emit `llm` 事件。
- Consumes: pass2 事件的 text/speaker。

- [ ] **Step 1: 失败测试(key 解析)** — `test_parse_key_rtf`(构造迷你 RTF 壳)、`test_parse_key_plain`(纯文本)、`test_parse_key_none`(无 sk- 返回 None)。
- [ ] **Step 2: 失败测试(refiner,monkeypatch urllib)** — `test_timeout_returns_none`、`test_bad_json_returns_none`、`test_fuse_after_5`、`test_prompt_contract`(捕获请求 body:temperature==0、输入含 prev_refined、不含音频/路径字段)。
- [ ] **Step 3: 跑测失败** — Expected: FAIL ×9。
- [ ] **Step 4: 实现** — RTF 剥壳:丢弃 `\` 控制字与花括号后正则 `sk-[A-Za-z0-9_-]{20,}`;HTTP 异常/超时/JSON 解析错误/字段缺失一律 None;计数熔断。
- [ ] **Step 5: 跑测通过** — Expected: 9 项 PASS。
- [ ] **Step 6: 真调用一次** — 测试标记 `@unittest.skipUnless(os.path.exists(env.rtf))`:真 key refine 一句"这季度毛利下滑主要是运费和促销投放两块",断言返回含非空 translation;打印结果供人工检查"不改意"。**禁止把 key 打进任何输出。**
- [ ] **Step 7: 接入 LiveSession 串行队列** — pass2 事件后异步 refine,emit `llm`;手测:回放模式看 llm 事件到达且 UI 软替换(Task 7 页面已支持)。

---

### Task 9: 停止转正 + 断电恢复 + 长跑

**Files:**
- Create: `transcriber/live_finalize.py`
- Modify: `transcriber/live_session.py`(stop 状态机接 finalize)
- Test: `transcriber/tests/test_live_finalize.py`

**Interfaces:**
- Produces: `finalize_session(session_dir, speakers_hint) -> str # job_id`:①状态 stopping;②强制补跑尾窗(force_tail);③AppendWav.finalize;④硬链接 `live_audio.wav` → `jobs/<new>/source.wav`;⑤写 meta(status='queued', source_file='source.wav', language/speakers 透传, title='实时 '+原 title)入 `job_queue`;⑥删 live 目录(保留 7 天可改为 move 到 live/_done/——按 spec §6.2 删除,hardlink 下不占双空间);返回 job_id。
- Produces: `live_recover() -> None`:启动时扫 `live/`,非终态目录(任一 crash)→ finalize(无尾窗补跑,音频保全即转正)。
- Consumes: app.py 的 `job_queue`、Task 5 Runner。

- [ ] **Step 1: 失败测试** — `test_finalize_creates_job`(造 3s 假 live 目录→finalize→jobs/ 下出现目录、meta.status=='queued'、source.wav 与 live 音频字节相同);`test_recover_auto_finalizes`(造 listening 态目录→live_recover→同上);`test_stop_async`(handle_stop 立即返回,事件流先 stop_ack 后 stopped,间隔≥whisper 一窗时间)。
- [ ] **Step 2: 跑测失败** → **Step 3: 实现** → **Step 4: 跑测通过**(常规 TDD 循环)。
- [ ] **Step 5: 终稿对拍** — `tests/replay_e2e.py`:同一 bench 音频 ①直接上传出 job ②走 live 回放+finalize 出 job,两 transcript.json 的 segments 文本一致(允许时间戳±200ms)。Expected: 一致(afconvert 恒等性的实证)。
- [ ] **Step 6: 2 小时长跑** — 回放 2h(循环拼接 bench 音频),记录:内存平稳(RSS 波动 <20%)、无未捕获异常、pass2 全窗出齐。失败回 Task 对应模块修。

---

### Task 10: 桌面端悬浮字幕窗(前置:UI session 批次已落地)

**Files:**
- Create: `transcriber/desktop/live-window.js`
- Modify: `transcriber/desktop/main.js`(**仅两处追加**:顶部 `require('./live-window')` + 菜单/ Dock 菜单项"实时字幕";动手前 `ls -lt` + 通读该文件最新版,确认 UI session 改动已合并)
- Test: 真机手测 + `--self-test`

**Interfaces:**
- Produces: `live-window.js` 导出 `createLiveCaptionWindow(serverUrl)`:`BrowserWindow({frame:false, alwaysOnTop:'screen-saver', transparent:true, hasShadow:false, width:560, height:120, resizable:true})`,loadURL `serverUrl + '/live?mode=bar'`(live.html 读 query 默认进字幕条形态);关闭=隐藏保留会话。

- [ ] **Step 1: 实现 live-window.js** — 窗口定位屏幕底部居中(`screen.getPrimaryDisplay().workArea`)。
- [ ] **Step 2: main.js 两处追加** — 与 UI session 最新代码合并后打补丁;确认无重名函数。
- [ ] **Step 3: 重建与自检** — `bash desktop/build-app.sh`;`open 会议录制工作台.app --args --self-test`,读包内 `selftest-report.json`:system/mic 采样字节 >0、screenPermission granted。
- [ ] **Step 4: 真机清单** — ①悬浮条叠加在视频会议上方且半透明;②拖动改变位置(位置 localStorage 记忆);③切完整面板→切回;④系统声音源会议(对方声音)字幕正常(TCC 链路,授权引导如出现按 guide 走);⑤「停止」→ 主窗口任务列表出现新 job;⑥Dock 无残留窗口。

---

## 执行顺序与外部依赖

Task 1→2→3→4→5→6 严格串行(接口依赖);7 依赖 4(可在 5 进行中并行做);8 依赖 4、可与 7 并行;9 依赖 5/8;10 最后且依赖 UI session。Task 1 闸门失败则全计划暂停回 spec。
