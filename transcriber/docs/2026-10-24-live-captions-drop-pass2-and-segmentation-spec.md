# 实时字幕摘除 Pass2 与断句修复 spec(2026-10-24)

> v2(2026-10-24 晚,/plan-eng-review 评审后修订):折入评审发现——合并批(用户拍板 3B)、
> 前端兜底补齐(1A)、在飞排空竞态修复、说话人标签剥离语义、验收口径与新增测试;
> 默认项(待用户确认)见 §7 决策 9~13。

## 1. 意图与背景

两个问题(用户 2026-10-24 提出):

1. **断句碎片化**:Pass1 一个词或几个词就断句。根因已实证:`live_pass1.py`
   钉的 `RULE3 = 1.0` 误读了 sherpa-onnx 的 rule3 语义——它是「说话时长超过
   该值**强制断句**,不管静音」的句长上限(官方默认 20s),不是「rule2 生效
   所需最小句长」(代码注释按 icefall 旧语义误读)。等于每说满 1 秒切一句。
   实测(15.7s 连续合成中文,`say -v Tingting`):当前参数切 **13 条碎片**
   (每条约 1.2s);改官方默认(rule2=1.2/rule3=20)得 **1 条整句**。
2. **Pass2 whisper 窗口重转低效**:每 20~35s 占一次 GPU(与离线 job 经
   `GPU_LOCK` 互斥排队),修正/翻译要等窗口攒满,字幕滞后 25s+。

用户决策(2026-10-24,三问三答):**方案 A 摘除 Pass2**,Pass1 整句直接喂
LLM 修正+翻译;**保留实时分人**(Pass1 final 切片喂 TitaNet 在线聚类);
**Pass2 代码彻底删除**(不留开关回退)。评审 D1 拍板:**分两步提交**
(①断句参数独立先行;②Pass2 摘除)。

这是对 2026-10-23 设计 spec §4「LLM 放 whisper 之后」决策的主动推翻:实时层
文本质量从「whisper 重转」降为「zipformer + LLM 软修正」,换来 GPU 零占用与
句级翻译时延;**终稿质量不受影响**(转正仍走离线全量 whisper + diarization,
`live_finalize` 对 pass2 零依赖,已核实)。

## 2. 目标 / 非目标

**目标**
- 断句恢复整句粒度(自然停顿 >1.2s 断句;连续说话最多 20s 强制切);
- Pass1 final → LLM 直连:修正+翻译在句尾停顿后数秒到达(验收口径见 §5.6);
- 实时说话人标签保留:`pass1_final` 事件带簇号,f1 行即着色;
- GPU 让给离线任务:实时会话期间同时提交大 job 不再互相排队;
- 转正(job 创建、离线管线、终稿质量)零变化。

**非目标**
- 不动 WS 协议(start/resume/stop/audio 二进制帧形状;pass2/pass2_skip 下行事件消失属服务端内部裁剪);
- 不动 Pass1 模型选择、喂入路径、jsonl 落盘;
- 不做 Pass2 开关回退(用户已裁定删除);
- 不动离线 job 管线与 `GPU_LOCK` 本身(离线 job 间互斥仍需要)。

## 3. 关键事实(2026-10-24 实测/查证)

