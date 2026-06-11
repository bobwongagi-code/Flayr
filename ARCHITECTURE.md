# Flayr 技术架构设计

> 当前架构基准：2026-05-31（全模态两阶段）。本文区分当前 report-first 分析产品和未来 improved.mp4 生产系统。

---

## 0. 契约（spec §0）

> 本节是系统的**唯一真相源契约**（2026-06-10 落定）：只描述实际存在的东西，不写愿景。
> 各文档（framework / ANALYSIS-PROMPT / QA-RULES）对本节内容只引用不复制；冲突时以本节为准。
> 任何"要不要加 X"的提案先对照本节自查，避免基于过时结构或臆想决策。

### 0.1 素材包清单（每条视频，`process_video` 产出）

| 产物 | 生成者 | 可选 | 失败降级 |
|---|---|---|---|
| `frames/`（全片 1fps） | ffmpeg | 必需 | 缺 ffmpeg 时记 error，主流程继续 |
| `focus_frames/`（首尾 5s 各 2fps，Hook/CTA） | ffmpeg | 必需 | 同上 |
| `audio.wav`（混音，**不分轨**——人声分离已评估不采纳） | ffmpeg | 必需 | 同上 |
| `transcript.txt / .srt / .zh.txt` | whisper-cli（泰语自动切 th 专用模型，缺则回退通用） | 必需 | 占位文本，主流程继续 |
| `shot_track.json`（自适应镜头边界） | ffmpeg 场景检测 | 默认开（零成本） | 状态标记，缺失显示占位 |
| `subtitle_track.json`（OCR 权威字幕轨） | qwen-vl-ocr，auto 策略（有 DashScope key 即开） | 可选 | 状态标记，缺失显示占位 |
| `_preprocess.json`（复用缓存） | flayr.py | 自动 | `--reuse-preprocessing` 命中即跳过重抽 |

### 0.2 两阶段架构 + Phase C

- **阶段一（事实）**：omni 每视频一次，吃原生视频（fps3 重编码+完整音轨），产出 `video_facts_{role}.json` 的
  `evidence_units[]`。事实一旦产出即**锁定**（阶段二/Phase C 不得增删改）。
- **阶段二（判断）**：锁定 facts + analysis_input.md（产品信息/三层指引/结构库/QA/schema + 字幕轨/镜头轨）
  单次调用完成 S1-S6 对比、improvements、key_conclusions。
- **Phase C（回看）**：模型自报 ∪ 代码确定性检测（占位证据/visual_only + medium/large），≤2 阶段、
  仅一次；切对应阶段原生片段（含音轨）重判，整对象替换后重跑后处理链。规范见 0.6。
- **后处理链**：validate（阻断→repair 重试）/ repair（确定性修补）/ qa_warnings（软警告）。

### 0.3 字段唯一真相源

`references/analysis-output-schema.json`。evidence_unit 含多模态事实字段 + 结构化标记
（`product_visible` / `product_coverage` / `third_party_endorsement`）；标记由模型按定义判，
代码只做确定性消费（占比累加、归属搬运、severity 一致性），不得用正则重新推断语义。

### 0.4 severity 判定宪法（2026-06-11 修订，4d 架构）

> **模型供事实，代码定政策。** 模型逐阶段输出稳定事实：两侧独立执行分
> （creator/benchmark_execution，0=不执行/0.5=敷衍或无法有效接收/1=合格/2=出色，
> 先打分再对比）、painpoint_relevance（痛点命中四值枚举）、category_profile（品类画像）；
> severity 由 `postprocess/derive.py` 确定性推导：E = 标杆执行分 − 达人执行分，
> S = E × W（品类原型×阶段权重表）× C（痛点命中系数，S6 促单与痛点正交不参与调制），
> 含 S5 背书门槛 / S4 演示差分 / S1 痛点差分三个事实覆盖与 S1/S6 缺失红线、
> 极性红线（达人持平或更优 → small），逐阶段写 severity_derivation 算法溯源。
> 权重表数值随对比数据 + 人工裁决积累，对存量 facts 零 LLM 成本离线重拟合。

