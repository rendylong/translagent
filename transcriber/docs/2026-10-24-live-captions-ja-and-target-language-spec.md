# 实时字幕日文支持与翻译目标语言选择 spec(2026-10-24)

## 1. 意图与背景

现状:实时字幕的语言是固定四个组合(zh>en / en>zh / zh>zh / en>en),识别语言与翻译
目标都只支持中英;pass1 流式模型 `models/streaming-zipformer-zh-en`(双语)写死在
app.py 的 `LIVE_MODEL_DIR`,与语言无关。

需求:
1. **识别支持日文**(src ∈ zh/en/ja);
2. **翻译目标语言可独立选择**(不翻译 / 中文 / 英文 / 日文),与识别语言解耦。

## 2. 目标 / 非目标

**目标**
- 开始表单拆成两个下拉:识别语言(中文/英文/日文)+ 翻译目标(不翻译/中文/英文/日文),
  任意组合;
- 日文会话:pass1 流式 partial 上屏、pass2 whisper 窗口精转、pass3 LLM 翻译,三层全通;
- 停止转正的 job `language='ja'`,离线管线(已支持 ja)照常出完整文稿。

**非目标**
- 不做识别语言「自动」(pass1 流式模型无法多语切换;whisper auto 仅 pass2 层,不提供);
- 不替换中英 pass1 模型(bench 已钉,中文质量优先);
- 不改离线上传管线的语言集合(本就含 ja);
- 不动 WS 协议:`start.language` 仍是 `'src>tgt'` 字符串,旧页面缓存的旧值继续合法。

## 3. 关键事实与约束(2026-10-24 实测/查证)

- **日文流式模型只有一条路**:sherpa-onnx releases 的流式 transducer 中含日文的仅
  `sherpa-onnx-streaming-zipformer-ar_en_id_ja_ru_th_vi_zh-2025-02-10.tar.bz2`
  (8 语种多语言,247MB;其余 ja 模型均为离线)。解压后文件名与中英模型不同
  (非 `*-epoch-99-avg-1.onnx`)→ pass1 需按目录内容解析文件名。
- pass2 whisper large-v3-turbo 原生支持 `-l ja`;`PROMPTS` 无 ja 条目 → 不注入提示
  (与 en 一致,whisper 自身日文标点/假名汉字平衡可接受)。
- pass3 `LLMRefiner._LANG_NAME` 只有 zh/en;翻译指令是「translation 输出%s译文」——
  src==tgt(不翻译)时也在请求译文,浪费 token 且译文≈原文,应明确指示输出空串。
- `live_meta.language` 只存 src;tgt 不落盘(转正 job 只需识别语言;断线恢复场景下
  服务必然重启过,会话已死,tgt 无从恢复也无须)。
- 无 MiniMax key 时 Pass3 整层禁用(现状),不因 tgt 变化。
- 单 worker 单会话架构:同一时刻只有一个 pass1 模型在内存,ja 与 zh-en 模型不会并存。

## 4. 方案

### 4.1 模型与目录

- 新增 `models/streaming-zipformer-ja/`(多语言流式模型解压于此,目录名与 zh-en 并列)。
- `app.py`:`LIVE_MODEL_DIR`(中英)保留;新增 `LIVE_JA_MODEL_DIR`;
  `ws_live` 的 start 分支按 `start.language` 的 src 选目录传给 `LiveSession`
  (src=='ja' → ja 目录,否则 zh-en 目录)。目录/模型缺失 → `Pass1Engine` 构造抛错 →
  走现有「无法开始实时会话:…」显式 error 路径(文案净化保留),前端停止重连并显示原因。

### 4.2 pass1 模型文件解析(live_pass1.py)

`Pass1Engine` 在模型目录内按 glob 解析:
- `encoder-*.onnx` / `decoder-*.onnx` / `joiner-*.onnx`,排除 `*.int8.onnx`,多匹配取字典序首个;
- `tokens.txt`;
- glob 无匹配时回落现硬编码名(`encoder-epoch-99-avg-1.onnx` 等)——兼容既有中英目录,
 也让「目录存在但文件名意外」获得与今天相同的报错路径。

### 4.3 语言对解析(live_session.py)

`_parse_lang`:src 合法集 {zh, en, ja};tgt 合法集 {zh, en, ja};非法仍回落 ('zh','en')。
`LLMRefiner(key, lang_pair=(src, tgt))` 接线不变。

### 4.4 LLM 不翻译分支(live_llm.py)

`_LANG_NAME` 增 `ja: '日本語'`。`_build_messages`:src==tgt 时规则 4 改为
「translation 输出空字符串 ""(源语与目标语相同,无需翻译)」——省 token、避免同语种
重复上屏;其余语种照旧。

### 4.5 前端(live.html / live.js)

- 单个 `fLang` 下拉拆成两个:
  - `fSrc` 识别语言:中文 / 英文 / 日文;
  - `fTgt` 翻译目标:不翻译 / 中文 / 英文 / 日文;
- 序列化仍为 `'src>tgt'`;「不翻译」序列化为 tgt==src(与旧 `zh>zh` 语义一致);
- `localStorage['live.lang']` 兼容旧值:旧四选的 src/tgt 均在合法集内,读入时映射到
  两个下拉(不在合法集的回落 zh>en);
- 会话信息卡 `spLang` 显示「中文 → 日文」/「日文(不翻译)」式的人类可读对;
- 状态镜像(badge/字幕条)逻辑与语言无关,不动。

### 4.6 WS 协议与会话恢复

- 协议零改动(见 §2 非目标);`resume` 不带 language,沿用会话内存态,不受影响。

## 5. 验收标准

1. **单元**:`_parse_lang` 对 ja 全组合(含非法回落);LLM src==tgt 的 prompt 含「空字符串」
   指令、跨语种 prompt 含「日本語」;pass1 glob 解析(多匹配取首个、无匹配回落硬编码)。
2. **静态钉子**:live.html 有 `fSrc`/`fTgt` 与「日文」选项;live.js 序列化 `'>'` 拼接。
3. **e2e(真实服务)**:say 合成日文语音 → WS 会话 `ja>zh` start → 收到日文 partial/
   pass1_final;停止 → stopped 带 job_id 且 job `language='ja'` → whisper 完整文稿为
   正确日文。
4. 中英会话回归:`zh>en` 照旧(模型选择不回归)。
5. 桌面客户端重新打包,自检不回归。

## 6. 风险与对策

- **多语言模型日文质量**:partial 层可能有错字,pass2 whisper 每窗重转兜底,最终文稿
  不受 pass1 影响;e2e 验收看 final 质量。
- **文件名解析**:多语言模型若非 transducer(无 decoder/joiner)则 from_transducer 不可用
  ——下载后先验形状再接线;glob 回落硬编码保证中英路径零行为变化。
- **旧缓存页面**:旧 live.html 发旧四选值,服务端全部合法兼容,无断代。