- **endpoint 语义**(已装 sherpa-onnx 2.0.0 的 `EndpointConfig` 文档):rule1=
  尾静音超时(默认 2.4s,即使没说过话);rule2=说过话后尾静音超时(默认
  **1.2**);rule3=句长上限强制切(默认 **20**)。现值 2.4/0.6/**1.0** 中
  rule2 偏短、rule3 致命。
- **TitaNet 提取开销**:RTF 0.019(16s 音频 0.29s,4 线程 CPU)——内联在
  pass1 worker 的 `_on_final` 里做分人可行,每句一次、百毫秒级。
- **分人粒度**:Task 1 标定 ~1.2s 句粒度声纹不稳、3.6-6s+ 良好
  (`live_speaker.py` docstring);断句修好后 final 恰为整句粒度(通常 5~20s,
  按 CHUNK_S=8s 分块),与既有标定粒度同区——**该主张由 §5.4 双声 e2e 验证**。
  θ=0.40/CHUNK_S=8s 沿用不动。
- **磁盘可见性不变量**:`live_wav.AppendWav.append` 每次 write 后显式
  `flush()`(live_wav.py:44),且 `_run_writer` 先落盘再喂引擎——`_on_final`
  时刻 final 的 [t0,t1] 字节必已在盘,`read_wav_slice` 读得到。
- `refine_worker` 现只吃 `pass2` 事件(字段 t0/t1/text/speaker);
  `pass1_final` 事件字段是 t0_ms/t1_ms。
- 前端 `makeEntryEl` 对所有 kind 都画说话人圆点/名字,但文本着色与字幕条
  兜底主行只认 p2/llm(`live.js:563`、`live.js:599`)。
- `live_finalize.py` 不依赖 pass2 任何产物;`app.py` 的 GPU_LOCK 注释提到
  pass2,需同步改注释(锁保留)。

## 4. 方案

管线总览(改后):

```
WS 二进制帧(s16le/16k/mono)
   │ live-writer 线程:AppendWav.append(每批 flush)→ float32 → Pass1Engine.feed
   ▼
Pass1 worker 线程(sherpa-onnx 流式)
   ├─ 解码出字 → on_partial ──→ WS: partial(活动行,灰)
   └─ endpoint 断句(2.4/1.2/20)→ on_final:
        ① live_pass1.jsonl 落盘
        ② read_wav_slice[t0,t1) → OnlineSpeakerCluster.assign → speaker
           (try/except 全包:失败不带 speaker 键)
        ③ WS: pass1_final{text,t0_ms,t1_ms,speaker?}
        ④ llm_q 投递(refiner 存在时)
   ▼
live-llm worker 线程(串行)
   ├─ 合并批:相邻 final 间隔 ≤2.5s 并批(§4.3)
   ├─ LLMRefiner.refine(逐行「说话人N:句子」,prompt 硬约束)
   └─ WS: llm{t0,t1,text(剥标签),translation,speaker|null}
停止(handle_stop → live-finalize 线程):
   排空音频 → engine.stop(尾部冲刷出尾句 final,同走①~④)
   → LLM 排空(等「队列空且不在飞」,≤30s)→ wav finalize → 转正建 job
   → WS: stopped{job_id}
```

### 4.1 断句参数(live_pass1.py)

```python
RULE1 = 2.4   # rule1:尾部静音超过即断句(官方默认)
RULE2 = 1.2   # rule2:说过话后尾部静音超过即断句(官方默认;原 0.6 偏碎)
RULE3 = 20.0  # rule3:句长上限,超过强制断句(官方默认;原 1.0 = 每秒强切,断句碎片化根因)
```

注释更正语义(原注释的 icefall 误读删掉),注明实测依据与「标定量可微调」。

### 4.2 会话接线(live_session.py)

- **删**:`_run_pass2`、`_silence_ms`、`_on_pass2_event`、Pass2Runner 接线、
  `Pass2Cfg`/`plan_windows` import、`_cursor_ms`、`_pass2_stopping`/
  `_pass2_done`、`PASS2_TICK_S` 常量、构造注入点 `pass2_cfg`。
- **`_on_final` 扩展**(仍单线程 pass1 worker;docstring 按上述 ①~④ 步骤清单
  重写,注明「writer 先落盘后喂引擎」不变量):
  1. jsonl 落盘(不变);
  2. **分人**:`read_wav_slice(live_audio.wav, t0, t1)` → `cluster.assign(pcm)`
     → 簇号;整体 try/except,失败不带 speaker 键(前端回落通用色);
  3. emit `{'type':'pass1_final', text, t0_ms, t1_ms, speaker}`(原事件形状
     加可选 speaker);
  4. `self._llm_q.put(同事件)`(refiner 存在时;原由 pass2 驱动回调投递)。
- **`_finish` 转正顺序简化**:q STOP → writer join → `engine.stop()` →
  `_drain_llm()` → wav.finalize → finalizer(删 pass2 stopping/done 两步)。
  **`_drain_llm` 修在飞竞态**(评审折入):改为等「队列空 **且** worker 不在飞」
  ——worker 取走尾句后队列即空,但 refine 还要跑最多 ~18s,原实现会立即收尾,
  尾句的 llm 事件在 stopped 之后被丢;worker 侧加在飞标志(取事件置位、emit 后
  复位),deadline 30s 不变。
- `OnlineSpeakerCluster` 构造保留(分人唯一消费方从 Pass2Runner 变 _on_final,
  单线程调用约定不变)。

### 4.3 LLM 层(live_llm.py)

- `refine_worker`:事件过滤 `type == 'pass1_final'`;t0/t1 取 `t0_ms`/`t1_ms`。
- **合并批(用户拍板 3B)**:
  - 取得首事件后,队列非空则零等待贪心抽取;队列为空则最多等 `MERGE_WAIT_S`
    (1.5s;等待窗与间隔阈值 `MERGE_GAP_MS=2500` 解耦,保住 §5.6 的 ≤5s 参考口径;
    实施裁决 SDD ruling 4)攒批;
  - 连续事件满足 `t0_{n+1} - t1_n ≤ MERGE_GAP_MS`(2500ms)则并入同批;
  - 批上限 `MERGE_MAX_ITEMS=6` 条或跨度 30s,到达即出批;
  - 批内逐行拼「说话人N:句子」(带各自 speaker 标签)送 refine;corrected
    按指令保留行首标签。
- **说话人标签剥离与 llm 事件语义**(评审折入,修「chips+正文前缀双重显示」):
  - 批内**只有一个**说话人:corrected 剥掉已知行首标签(防御式 startswith
    判断)后以 `\n` 连接,`speaker` 字段=该簇号;
  - 批内**跨**说话人:保留全部行标签在文本里,`speaker` 置 null(前端通用色,
    逐行标签可读);
  - translation 为整批整体译文(prompt 已支持多行)。
- **在飞标志**:worker 取事件置位、emit 后复位(供 `_drain_llm` 判「不在飞」)。
- prompt 增一句:「输入文本可能没有标点,请在 corrected 中补全标点」
  (zipformer 输出无标点;规则 1 已含标点修正,此为显式化)。
- 熔断/降级/滚动上下文/超时参数(8s 超时、重试 1 次、退避 2s)全部不变。

### 4.4 live_pass2.py 处置(彻底删除)

- 删 `Pass2Runner`、`plan_windows`、`Pass2Cfg`、`WindowPlan`、`TAIL_CHARS` 等
  窗口调度与 whisper 重转代码;
- **留** `read_wav_slice` + `_wav_data_span`(4.2 分人切片用),docstring 更新
  为「Pass1 final 时间戳切片读取工具」;文件保留原名(减小 churn,名字与旧
  职责的出入在 docstring 说明);
- `tests/test_live_pass2.py`:删 plan_windows 决策表与 Runner 测试;
  read_wav_slice 的用例(若有)迁入 `tests/test_live_session.py`(分人切片
  测试天然覆盖同一函数)。

### 4.5 前端(live.js / live.html)

- `pass2` / `pass2_skip` case 分支删除(服务端不再发);`.e.p2`、`.e.skip`
  的 CSS 随之清理;
- `pass1_final` case 透传 `speaker: ev.speaker`;
- `makeEntryEl` 文本着色条件加入 `'f1'`(有 speaker 时立即着色,内联色覆盖
  `.e.f1` 的灰色);
- **字幕条兜底主行补齐**(评审 1A):`renderBar` 兜底分支透传 `show.speaker`
  (圆点+名字+着色);`.bmain.fallback` 的永久降亮仅保留给「无 speaker」的
  条目——无 key 会话整场不再灰暗;
- 「修正」徽标 `bdgFix` 从 live.html 删节点与 CSS、live.js 删 badge 调用
  (两级:识别 → 翻译);
- live.html:270 说明文案改为两级口径:「识别与说话人即时上屏;修正与翻译在
  句尾停顿后数秒自动替换」;
- 桌面端壳(main.js/live-window.js)零改动,前端由服务端伺服,刷新即生效,
  **无需重打包**。

### 4.6 文档同步

- `AGENTS.md` 实时字幕段:管线描述从三层改两层,注明本 spec;
- app.py `GPU_LOCK` 与 `run_whisper` 处注释去掉 pass2 字样(锁本身保留)。

## 5. 验收标准

1. **断句回归钉子**:`test_live_pass1` 断言 `RULE1/RULE2/RULE3 == 2.4/1.2/20.0`
   (防止再被「优化」回 1.0);15.7s 合成语音回放 → 1 条整句 final(或按
   1.2s 停顿合理分句),非 13 条碎片。
2. **单测**:
   - `test_live_session`:pass2/pass2_skip 事件绝迹(会话全事件流断言);
     `_on_final` 分人(`_StubCluster` + 真字节落盘,断言切片内容与 speaker);
     分人异常静默(无 speaker 键);无 pass2 的 `_finish` 生命周期;停止时
     尾句 final 进 llm 队列;setUp 不再需要 fake whisper;
   - `test_live_llm`:worker 吃 pass1_final(t0_ms/t1_ms 映射);合并批 ×5
     (间隔 ≤/> 阈值、队空等待出批、批上限、跨说话人批、同说话人多句批剥标签);
     **drain 竞态 ×2**(慢 refiner 下,停止后尾句 llm 事件仍先于 stopped 发出;
     在飞标志复位);prompt 契约含补标点句;
   - **session 级 LLM 接线集成**:patch `LLMRefiner` 为假件,喂真实音频字节,
     断言 llm 事件文本已剥标签、speaker 正确、seq 单调;
   - `live_page_selftest`:静态钉子更新(bdgFix/b-fix 不存在、f1 着色条件、
     兜底 speaker 透传、两级文案);协议段不变(本就只走 p1 路径)。
3. **e2e(真实服务)**:`say` 双句音频 → partial → pass1_final(带 speaker)→
   llm(修正含标点 + 译文),句尾停顿后及时到达(§5.6 口径);stop →
   stopped 带 job_id,终稿 whisper 全文与分人正常(转正零回归)。
4. **双声交替 e2e**(评审折入):`say` 两种声音交替合成 ≥6 段喂会话,断言
   分人标签交替正确、无互相吞并(前 2 分钟冷启动不计);同时验证「整句粒度
   声纹足够稳」的主张。
5. **无 key 会话手工清单**(评审默认,待确认):开一场无 key 会话,检查五项——
   「未配置翻译密钥」提示、f1 主层级不灰暗、说话人着色、停止正常、终稿正常。
6. **时延口径**(评审折入):「句尾停顿后 ≤5s 到达」指**空队列、首响应成功、
   无重试**的口径;积压/重试时按串行队列线性顺延,不作为验收失败项。
7. 中英/日文组合回归:zh>en、ja>zh 各跑一遍(模型选择不受影响)。

## 6. 风险与对策

| 风险 | 对策 |
| --- | --- |
| zipformer 同音字错误对 LLM 是高歧义输入,幻觉修正风险(原设计 §4 反对此直连的理由,本次主动推翻) | prompt 硬约束(只修 ASR 类错误不改意思)+ 温度 0 + LLM 只做软更新 + 终稿离线 whisper 兜底;e2e 抽检 |
| LLM 调用量 2~3×(每句一次,约 300~400 次/小时,原约 100~180 次);M3.1-Flash 思考 token(实测 392~951/次)是**不随输入变短的固定开销**,token 总量 ≈2×(非「略增」);绝对成本每小时 <¥1 量级 | 合并批(3B)降 30~50% 调用量;真实会议后按数据决定是否再调;串行+超时+熔断已有 |
| LLM 队列无界,最坏单件 ≈18s(8s 超时+2s 退避+8s 重试);「慢而成功」区间滞后拉长但有界可自愈(生产 ≈ 每 10~20s 一批 < 消费 3~5s/批);持续失败由熔断兜底;停止排空 30s 上限内未完成项丢软修正(修在飞竞态后仅剩真超时项,终稿不受影响) | 评审默认 4A:接受,数学记录在此;真实使用后如滞后可感知再上水位丢弃 |
| `_on_final` 内联分人阻塞 pass1 worker(每句 ~0.1-0.3s,实测 RTF 0.019) | 音频队列天然缓冲;若实测 partial 卡顿可感知,后备:分人移到 emit 之后、speaker 仅随 llm 事件 |
| final 的 t0 含 ≤2.4s 前导静音(rule1 空文本 endpoint 推进 _utt_start_ms 的机制),混入声纹切片 | 评审默认 5A:接受并记录;聚类是软标签、TitaNet 对少量静音鲁棒;裁静音需能量 VAD,不值得 |
| 聚类 warmup 期(前 3 块)簇可能合并重编号,已下发的 speaker 标签不回改(既有行为) | 软标签设计:实时标签不落 transcript.json,终稿 diarization 兜底;双声 e2e 验收观察误标率 |
| 分人粒度主张(整句 ≈ whisper 段落粒度)未经双声实测 | §5.4 双声交替 e2e 闸门;不达标则调 CHUNK_S/θ 或回议 |
| rule 参数不适配真实会议语速 | 三个常量集中一处可调;验收 3 用真实节奏校验 |
| 旧缓存页面收到新事件序列 | 事件只有减少没有新增形状;旧页面 pass2 分支收不到事件即死码,无害 |

## 7. 决策记录

1. 方案 A(摘除 Pass2)+ 保留实时分人 + 彻底删除 Pass2 代码:用户 2026-10-24
   三问三答拍板。
2. 推翻 2026-10-23 设计 spec §4「LLM 放 whisper 之后」——实时层质量换效率与
   时延,终稿质量为安全网(用户明示以效率为先)。
3. 断句参数取 sherpa-onnx 官方默认(2.4/1.2/20),不自行发明中间值:实测已
   证明恢复正常断句,后续按真实会议微调。
4. 分人内联在 `_on_final`(emit 之前),speaker 随 pass1_final 即时下发:
   TitaNet 实测开销百毫秒级,换取无 key/熔断会话也有分人。
5. live_pass2.py 文件保留(只留 read_wav_slice/_wav_data_span),不整文件删除:
   分人依赖这两个函数,重命名文件徒增 churn。
6. **分两步提交**(评审 D1,用户拍板):①断句参数+钉子测试独立提交;
   ②Pass2 摘除独立提交——增量交付、回滚粒度清晰。
7. **合并批 3B**(评审 D3,用户拍板,推翻 3A 推荐):相邻 final 间隔 ≤2.5s
   合并送修,降调用量 30~50%;设计见 §4.3。
8. **前端兜底补齐 1A**(评审 D2,用户拍板):renderBar 兜底 speaker 透传 +
   f1 主层级样式语义。
9. [默认已接受] 2026-10-24 用户下令实施,未逐条否决。队列背压 4A:接受现状,量化数字记 §6。
10. [默认已接受] 2026-10-24 用户下令实施,未逐条否决。声纹切片前导静音 5A:接受并记录。
11. [默认已接受] 2026-10-24 用户下令实施,未逐条否决。无 key 会话验证 5A:手工清单进验收(§5.5)。
12. [默认已接受] 2026-10-24 用户下令实施,未逐条否决。在飞排空竞态修复(外部声音张力 1):spec §4.2 已折入设计。
13. [默认已接受] 2026-10-24 用户下令实施,未逐条否决。说话人标签剥离语义(外部声音张力 3)+ 双声 e2e(张力 2)+
    时延口径(张力 4):已折入 §4.3/§5.4/§5.6。

## 8. 实施状态(2026-10-24)

分支 `live-drop-pass2` 按本 spec v2 实施:Task 1~6 完成,六场景 e2e 全绿
(S2 时延实测 7.3s:>5s 参考、<10s 硬线,后续可按 §6 调 `MERGE_WAIT_S`);
translation 纯净译文指令(不得包含说话人标签或行前缀)为实施期补充
(与 live_llm.py rule4 同源)。

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found | 4 findings(3 折入 spec:排空竞态/标签语义/双声 e2e;1 编辑性:时延口径) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_open | 11 issues, 1 critical gap(排空竞态,已折入 §4.2), 5 decisions defaulted |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | issues_open | score 7→9, 6 unresolved(前置 UI session 遗留) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CODEX:** 4 findings — 在飞排空竞态(真问题,spec§4.2 已修)、说话人标签与事件字段打架(真问题,spec§4.3 已定义剥离语义)、分人粒度缺双声实测(spec§5.4 已加闸门)、验收 5s 口径不明(spec§5.6 已改)。
- **CROSS-MODEL:** codex#1 与评审 D4 默认(4A 接受)直接冲突——codex 对:逐句化后竞态从偶发变必然,已按张力 1 修复折入;其余三条与评审发现互补,无冲突。
- **VERDICT:** ENG REVIEW COMPLETE — spec v2 已折入全部已拍板与默认决策;5 项默认决策待用户确认(§7 决策 9~13)后即可实施。

**UNRESOLVED DECISIONS:**
- D4 队列背压:默认 4A 接受+量化(spec§6),未获用户确认
- D5 无 key 会话验证方式:默认 5A 手工清单(spec§5.5),未获用户确认
- 张力 1 在飞排空竞态:默认采纳修复(spec§4.2),未获用户确认
- 张力 2 双声交替 e2e:默认采纳(spec§5.4),未获用户确认
- 张力 3 说话人标签剥离语义:默认采纳(spec§4.3),未获用户确认
- + 6 unresolved from prior reviews(plan-design-review 2026-10-24,前置 UI session)