修订背景：原宪法"商业判断归模型（prompt 框架）"经 r1-r2 两轮实测不成立（prompt 调 severity
不收敛，11/18→9/18 修一伤二）；r4 实弹 15/18 + severe 0 过 T4 预注册线，同批模型直判
12/18 + severe 2（划算感误归背书、极性 bug 第四轮复发，均被推导层机械纠正）。

stabilize 残余职责：一致性修复（severity↔task_completion↔gap 文本矛盾收敛）、归属搬运、
以及执行分缺失时（旧数据/降级路径）的薄兜底。推导失败必须优雅降级保留模型 severity，
绝不拖垮主流程。S3/S4 牙膏品类正则已按 TODO #1 处置清单删除（门禁过线后执行）。
推论：`task_completion=partial` 档代码不替模型定级；禁止新增"partial→medium"类映射；
禁止再用 prompt 判例校准 severity（校准动作 = 调权重表 + 离线重放）。

### 0.5 第三方背书定义（`third_party_endorsement` 的判定规格）

机构类型 + 关联性门槛**同时成立**才为 true：
- 机构类型：监管/认证机构（KKM、Halal、SIRIM、TISI…）、行业协会、第三方评测中心/实验室、
  高校与研究机构、三方调研咨询公司、疾病·医院·防治中心。
- 关联性门槛：该机构的实验/数据/研究**在证明本产品价值**。

判例：大学检测报告证明本品杀菌率 → true；画面出现 KKM 认证标号 → true；
仅提机构名/赞助商/合作 logo（无证明本品价值的数据） → false；达人自称"用了三年" → false（自述）；
用户评论截图 → false（社会证明，属 S5 信任内容但非机构背书）。

### 0.6 Phase C 输入/输出规范

- 输入：目标阶段的标杆+达人原生切片（fps3、480px、含音轨），时间窗 = 阶段 time_range **±2s 缓冲**；
  prompt 必须告知"切片边界可能有误差，按功能归属判断，勿把相邻阶段内容算进本阶段"。
- 输出：完整 stage 对象整体替换；引用口播必须能对上切片音频/转写，听不清标 voice_only 并写明，
  **禁止推断补全未听清的话术**（kakwan S6 幻觉教训）；回看 prompt 不得含方向性压力
  （如"持平必须给 small"——已删）。
- 合并后重跑全套校验；facts 不可改。

### 0.7 验收与回归原则

- 回归集 = **输入视频 + `references/ground-truth-labels.md` 人工标签**；绝不 diff runs/ 存档输出。
- stabilize 重构的验收：4 样本（3 新 + 牙膏）× 预注册阈值（开跑前写死）× 标签一致率。
  QA-RULES §10 旧 PASS 定义围绕旧 stabilize + 单一牙膏 run 校准，重构后须按此重写。
- 框架判例五条（省钱前提 / CTA 过度=硬推 / S1-S6 位置先验 / 弱 CTA≠有效 / 镜头语言）
  见 `references/ground-truth-labels.md` 末节，已写入对应指引文档。

---

## 1. 产品阶段

### 当前阶段：Insight / Report MVP

当前 Flayr 的核心交付不是自动成片，而是给运营团队一份可复核的分析报告：

```
爆款视频 + 达人视频
  → 视频解析、抽帧、抽音频、转写、中文翻译
  → 阶段一：全模态 LLM（omni）原生视频各跑一次，建立单视频事实清单（含画面/口播/字幕/音频事实）
  → 阶段二：对比判断，喂 facts 文字 + 每条 evidence 的关键帧 + 切片音频，按 S1-S6 横向对比
  → Phase C：仅当模型声明 low_confidence_stages 时，对对应阶段切原生视频片段回看一次并重判
  → 提案样片模块：Top 提升点切达人原片 3-5 秒，打包本地话术和改造理由
  → report.html + analysis.json + improved_video_plan.json + proposal_clips.json
```

核心理念：**全模态主导 + 事实判断分离**（详见 3.6）。模型用 omni 同时"看连续画面 + 听音轨"，
自定位变化点；阶段一锁定事实防止两条视频串证据，阶段二在事实基线上重获感官做判断。

当前阶段的重点：

- 识别东南亚本地语言口播，并输出中文翻译。
- 两级运行：仅有视频时完成“视频证据分析”；补充品类、价格带、核心卖点、目标用户/痛点与购买动机后升级为“策略增强分析”。
- 严格按 `ANALYSIS-PROMPT.md`、商业评判框架和目标市场知识库执行：先做整片感知判断，再按 `structure_library_full.md` 完成槽位/模块识别和证据归因，最后输出对标差距与 GMV 优先改造。
- 基于 `structure_library_full.md` 的官方模块编号与适配规则对事实做阶段归因，而不是先套阶段再找素材。
- 阶段时间不是硬切片，必须由模型先整体理解视频后填写真实 `time_range`。
- 报告结论必须引用 `evidence_units`，并由该事实时间段抽取对应视频帧。
- 输出改进建议和画面方向，但不假装已经生成 `improved.mp4`。

### 下一阶段：Production / improved.mp4

未来再进入自动生产链路：

```
analysis.json / improvements.json
  → timeline.json
  → TTS 音频
  → 字幕、标注、片段调速/替换
  → improved.mp4
```

这需要更强的 schema、TTS、timeline、compose 能力。当前不应把这些能力硬塞进主链路。

---

## 2. 当前代码结构

```text
scripts/
├── flayr.py                          # skill harness: CLI、依赖检测、校验、流程编排
└── flayr_core/
    ├── artifacts.py                  # manifest 读取、帧候选、按时间段选帧
    ├── llm/                          # LLM 调用包（按职责拆 5 个子模块）
    │   ├── __init__.py
    │   ├── api.py                    # HTTP 调用底层 + Keychain
    │   ├── parse.py                  # JSON 解析 + schema normalize + STAGES + 共享工具
    │   ├── payload.py                # build_*_payload 系列请求构造
    │   └── pipeline.py               # merge / parse_and_validate / run_large_model_analysis
    ├── postprocess/                  # 分析结果修补与校验包（按职责拆 6 个子模块）
    │   ├── __init__.py               # 仅 re-export apply_postprocess_chain
    │   ├── utils.py                  # 通用工具（SRT / evidence_unit / 时间关系）
    │   ├── repair.py                 # 修补 result data（align / bind / reconcile / ground / fill / …）
    │   ├── validate.py               # 通用校验，会抛 SystemExit 触发 repair 重跑
    │   ├── claims_my.py              # 马来西亚 KKM/认证主张专项
    │   ├── health_rewrite.py         # 健康品类合规重写专项
    │   └── chain.py                  # apply_postprocess_chain 流水线编排
    ├── prompt.py                     # analysis_input.md 装配（LLM 输入包）
    ├── proposal_clip.py              # Top 提升点提案样片结构化 + 达人原片切片
    ├── proposal_video.py             # DashScope/Wan 提案样片生成 adapter
    ├── report.py                     # HTML 报告渲染
    ├── translation.py                # 本地语言转中文（调用 llm.api）
    ├── utils.py                      # 通用文件/进程 helper（read_optional_text、write_json 等）
    ├── video.py                      # ffmpeg/ffprobe、抽帧、音频、manifest 写入
    └── whisper.py                    # Whisper 转写和语言检测
```

### 当前覆盖

| 能力 | 模块 | 状态 |
|------|------|------|
| CLI / 依赖检测 / 校验 / 流程编排 | `flayr.py` | 已覆盖 |
| 视频时长、抽帧、音频提取 | `video.py` | 已覆盖 |
| frame/focus/stage manifest 读取和选帧 | `artifacts.py` | 已覆盖 |
| Whisper 转写和语言检测 | `whisper.py` | 已覆盖 |
| 中文翻译 | `translation.py` | 已覆盖 |
| LLM 请求构造 / 调用 / schema 解析 | `llm/` 包（api / payload / parse / pipeline） | 已覆盖 |
| 分析结果修补 / 校验 / 品类合规 | `postprocess/` 包 | 已覆盖 |
| analysis_input.md 装配 | `prompt.py` | 已覆盖 |
| 提案样片结构化 / 原片切片 | `proposal_clip.py` | 已覆盖 |
| DashScope/Wan AI 示意样片 adapter | `proposal_video.py` | 已覆盖 |
| HTML 报告 | `report.py` | 已覆盖 |

---

## 3. 模块边界

### 3.1 `flayr.py` — Skill Harness

职责：

- CLI 参数解析。
- 依赖检测。
- 输入校验。
- 创建 run directory。
- 串联 video / whisper / translation / prompt / llm / report。
- 装配 analysis dict、写出 `analysis.json`、`improved_video_plan.json` 和 `proposal_clips.json`。
- 计算分析等级和结论边界，并随分析输入、结构化结果与报告输出；缺少产品策略时不阻止事实分析，但限制策略结论。

约束：

- 不直接承担 LLM、报告渲染、抽帧、Whisper、翻译、prompt 装配等核心实现。
- 不写 `analysis_input.md`（已迁至 `prompt.py`），harness 只负责调用。
- 保持命令入口稳定：`python3 scripts/flayr.py ...`。

### 3.2 `video.py` — 输入侧视频处理

职责：

- 用 `ffprobe` 读取视频时长。
- 用 `ffmpeg` 抽取全片 1fps 帧。
- 抽取 Hook/CTA 加密帧。
- 生成 `frames/manifest.json`、`frames/stage_frames.json`、`focus_frames/manifest.json`。
- 提取 `audio.wav`。

约束：

- 只负责输入侧拆解，不负责报告、不负责 LLM、不负责合成视频。

### 3.3 `artifacts.py` — 产物读取和证据选取

职责：

- 统一读取 frame / focus frame / stage frame manifests。
- 在 manifest 缺失时从目录兜底恢复 frame entries。
- 按 `time_range` 选择最接近的证据帧。
- 提供 `sample_evenly`、帧排序、阶段代表帧构造等公共能力。

为什么需要这个模块：

- `llm.py` 和 `report.py` 都需要选帧，但不应该复制 manifest 读取逻辑。
- `video.py` 负责写 manifest，`artifacts.py` 负责读和选择 manifest。
- 它是分析侧和报告侧之间的稳定数据访问层。

### 3.4 `whisper.py` — 语音转写

职责：

- 优先使用 Whisper 内置语言检测。
- 适配 `whisper` / `whisper-cli` / `whisper-cpp`。
- 输出本地语言 `transcript.txt`。
- 输出短分段时间戳口播 `transcript.srt`，供阶段证据对齐使用。

约束：

- 不用英文式空格分词判断是否有有效口播。
- 东南亚语言如泰语、马来语、印尼语必须保留本地语言转写。
- 涉及口播归属到具体阶段时，以 `transcript.srt` 的时间范围为准，不根据文案相似度跨段引用。

### 3.5 `translation.py` — 中文翻译

职责：

- 维护 `transcript.zh.txt`。
- 使用 AirTranslate 相关电商翻译 prompt。
- 通过 `llm/api.py` 的底层 LLM 调用能力调用模型。

说明：

- `translation.py` 只 import `llm.api`（HTTP 调用层），不经过 `llm/` 包顶层；
  这避免了被动加载 payload / parse / pipeline 等业务规则模块。
- 翻译结果用于中国运营理解；口播节奏判断仍优先参考本地语言转写。

### 3.6 `llm/` 包 — 大模型分析

按职责拆为 5 个子模块，依赖单向：`api → payload / parse → pipeline`。下游（translation）只 import `llm.api`，不被动加载整套业务规则。

| 子模块 | 职责 |
|------|------|
| `llm/api.py` | HTTP 调用底层 + 三个 data URL 工具：`video_to_data_url`（原生视频 ffmpeg 重编码 fps=3+降分辨率含音轨，支持 start/duration 切片）/ `audio_to_mp3_data_url`（整条或按 start/duration 切片）/ `image_to_data_url`（关键帧）。不含业务规则。 |
| `llm/parse.py` | JSON 解析 + schema normalize。含 `STAGES` 常量、`is_effective_voiceover` 等基础工具，被 `postprocess` 复用。evidence_unit 含 `audio_fact` 字段（BGM/语气/音效）。 |
| `llm/payload.py` | `build_*_payload` 系列。阶段一 `build_video_fact_payload`（原生视频直传）；阶段二 `build_llm_comparison_payload` + `build_evidence_sensory_inputs`（每条 evidence 配关键帧+切片音频）；Phase C `build_stage_review_payload`（低置信阶段原生视频切片）。 |
| `llm/pipeline.py` | 主入口：`merge_analysis_result` / `parse_and_validate_llm_result` / `run_large_model_analysis` / `run_video_fact_extraction`。内部用 `_process_llm_result` 抽取主链与 repair 重试共享逻辑，并在第一遍成功后最多触发一次 Phase C 回看。 |

**两阶段架构（全模态主导）**：

| 阶段 | 函数 | 输入 | 产出 | 意图 |
|------|------|------|------|------|
| 一：事实抽取 | `run_video_fact_extraction` → `build_video_fact_payload` | 原生视频（fps=3+音轨），benchmark/creator 各一次 | 锁定的 `evidence_units`（唯一事实源） | omni 自定位变化点，像人一样看连续画面+听声音 |
| 二：对比判断 | `build_llm_comparison_payload` → `build_evidence_sensory_inputs` | facts 文字 + 每条 evidence 的关键帧 + 切片音频 | severity / key_conclusions / 改进 | 判断环节重获感官，按 S1-S6 功能阶段横向对比 |
| C：低置信回看 | `maybe_refine_low_confidence_stages` → `build_stage_review_payload` | 第一遍声明的 low_confidence_stages + 对应阶段原生视频片段 | 仅替换对应 `stage_analysis` | 解决代表帧信息不足导致的边界阶段漂移，硬限制 1 次 |

关键约束：阶段一 facts 一旦锁定即"唯一事实源"，阶段二感官素材仅辅助评估声画质感，
**不可新增或改写 facts**（冲突以 facts 为准，可标注"感知歧义"）；阶段二 temperature=0 保证可复现；
Phase C 只允许模型主动声明低置信后触发，最多回看 2 个阶段、最多 1 次，不做无限 agent loop；
ffmpeg 不可用时阶段一降级为"关键帧+完整音频"，不中断。
背景：早期 qwen-vl-max（抽帧式 VL）听不到音轨、对比判断只喂文字 facts，导致判断环节"看不见画面听不见声音"；
换 qwen3.5-omni-plus + 两阶段后根治。

职责：

- 构建多模态分析请求。
- 使用 Keychain/env 读取 API key。
- 调 OpenAI-compatible endpoint。
- 写出 `llm_request.json` / `llm_response.json`。
- 解析和修复 JSON。
- 规范化 `analysis_result.json`。
- 校验三步分析契约：整体感知、产品可见度/模块适配/闭环、证据支撑的对标改造（具体校验在 `postprocess/` 包）。
- 维护 `video_understanding.evidence_units -> stage_analysis.*_evidence_ids` 的证据绑定。

约束：

- Prompt 必须同时载入 `ANALYSIS-PROMPT.md`、商业评判框架、目标市场知识库与完整 `structure_library_full.md`，并先完成整片判断与事实清单再归因。
- 视频证据分析不得臆测真实卖点、人群适配、价格策略或最终 GMV 排序；策略增强分析才可结合已确认业务输入下完整成交判断。
- 每个阶段必须给出结构库官方模块编号、适配判断、任务完成度、差距类型和口播表现；缺少即判分析结果无效。
- `time_range` 必须是模型理解后的真实阶段时间，而不是机械照抄参考范围。
- 有效口播是信息核心，画面只证明实际可见内容；静音视频改以画面/字幕组织分析。
- 输出不合 schema 时允许 repair 一次，再失败就 fail loud。
- `llm/__init__.py` 不主动 re-export 子模块，下游必须显式 import 子模块路径。

### 3.7 `postprocess/` 包 — 分析结果修补与校验

按"职责性质"（修改 data vs 抛 SystemExit vs 市场专项 vs 品类专项）拆为 6 个子模块，依赖方向 `utils → repair / validate / claims_my / health_rewrite → chain`。

| 子模块 | 职责 | 行为语义 |
|------|------|--------|
| `postprocess/utils.py` | 通用工具：SRT 读取、evidence_unit 查找、时间关系。 | 纯函数 |
| `postprocess/repair.py` | 修补 result data：align / bind / reconcile / ground / fill / materialize / deduplicate / downgrade + 品牌型号清洗 + 时间归一。 | 修改 data 后正常返回 |
| `postprocess/validate.py` | 通用校验：evidence_alignment / analysis_dimensions / transcript_attribution / stage_ownership。 | **抛 SystemExit 触发 repair 重跑** |
| `postprocess/claims_my.py` | 马来西亚（MY）市场 KKM/kelulusan 认证主张专项。扩市场时新增 `claims_xx.py` 平级文件。 | 修改 data |
| `postprocess/health_rewrite.py` | 健康品类（维生素 / 营养补充 / 儿童牙膏）合规重写。含 2 个会抛 SystemExit 的 validate_*。扩品类时新增 `xx_rewrite.py` 平级文件。 | 修改 data + 抛 SystemExit |
| `postprocess/chain.py` | `apply_postprocess_chain`：两个 caller 共享的中段流水线。每步带模块来源注释。 | 编排 |

职责：

- 校验三步分析契约：整体感知、产品可见度/模块适配/闭环、证据支撑的对标改造。
- 校验口播归属：标杆与达人转写不得被模型交叉写入对方证据单元。
- 对有口播的阶段建立按阶段时间绑定的口播证据，无法从画面验证时显式标记 `voice_only`。
- 校验阶段顺序、证据时间对应关系和认证信息唯一归属。
- 对已能明确归属的信息做确定性归位：认证和产品卖点归产品引出、反馈归效果呈现、成分说明归信任放大、购买指令归 CTA；未发现证据的阶段必须显式标为空缺或待复核。

约束：

- 包级 `__init__.py` 只 re-export `apply_postprocess_chain`，其他函数显式 import 子模块路径。
- KKM 等认证信息不得在 Hook 出现，也不得跨阶段重复引用；与产品引出同段出现时归入 S2。
- 新增市场或品类专项一律新建平级文件，不修改 `claims_my.py` / `health_rewrite.py`，保持每个文件单一规则集。

### 3.8 `prompt.py` — analysis_input.md 装配

职责：

- 把 analysis dict + 关键帧 manifest + 转写 + 翻译 + 商业评判框架 + 目标市场知识库 + `structure_library_full.md` + `ANALYSIS-PROMPT.md` 装配成 LLM 输入包 `analysis_input.md`。
- 提供 `speech_status` / `read_analysis_prompt` / `render_*_markdown` 等 prompt 装配辅助。

说明：

- 从 `flayr.py` 拆出（解决 5.1 节标记的 risk）。
- 凝聚度：prompt 装配是独立子系统，与 harness 编排分离。
- 变更频率：prompt 内容每次 LLM 调优都要改；harness 几乎不动；二者频率差几个数量级。

### 3.9 `report.py` — HTML 报告

职责：

- 读取结构化 analysis 数据。
- 通过阶段引用的 `evidence_units` 时间段选取达人/标杆画面。
- 渲染 `report.html`。

当前报告原则：

- 报告顶部先展示分析等级和结论边界，再展示整体感知、产品可见度与闭环判断，不把阶段评分当作整体洞察。
- 阶段拆解和证据帧合并展示。
- 三列布局：差距 / 达人表现 / 标杆表现。
- 差距概览使用色块与中文等级表达，点击后阅读对应阶段证据。
- 每侧先展示核心信息，再展示口播证据、对应帧、画面证据和结论。
- 不展示技术附录。
- 不展示孤立的"全链路代表帧"区块。
- Top 提升点固定聚焦前三项，绑定标杆证据 ID 与达人基底证据 ID，同时展示方案 A（已有 AI 成图或可执行出图基底/指令）和方案 B（标杆对应镜头）；没有合适达人基底时明确要求补素材。
- Top 提升点展示目标槽位、GMV 影响和结构性/执行性/资源性差距类型，避免仅按视频时间顺序排列。

### 3.10 `proposal_clip.py` / `proposal_video.py` — 改进点提案样片

职责：

- 消费 `analysis["improvements"]` 的 Top3，而不是重新判断问题。
- 从达人原视频按提升点时间窗切 3-5 秒原片片段。
- 打包本地语言话术、中文解释、改造理由、AI prompt 和达人确认标记。
- 写出 `proposal_clips.json`，并让 `report.py` 在 Top 提升点中作为独立区块展示。
- 可选调用 `proposal_video.py` 的 DashScope/Wan adapter 生成 `proposal_*_ai.mp4`。

约束：

- 默认不调用 AIGC 后端；未配置时，报告显示达人原片切片 + 改造文案。
- `dashscope-i2v` 使用 Wan 图生视频接口，基于本地达人关键帧 data URL 生成 AI 示意样片。
- `dashscope-s2v` 使用 `wan2.2-s2v` 数字人接口，必须提供公网可访问的正脸图和台词音频 URL，并可先走 `wan2.2-s2v-detect`。
- AI 样片生成失败只降级该 unit，不阻塞报告和其他提升点。
- 单条样片默认 4 秒、最长 5 秒，Top3 总时长不超过 15 秒。
- AI 成图仅作构图与镜头执行参考；其中包装文字、认证、成分和价格信息不得作为分析证据，事实仍以原视频和可验证帧为准。

### 3.11 `utils.py`

职责：

- `run_command`
- JSON 写入
- 文本文件写入
- 可选文本读取（`read_optional_text` 被 `prompt.py` 和 `translation.py` 共用）

---

## 4. 当前数据流

```text
flayr.py
  ├─ video.py
  │   └─ 写 frames/audio/manifests
  ├─ whisper.py
  │   └─ 写 transcript.txt
  ├─ translation.py
  │   └─ 通过 llm.api 调模型，写 transcript.zh.txt
  ├─ prompt.py
  │   └─ 写 analysis_input.md
  ├─ llm/pipeline.py
  │   ├─ llm/payload.py    构造请求
  │   ├─ llm/api.py        HTTP 调用 + 写 llm_request/response
  │   ├─ llm/parse.py      JSON 解析 + schema normalize
  │   └─ postprocess/      apply_postprocess_chain + 尾部 sanitize/validate/clamp
  │       └─ 写 analysis_result.json
  ├─ flayr.py
  │   └─ 合并 analysis.json + improved_video_plan.json
  └─ report.py
      └─ 通过 artifacts.py 取证据帧，写 report.html
```

原则：

- `flayr.py` 负责流程编排，不直接承担 LLM / prompt / postprocess / 报告等核心实现。
- 包之间依赖单向：`translation → llm.api` 而不是 `llm` 顶层；`llm.pipeline → postprocess` 单向，`postprocess` 不反向依赖 `llm.pipeline`。
- 包级 `__init__.py` 不做主动 re-export，避免下游被动加载整套依赖图。
- 核心模块不互相驱动业务流程，不在内部创建完整 run pipeline。

---

## 5. 当前架构风险

### 5.1 ~~`analysis_input.md` 仍在 harness 中构造~~（已解决，2026-05-28）

原 risk：prompt 装配混在 `flayr.py` 里，凝聚度差、变更频率与 harness 不匹配。

处理：拆出 `flayr_core/prompt.py`，迁入 `write_analysis_input` 和 5 个辅助函数。
`flayr.py` 从 637 行降到 463 行，单一负责 CLI / 校验 / 编排 / analysis dict 装配。

### 5.2 LLM 输出 schema 仍偏 report-first

当前 `analysis_result.json` 适合报告，不适合直接合成视频。未来做 `compose.py` 前，需要新增 production schema：

```json
{
  "improvements": [
    {
      "id": 1,
      "type": "script | pacing | visual | subtitle",
      "time_range": { "start": 0.0, "end": 3.2 },
      "original_text": "...",
      "improved_text": "...",
      "visual_instruction": "...",
      "requires_tts": true,
      "requires_subtitle": true
    }
  ]
}
```

### 5.3 视频片段切割仅用于分析侧，生产侧仍缺

分析侧已具备：`video.py` 导出 `audio.wav`；`api.audio_to_mp3_data_url(start, duration)`
按时间窗切音频片段（阶段二声画对齐用）；`api.video_to_data_url(start, duration)` 可整片或按时间窗重编码喂 omni。

进入 improved.mp4 生产阶段前仍需补：

- 写出可复用的生产侧视频 segment manifest（当前 Phase C 只在 LLM 请求内临时切片，不落生产产物）。
- 视频元信息 manifest，包括分辨率、帧率、编码。

---

## 6. 未来生产系统模块

只有当目标从“分析报告”进入“自动生成 improved.mp4”时，才加入以下模块。

### `timeline.py`

职责：

- 消费 production improvements schema。
- 检测时间段重叠。
- 合并冲突提升点。
- 输出 `timeline.json`。

### `tts.py`

职责：

- 根据本地语言和改写话术生成音频。
- 控制语速和目标时长。
- 输出 `duration_delta`。

### `compose.py`

职责：

- 消费 `timeline.json`、TTS 音频、原始视频素材。
- 完成片段保留、替换、字幕、标注、调速。
- 输出 `improved.mp4`。

关键风险：

- 声画同步。
- TTS 时长和原片段时长不一致。
- 多提升点时间段冲突。
- ffmpeg filter_complex 复杂度。

---

## 7. 近期建议

1. 保持当前 report-first 产品稳定，不急着接入 `compose.py`。
2. 先把 `analysis_result.json` 和未来 production schema 分开，避免报告 schema 被合成需求污染。
3. ~~如果继续拆代码，优先拆 `prompt.py`，把 `analysis_input.md` 构造从 harness 中移出。~~（已完成）
4. 如果要外发报告，再做 HTML 图片内嵌或 report assets 打包。
5. `STAGES` 阶段时间常量目前在 `flayr.py` 和 `llm/parse.py` 各有一份硬编码，且 `artifacts.stage_time_ranges` 写死 S1-S6 分段；等 LLM 稳定输出真实 `time_range` 后统一收口，让"阶段时间由 LLM 识别"的设计原则真正落地。
6. `postprocess/claims_my.py` 和 `health_rewrite.py` 是市场/品类硬编码的妥协。未来扩品类/扩市场应新建平级文件（`claims_xx.py` / `xx_rewrite.py`），不要往现有文件塞；积累到 3-4 个后考虑抽象成 `references/category-policies/*.yaml` 配置层。
7. `validate_stage_ownership` 与 `validate_evidence_alignment` 内含 MY 市场 KKM 硬编码，未来抽到 `claims_my.py` 的 validate 区，让 `validate.py` 保持纯通用校验。
