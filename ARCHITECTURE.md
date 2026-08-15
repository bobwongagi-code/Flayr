# Flayr 技术架构设计

> 当前架构基准：2026-08-13（双模型职责路由 + Stage1 证据账本 + 定向原生视频复核 + 分段 Stage2/Stage3）。本文描述已落地的
> report-first 分析产品及其资源与证据契约。冻结设计的唯一来源是
> [`references/ADR007.md`](references/ADR007.md)；Stage1 交接细节以
> [`references/stage-evidence-contract.md`](references/stage-evidence-contract.md) 为准。

---

## 0. 契约（spec §0）

> 本节是系统当前实现的**唯一真相源契约**（2026-08-08 更新）：只描述实际存在的东西，不写愿景。
> 各文档（商业评判框架 / 观察指引 / QA-RULES）对本节内容只引用不复制；冲突时以本节为准。
> 任何"要不要加 X"的提案先对照本节自查，避免基于过时结构或臆想决策。

### 0.1 素材包清单（每条视频，`process_video` 产出）

| 产物 | 生成者 | 可选 | 失败降级 |
|---|---|---|---|
| `frames/`（预算内自适应，最高约 2fps） | ffmpeg | 必需 | 记录 `degraded`，独立产物继续；不生成虚假帧 |
| `focus_frames/`（首尾 5s 各 2fps，Hook/CTA） | ffmpeg | 必需 | 同上 |
| `audio.wav`（混音，**不分轨**——人声分离已评估不采纳） | ffmpeg | 必需 | 同上 |
| `transcript.txt / .srt / .zh.txt` | 北京 MaaS 在线 Fun-ASR | 必需（可显式降级） | 默认失败并返回非零；`--allow-degraded` 下继续但不得发布为 `completed` |
| `shot_track.json`（自适应镜头边界） | ffmpeg 场景检测 | 默认开（零成本） | 状态标记，缺失显示占位 |
| `subtitle_track.json`（OCR 权威字幕轨） | 视觉模型，auto 策略（有分析 key 即开） | 可选 | 状态标记，缺失显示占位 |
| `_preprocess.json`（复用缓存） | flayr.py | 自动 | `--reuse-preprocessing` 仅在源视频内容和预处理配置指纹完全一致时命中；旧缓存或任一配置变化会重跑 |

### 0.2 Step-0 + 分层分析架构 + Phase C

模型路由只有一份：`qwen3-vl-plus` 写入 Stage1-A/C 视觉观察并承担 Phase C 定向视频复核，`qwen3.7-plus` 负责产品合同、Stage1-B/D 资格投影、Stage2/Stage3、综合与世界知识判断。路由只改变 provider 执行者，不改变 ADR-007 的字段所有权、Evidence Ledger、resolver 或 Phase C 合同。`qwen3.6-plus` 只能显式替代 judgment 角色并继续与 `qwen3-vl-plus` 配对，不是自动 fallback；旧单模型参数仅服务历史严格回放，`qwen3-vl-flash` 已退役。

- **Step-0（产品合同）**：只吃运营产品信息与品类知识，先生成卖点分流计划和 `proof_contract`；
  `observable_dimension` 是 S4 单一主证明的硬边界，`consumer_outcome` 只负责自然语言表达结果。
- **阶段一（事实）**：Stage1-A 固定消费 canonical 帧/时间线、在线 Fun-ASR 和 OCR，不把整支原生视频作为首次事实源；Stage1-B 负责首次 S1-S6 资格投影。若必需信号缺失、未知或冲突，Stage1-C 最多一次只追加目标阶段的原生视频候选观察，随后 Stage1-D 使用判断模型只读 Canonical A/C 账本并重投影目标资格。C 不能输出 `stage_evidence_checks`，D 不能补写事实。A/B/C/D 每次 provider 响应都先写入带完整 request identity、response hash、retry/usage 元数据的 `stage1_provider_*.json`；严格回放缺失或身份变化时直接失败，不得回退到缓存或 provider。补观察完成后才锁定 facts。
- **阶段二（判断）**：只消费锁定 facts 中已经资格化的阶段证据 + analysis_input.md，不附视频或音频。默认路径按 `S1+S2 / S3+S4 / S5 / S6` 四个小阶段组分别请求，随后由只读 Stage3 综合；Stage2/Stage3 provider 原文先写入 `stage2_provider_*.json`，单个阶段组失败不得让其他组重跑或丢失。Stage3 只输出建议 prose 和 target stage，时间范围、证据 ID、gap type、priority 由代码从已锁定 Stage2 结果投影，不能由模型重新编写。
- **Phase C（回看）**：代码确定性检测覆盖、资格、连续性或 resolver 冲突，≤2 阶段、仅一次；模型自报低置信只能与独立信号相关联，不能单独花费视频预算。切对应阶段原生视频片段，并同时提供窗口安全 ASR；VL 只负责视觉内容，不能把视频音轨当作已理解语义。回看只接受受限 `stage_patches[]`，再重跑目标阶段的后处理链。Phase C 不能绕过 Stage1，也不能新增或改写已锁定 facts。规范见 0.7。
- **后处理链**：确定性投影、validate、resolver、局部 patch repair 与 qa_warnings；默认路径不做整对象 Stage2 repair。
- **最终建议收敛**：确定性 severity 与可选 S4 视觉复核全部完成后，若仍有 `large` 阶段未被
  `improvements` 覆盖，只做一次纯文本缺项补全；它不得重判阶段，失败时保留主分析并写明状态。

### 0.3 字段与运行时契约

`references/analysis-output-schema.json` 是模型输出字段说明；`flayr_core/analysis_model.py` 读取该 schema，
集中声明结果领域模型、标准化必需字段、唯一的 runtime projection、契约版本和
`raw_model_response -> validated_normalized_result -> final_derived_result` 生命周期。
四层职责、字段唯一所有者、技术重放与语义重跑边界以
`references/result-pipeline-architecture.md` 为准；代码修复后的结果收口必须优先使用
`scripts/replay_finalization.py` 离线验证；它要求 source run 的 provenance、canonical 结果、分析上下文和 `analysis_input.md` 哈希全部一致，不得用真实模型调用代替确定性回放。
`llm/analysis_contract.py` 只负责边界校验（阶段数量、顺序、改进项范围和标准化结果骨架），不再复制字段清单。
evidence_unit 含多模态事实字段 +
结构化标记（`product_visible` / `product_coverage` / `endorsement_verbal` / `endorsement_visual` / `evidence_strength`）；
标记由模型按定义判，`evidence_strength` 是 floor 门禁的唯一权威强度字段。代码只做确定性消费
（占比累加、归属搬运、状态一致性），不得用正则重新推断语义。

Stage1 的 `stage_evidence_checks` 是共享注册表 `stage_evidence_contracts.py` 的资格投影，不是第二份事实库：

Stage1 到阶段判断的完整交接合同见 `references/stage-evidence-contract.md`。代码为 S1-S6 统一生成
`stage_evidence_gate`：只有两侧证据均已闭合时才进入 grounded 判断；unknown、conflict、预算未闭合
和旧合同结果必须显式阻断或标记 legacy，不能由 Stage2、Repair 或报告层猜成 absent。
每个阶段只能有 `present/absent/unknown/conflict` 之一；`present` 必须列出该阶段全部必需信号并引用真实单元，资格强度由引用单元的
`evidence_strength` 重新计算，不能信任模型在投影中自报的强度。`unknown`、`conflict` 和不完整覆盖不得被归一成 `absent`。
旧 `functions` 仅保留为兼容和原始观察统计字段，新合同下不得作为阶段归属或严重度依据。

### 0.4 severity 判定宪法（2026-07-24，floor/ceiling resolver）

> **模型供初始判断，代码只收窄确定性区间。** `model_severity` 是默认结果；
> `postprocess/derive.py` 不再用 E/W/C 连续公式、痛点系数或另一套阈值模型给 severity 赋值。

- `resolve_severity` 是唯一的 severity 收口点。所有规则只能提交 `floor` 或 `ceiling`，
  不能直接写 severity；所有 floor 取 `max(触发的 floor)`，所有 ceiling 取
  `min(触发的 ceiling)`，因此合并与调用顺序无关。
- `floor == ceiling` 是合法 clamp；`floor > ceiling` 是约束冲突，保留
  `model_severity`，标记 `constraint_conflict`，并进入已有的共享 Phase C 候选/预算，不能
  通过后处理强行覆盖。
- 缺失、unknown、uncertain、非法状态、证据强度不足和 hard-fact 校验不完整都不触发
  severity-increasing floor。规则评估必须写入闭集 `reason_code`，并区分缺字段、事实不确定、
  证据强度不足、约束冲突和模型保留。
- `evidence_strength` 以 Stage1 `video_understanding.*.evidence_units[]` 为唯一权威来源。
  任何提高 severity 的 floor 都要求所引用证据的最弱强度为 `direct` 或 `explicit`；
  `inferred`、`absent`、缺失和非法值都只能保留模型判断。旧产物仍可读取，但没有新字段或
  新标记时不能触发这些 floor。过渡期内，历史产物大多数会因缺少该 Stage1 字段而不触发
  floor，这是有意的安全停用，不是 resolver 失效；只有新事实链产出强度后才进入校准。
- 使用状态与证据强度是两个独立门禁，必须同时满足（状态 predicate AND `direct|explicit`），
  不能用一个轴替代另一个轴。
- S1 Hook large floor 还要求 `repair_s1_hook_boundaries` 在所有修复完成后写入消费 marker；marker
  必须带 checked fields 与修复后事实快照哈希，没有或过期的 marker 不能消费修复前的 Hook 边界字段。
  S1 landing/命题锚定只提交 medium floor。
- 主分析、Repair 重跑和 Phase C 都从 `llm/parse.py` 归一后的同一份 S1/S3/S4 flag 读取，
  并在消费前经过同一条 `repair_s1_hook_boundaries` / hard-fact marker 链；不得在任一路径
  另行维护 landing、使用完成度或效果完成度判断。
- S3 使用完成度是四态 `usage_evidence_state`：`none`、`partial`、`complete`、
  `uncertain`。S3 large floor 只允许在达人 `none`、标杆 `complete`、双方 hard facts
  校验为 `consistent` 且证据强度满足时提交；`partial` 不能被压成 `none`。
- S4 效果完成度是四态 `effect_evidence_state`：`none`、`result_only`、`verified`、
  `uncertain`。`result_only` 不得被当作 `none` 或 `verified`；S4 large floor 当前只记录
  audit candidate，不启用 severity 改写，直到完成边界校准和全新 blind cohort 门禁。
- S3/S4 结构化 flag 的 `evidence_ids` 也必须是对应侧 Stage1 `evidence_units` 中的真实、唯一 ID；
  不得引用不存在的 ID、重复 ID 或 repair 生成的 `_NO_` 占位 ID。主阶段引用和嵌套 flag 引用都必须通过同一闭世界校验。
- `repair_evidence.py` 只检查状态与硬布尔事实之间的机械矛盾，并把结果写入
  `_postprocess_state.evidence_hard_fact_checks`；它不能推断、重写或降级 S3/S4 的语义状态。
  `partial`、`result_only`、`uncertain` 和 hard-fact conflict 均保留模型 severity。
- 执行分与多模态观察仍可写入 `severity_derivation`，但属于诊断/审计数据，不参与 resolver。
  `painpoint_relevance` 由商业优先级层消费；severity 与商业优先级保持分离，不能把业务权重
  偷渡回 derive。

derive 的回归必须同时覆盖 resolver 的 max/min 交换律、clamp/conflict 边界、四态及其硬事实
一致性、Stage1 强度门禁、repair marker 顺序和 S4 audit-only 状态。校准卡片与 fresh blind
验收集不得混用；真实重复运行稳定性、floor 捕获率和回归数必须单独报告，缺少人工结构性 GT
时只能标记为不可测，不能把缺失当成通过。

### 0.5 失败、降级与完成状态

- 在线 Fun-ASR 是 compare/improve 的语音证据依赖；失败时默认写入 `FAILED` 并返回非零。显式 `--allow-degraded` 才能继续，但必须写入 `degraded_reason`、不得发布成功清单，也不得把占位转写当成真实事实。
- Stage1 阶段资格不完整时只允许一次有预算记录的定向补观察；补观察失败、仍有结构性合同错误或响应预算不足时，运行失败或保留 `unknown`，不得把失败伪装成完整事实，更不得用 Stage2/repair 自行补事实。
- OCR、镜头轨或音频质检失败时，写入明确的阶段状态和 `degraded_reason`；独立产物可以继续，但缺失能力不得产生占位证据或真实严重度。
- 已请求的 LLM 网络调用、响应解析、运行时 schema 或必需后处理失败时，任务状态为 `failed` 并返回非零；已有中间文件不能把任务发布为 `completed`。
- `compare` / `improve` 没有完成的 LLM 分析时默认失败；只有显式 `--allow-degraded` 才能继续，并保持未知 severity 为空、写入降级清单。
- 只有子进程成功、最终成功清单和必需产物完整性校验全部通过时，运行状态才是 `completed`。

### 0.6 第三方背书观察（`endorsement_verbal` / `endorsement_visual`）

这两个字段只记录观察，不在 Stage1 直接判定背书有效性：
- `endorsement_verbal`：口播/字幕中出现硬来源词；
- `endorsement_visual`：画面中清晰出现独立证书、检测报告或机构认证视觉证据。

机构类型 + 关联性门槛**同时成立**才构成有效硬背书，后续 S5 规则再消费这些观察：
- 机构类型：监管/认证机构（KKM、Halal、SIRIM、TISI…）、行业协会、第三方评测中心/实验室、
  高校与研究机构、三方调研咨询公司、疾病·医院·防治中心。
- 关联性门槛：该机构的实验/数据/研究**在证明本产品价值**。

判例：大学检测报告证明本品杀菌率 → true；画面出现 KKM 认证标号 → true；
仅提机构名/赞助商/合作 logo（无证明本品价值的数据） → false；达人自称"用了三年" → false（自述）；
用户评论截图 → false（社会证明，属 S5 信任内容但非机构背书）。

### 0.7 Phase C 输入/输出规范

- 输入：目标阶段的标杆+达人原生切片（fps3、480px）及窗口安全 ASR，时间窗 = 阶段 time_range **±2s 缓冲**；
  prompt 必须告知"切片边界可能有误差，按功能归属判断，勿把相邻阶段内容算进本阶段"。
- 输出：`stage_patches[]` 只能包含该阶段双方 `*_evidence_ids` 与双方结构化 flag（S1 为
  `*_hook`，S2-S6 为 `*_sN`）；禁止输出或写入 `severity`、叙事字段、执行分、商业优先级、
  improvements 或 multimodal 结论。补丁必须覆盖双方完整事实对，引用只能来自锁定 facts。
- 应用：合法补丁清除该阶段旧的 multimodal 聚合与 postprocess marker，随后进入同一条
  repair、validate、resolver 链；非法字段、非目标阶段、重复补丁或未知 evidence_id 一律拒绝。
- 审计：`phase_c_review` 使用 `schema_version=2` 与
  `snapshot_schema=phase_c_patch_snapshot_v1`，只记录补丁字段及其应用前后 resolver 结果；
  旧整段阶段快照在评测中按独立 schema 分开统计，不能与补丁快照合并比较。
- 引用口播必须能对上切片音频/转写，听不清标 `voice_only` 并写明，**禁止推断补全未听清的话术**
  （kakwan S6 幻觉教训）；回看 prompt 不得含方向性压力（如"持平必须给 small"——已删）。
- 合并后重跑全套校验；facts 不可改。

### 0.8 验收与回归原则

- 回归集 = **输入视频 + `references/ground-truth-labels.md` 人工标签**；绝不 diff runs/ 存档输出。
- `calibration` 可用于改规则；打开过结果或参与讨论的样本只能是 `seen_validation`；`blind` 只能在
  规则冻结、人工 GT 完成且模型结果未打开时建立。blind 一旦用于修改规则即标记 `spent` 并降级。
- 新 blind 用 `manage_validation_cohort.py` 锁定视频内容、GT、输入清单、代码、prompt/schema 与模型配置哈希；
  验收时必须提供同一冻结锁，目录名不参与身份判断。
- GT 不只记录最终 severity：每个有效阶段还要有单侧执行分、比较方向、决策关键事件和理由；整条视频
  记录人工 Top-N 根因。`evaluate_analysis.py` 按 L0 预处理、L1 事实召回、L2 证据使用/判断、L3 derive、
  L4 Phase C 分层归因，不能再从端到端标签倒推错误层。
- 晋级门槛预先写死：至少 12 个独立 blind 视频对、4 个品类、2 个市场；S1-S4/S6 每阶段至少 6 个
  gap 与 6 个 small 对照，S5 至少各 3 个；总准确率不低于 80%、单阶段不低于 70%、两档错误为 0、
  Stage1 决策事件召回和 Stage2 使用率均不低于 90%、Top-N 根因召回不低于 80%、Phase C 不得引入回归。
- 相同标杆或相同视频内容的多个配对不是独立样本，cohort 冻结会按 SHA-256 拒绝重复。

历史结果在没有 API 额度时可以通过 `scripts/replay_derive.py` 离线重放 derive：
`python3 scripts/replay_derive.py --input-root <历史结果目录> --output <重放目录>`。
该命令只读取已保存的 `analysis.json`，不创建 LLM 请求、不访问网络，并为每个结果保存源文件哈希、可发现的 sidecar 哈希、代码提交、证据强度门控和 derive 前后变化。
历史产物缺少 Stage1 `evidence_strength` 时，重放必须报告 `gate_closed`，保留模型 severity。离线重放摘要必须把
`historical_final_to_replay`（历史最终产物和当前代码的差异）与 `model_to_replay`（当前 resolver 相对模型原判的
实际效果）分开；后者只有一个触发规则时才计入该规则的 `direct_rule_effects`，多规则变化必须标为不可唯一归因。
因此历史回放只能证明代码路径、安全门控和已生效 ceiling 的离线表现，不能证明 floor 激活后的准确率。

S5 的来源对账 warning 在历史产物中异常集中，`S5_no_trust_ceiling` 在完成独立边界卡、双人盲标、证据状态稳定性与
新鲜 blind 验收前一律为 `audit_only`，不得改写 severity。其后续边界必须区分：`explicit_absence`（明确没有独立来源）与
`product_claim_or_offer`（存在产品卖点、规格或优惠主张，但没有可追溯的独立来源）；后者不是 absence，也不能复用 absence
的 ceiling。S6 CTA ceiling 是当前唯一在历史回放中观察到直接模型修正收益的规则，样本量仍很小，必须在新鲜 blind cohort
中分层抽样验证，不能据此宣布通用有效。

历史 `are_xie/S5` 保留为“认证来源归属”机制回归样本：它用于验证认证仍归 S5、不会被来源校验或 S5 audit-only 逻辑误删，
不能作为针对该样本固定最终 severity 的规则。变体 QA 中曾出现的 `*_CERT_S5`、`*_CTA_SRT` 等 ID 已确认是后处理生成的
非变体 evidence unit 被变体校验器误纳入，不是跨阶段 ID 泄漏；它们应记录为 `QA-NON-VARIANT-UNIT-SKIPPED`，真实多 SKU
字段问题才记录为 `QA-VARIANT-OBSERVATION-CONFLICT`。

derive 边界卡片属于 `calibration`，不得直接放入 blind cohort。每张卡片至少记录 S3/S4 双侧
预期四态、双侧 hard-fact 校验结果和 `expected_floor_outcome`（`trigger_large`、
`no_trigger_medium_kept`、`uncertain_no_trigger` 或 `audit_only_candidate`）；卡片只定义待验证
的预期，不替代两名标注者的独立标注，也不替代全新 blind 样本的晋级验收。重复运行稳定性必须
把缺字段视为不可确认，floor 捕获率只对人工明确标记为结构性缺口的样本计算并给出 Wilson 区间。
S3 边界卡片必须覆盖 creator 的 `none/partial` 与 benchmark 的 `partial/complete` 两条边界；
任一边界出现标注分歧都进入 `uncertain`。S4 的 `result_only/verified` 边界也采用同规格的
双人盲标，不能只为单个回归样本补一个结论；`youkoubo-c1/S4` 必须作为未知背景的普通卡片参与。
repair 的 hard-fact 检查使用同一批卡片验证机械矛盾，不承担语义标注。报告层展示这些更细状态
属于后续批次，本次只保证结构化状态与 audit trail 完整。

S4 large floor 的启用不是可手动翻转的布尔开关。只有通过摘要校验的外部 activation manifest，
经 `postprocess/calibration.py::load_s4_large_floor_activation_evidence` 加载为可信对象，再显式
传入 severity 收口入口时才可启用；结果文件中的 `derive_activation_evidence` 字段永远不具备激活权限：
至少 24 张 calibration 边界卡、S3/S4 边界覆盖、双人盲标通过、至少 5 次固定核心 hard-fact 字段的重复运行且稳定（每次观察绑定同一 input fingerprint）、
至少 12 对全新且已冻结的 blind cohort（至少 4 个品类、2 个市场）、floor coverage 已测量且无
derive/Phase C 回归。缺任一证据都保持 `audit_only`；当前工作树没有这组真实验收证据，因此不启用。

### 0.9 运行级资源预算

每次 `flayr.py` 运行创建一个 `flayr_core.resources.ResourceBudget`，并在主流程开始时激活；媒体预处理、OCR、LLM 重试、翻译、Phase C 和报告都复用同一个对象，不能在子模块内重置预算。默认上限字段为：

`max_source_bytes`、`max_source_duration`、`max_extracted_frames`、`max_ocr_calls`、`max_llm_calls`、`max_single_request_bytes`、`max_total_uploaded_bytes`、`max_download_bytes`、`max_report_bytes`、`max_total_wall_time`、`max_cost_estimate`；另外用 `max_local_artifact_bytes` 约束运行目录内生成的帧、音频和证据产物。

源文件在 `ffprobe`、抽帧或转码前先做存在性和字节预检，登记时再复核；时长、大小、超时和计数必须是有限、非负且不超过硬上限的数值。抽帧、OCR 和 API 重试都先预留额度；超过额度时停止该分支并保留可审计的失败/降级状态。基础帧、焦点帧和音频转码同时使用剩余本地产物预算作为 ffmpeg 输出上限，超限会删除本次部分产物；复用旧预处理时也重新计入这些产物。外部命令使用有界流式 stdout/stderr 捕获；图片、视频和音频仅在没有文件上传接口时使用带签名校验和字节上限的分块 Base64。最终 `analysis.json` 保存预算 limits/used 快照，报告 HTML 写入前执行 `max_report_bytes` 校验。

API 请求的 bearer 凭据只通过子进程标准输入传递，不进入命令参数或临时文件；请求载荷和流式响应只放在带 Flayr 前缀的短生命周期临时目录中，调用结束即清理，下一次调用会清理已过期的同类残留。每个逻辑请求复用稳定的 request ID/idempotency key，`api_events` 记录每次实际尝试、重试原因、请求字节数和估算成本，内部重试和外层重取都消耗同一份运行预算。

证据时间解析是严格校验而不是修复：缺失、反向、越界、负数、非有限值、结构歧义和无法确认角色归属的时间范围返回无效；不得交换起止点、裁剪到视频边界、补默认窗口或把合并的标杆/达人时间字符串用于单侧取证。无效时间只能跳过证据或让校验失败，不能生成新的证据事实。

---

## 1. 产品阶段

### 当前阶段：Insight / Report MVP

当前 Flayr 的核心交付不是自动成片，而是给运营团队一份可复核的分析报告：

```
爆款视频 + 达人视频
  → 视频解析、抽帧、抽音频、转写、中文翻译
  → Step-0：建立与视频独立的产品卖点分流计划和单一视觉证明合同
  → 阶段一：以 canonical 帧/时间线、窗口安全转写和 OCR 采集单视频原子事实；资格或连续性未闭合时，对目标窗口最多做一次原生视频补观察
  → 缺证据时：每侧最多一次定向 Stage1 补观察，之后锁定 facts
  → 阶段二：按 S1+S2 / S3+S4 / S5 / S6 分组判断，只喂已资格化 facts 文字，再由只读 Stage3 综合
  → Phase C：仅在预算和确定性门控允许时，对目标阶段切原生视频片段回看一次并应用局部 stage patch
  → 最终建议收敛：仅补齐确定性推导后遗漏的 large 阶段提升点
  → report.html + analysis.json
```

核心理念：**连续画面 + 转录语义 + 本地音频硬质检，事实判断分离**（详见 3.6）。模型负责连续画面与文本语义；
阶段一锁定事实防止两条视频串证据，阶段二在事实基线上重获视觉证据做判断。

当前阶段的重点：

- 识别东南亚本地语言口播，并输出中文翻译。
- 两级运行：仅有视频时完成“视频证据分析”；补充品类、价格带、核心卖点、目标用户/痛点与购买动机后升级为“策略增强分析”。
- 严格按主流程内置三步分析流程、商业评判框架和目标市场知识库执行：先做整片感知判断，再按 `structure_library_full.md` 完成槽位/模块识别和证据归因，最后输出对标差距与 GMV 优先改造。
- 基于 `structure_library_full.md` 的官方模块编号与适配规则对事实做阶段归因，而不是先套阶段再找素材。
- 阶段时间不是硬切片，必须由模型先整体理解视频后填写真实 `time_range`。
- 报告结论必须引用 `evidence_units`，并由该事实时间段抽取对应视频帧。
- 输出改进建议和画面方向，明确不生成替换视频。

### 产品边界

Flayr 只输出可复核的分析报告、证据和改进建议，不生成达人音色视频、AI 示意视频或替换后的完整视频。

---

## 2. 当前代码结构

```text
scripts/
├── flayr.py                          # skill harness: CLI、依赖检测、校验、流程编排
└── flayr_core/
    ├── artifacts.py                  # manifest 读取、帧候选、按时间段选帧
    ├── analysis_model.py              # schema 驱动的结果领域模型、字段投影与生命周期
    ├── llm/                          # LLM 调用、Stage1/Stage2/Stage3 编排包
    │   ├── __init__.py
    │   ├── api.py                    # HTTP 调用底层 + Keychain
    │   ├── analysis_contract.py      # LLM 结果最小运行时结构契约
    │   ├── compact_eval.py           # 只读模型评估工具
    │   ├── json_codec.py             # LLM JSON 文本容错解析
    │   ├── media.py                  # 多模态媒体输入构造
    │   ├── product_profile.py        # 产品地基与 S4 证明合同归一化
    │   ├── parse.py                  # 阶段 Flag、结果 schema normalize + 兼容导出
    │   ├── payload.py                # build_*_payload 系列请求构造
    │   ├── pipeline.py               # Stage1、分段 Stage2、Stage3、Phase C 与 finalizer 编排
    │   ├── stage2_projection.py      # Stage2 判断到代码所有字段的确定性投影
    │   ├── s4_visual_verifier.py     # 旧 S4 复核产物与离线 fixture 兼容；不含 provider 调用
    │   └── stage_review_contract.py  # 阶段 patch 合同
    ├── postprocess/                  # 分析结果投影、校验、局部修复与专项规则
    │   ├── __init__.py               # 仅 re-export apply_postprocess_chain
    │   ├── utils.py                  # 通用工具（SRT / evidence_unit / 时间关系）
    │   ├── audit.py                  # 审计轨与变更记录
    │   ├── calibration.py            # activation manifest 与校准门禁
    │   ├── claims_my.py              # 马来西亚 KKM/认证主张专项
    │   ├── commercial_priority.py    # 商业优先级聚合（不写 severity）
    │   ├── derive.py                 # floor/ceiling resolver 与派生字段
    │   ├── global_diagnosis.py       # 全局诊断投影
    │   ├── health_rewrite.py         # 健康品类合规重写专项
    │   ├── proposition.py             # 产品命题与建议投影
    │   ├── repair.py                 # 兼容路径的结果修补
    │   ├── repair_claims.py           # 主张/证据声明的局部修补
    │   ├── repair_evidence.py        # 证据硬事实机械检查
    │   ├── repair_stages.py          # 目标阶段局部 patch 应用
    │   ├── validate.py               # 通用校验
    │   └── chain.py                  # apply_postprocess_chain 流水线编排
    ├── finalization/                  # 最终投影、等价性与发布前收口
    │   ├── contracts.py
    │   ├── equivalence.py
    │   └── facade.py
    ├── stage_catalog.py               # S1-S6 唯一阶段目录与预处理回退窗口
    ├── stage_ownership.py             # 跨阶段认证归属规则
    ├── prompt.py                     # analysis_input.md 装配（LLM 输入包）
    ├── report.py                     # HTML 报告渲染
    ├── translation.py                # 本地语言转中文（调用 llm.api）
    ├── utils.py                      # 通用文件/进程 helper（read_optional_text、write_json 等）
    ├── video.py                      # ffmpeg/ffprobe、抽帧、音频、manifest 写入
    └── asr.py                        # 在线 Fun-ASR 转写和时间戳归一化
```

### 当前覆盖

| 能力 | 模块 | 状态 |
|------|------|------|
| CLI / 依赖检测 / 校验 / 流程编排 | `flayr.py` | 已覆盖 |
| 视频时长、抽帧、音频提取 | `video.py` | 已覆盖 |
| frame/focus/stage manifest 读取和选帧 | `artifacts.py` | 已覆盖 |
| 在线 Fun-ASR 转写和时间戳归一化 | `asr.py` | 已覆盖 |
| 中文翻译 | `translation.py` | 已覆盖 |
| LLM 请求构造 / 调用 / schema 解析 / 分段 Stage2 | `llm/` 包（api / payload / parse / pipeline / finalizer） | 已覆盖 |
| 分析结果修补 / 校验 / 品类合规 | `postprocess/` 包 | 已覆盖 |
| analysis_input.md 装配 | `prompt.py` | 已覆盖 |
| HTML 报告 | `report.py` | 已覆盖 |

---

## 3. 模块边界

### 3.1 `flayr.py` — Skill Harness

职责：

- CLI 参数解析。
- 依赖检测。
- 输入校验。
- 创建 run directory。
- 串联 video / asr / translation / prompt / llm / report。
- 装配 analysis dict、写出 `analysis.json` 和报告所需的证据产物。
- 计算分析等级和结论边界，并随分析输入、结构化结果与报告输出；缺少产品策略时不阻止事实分析，但限制策略结论。

约束：

- 不直接承担 LLM、报告渲染、抽帧、在线 ASR、翻译、prompt 装配等核心实现。
- 不写 `analysis_input.md`（已迁至 `prompt.py`），harness 只负责调用。
- 保持命令入口稳定：`python3 scripts/flayr.py ...`。

### 3.2 `video.py` — 输入侧视频处理

职责：

- 用 `ffprobe` 读取视频时长。
- 用 `ffmpeg` 在共享预算内抽取自适应基础帧（最高约 2fps）。
- 抽取 Hook/CTA 加密帧。
- 生成 `frames/manifest.json`、`frames/stage_frames.json`、`focus_frames/manifest.json`。
- 提取 `audio.wav`。

约束：

  - 只负责输入侧拆解，不负责报告、不负责 LLM、不负责报告外的媒体生成。

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

### 3.3b `video_evidence.py` — 二级视频证据视图

职责：

- 基于已有 `frames/`、`focus_frames/`、`audio.wav`、`transcript.srt` 和词级 ASR 边界生成复核用 artifact；原始 SRT 只作审计，窗口消费使用 `transcript_windowed`。
- 写出 canonical `frames/analysis_manifest.json`、`analysis_stage_frames.json`，以及 `selection_report.json` 和 `.html`；后者记录滑动窗口视觉去重的 keep/drop 原因。
- 写出 `contact_sheets/`，把 Hook、CTA、阶段代表帧按时间顺序压成联系表。
- 写出 `timeline_views/`，把 canonical 帧序列、波形、口播时间戳放在同一张图中，并记录所用帧路径以便审计。
- 写出 `transcript_packed.md/json`，作为紧凑的时间戳口播索引。
- 写出 `video_evidence_audit.json`，自检关键证据视图是否真实落盘。
- `prompt.py` 在 `analysis_input.md` 中展示这些证据索引；原始转写只保留路径，不把全文发送给阶段 2/比较模型。
- `llm/payload.py` 在单视频事实抽取时优先附加 Hook/CTA timeline view，再补原始帧。

约束：

- 不删除原始帧。
- 不直接改变评分、severity 或报告结论。
- 缺少可视化依赖时记录 `degraded`，不阻断不依赖该能力的主流程；已请求的 LLM/API/schema 失败仍必须阻断并返回非零。

### 3.4 `asr.py` — 在线语音转写

职责：

- 通过批准的北京 MaaS Fun-ASR endpoint 发送本地音频 Data URI。
- 保留 `transcript.txt`、`transcript.srt` 和 `transcript.words.json` 作为审计/兼容产物；下游阶段归属唯一消费接口是词级时间线生成的 `transcript_windowed`。
- 直接归一化句级与词级时间戳，不在本地运行 ASR 模型。
- 输出本地语言 `transcript.txt`。
- 输出短分段时间戳口播 `transcript.srt`，供审计和兼容读取；有词级时间戳时另生成 `transcript_windowed.md/json`，阶段证据对齐以窗口安全版本为准；没有词级时间戳时不能把粗分段扩展成精确阶段证据。

约束：

- 不用英文式空格分词判断是否有有效口播。
- 东南亚语言如泰语、马来语、印尼语必须保留本地语言转写。
- 涉及口播归属到具体阶段时，有词级时间戳则以 `transcript_windowed` 的窗口为准；没有词级时间戳只能标记时间粒度不足，不能用粗粒度 `transcript.srt` 整段推断精确边界。

### 3.4b `speech_mode.py` — 证据组织模式

职责：

- 根据 `transcript.txt`、`transcript.srt`、`subtitle_track.json` 和音频存在性，为每条视频写出 `speech_mode`。
- 模式包括 `spoken`、`subtitle_driven`、`visual_driven`、`music_driven`。
- 为 `prompt.py` 和 `llm/payload.py` 提供统一的证据优先级提示。

约束：

- `spoken` 才以口播时间线作为主骨架。
- `subtitle_driven` 以 OCR 字幕轨作为文案骨架，不能把字幕写成口播。
- `visual_driven` / `music_driven` 不因 `voiceover` 为空天然扣分，必须按画面变化、镜头轨、BGM/节奏判断阶段功能是否完成。

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

按职责拆分请求、解析、Stage1、分段 Stage2、Stage3 和 Phase C。请求与解析依赖单向流入 `pipeline`；
`stage2_projection` 只依赖 artifacts 和 Stage1 证据合同，再由 `pipeline` 调用。下游（translation）只 import `llm.api`，不被动加载整套业务规则。

| 子模块 | 职责 |
|------|------|
| `llm/api.py` | HTTP 调用底层 + 三个 data URL 工具：`video_to_data_url`（原生视频 ffmpeg 重编码 fps=3+降分辨率含音轨，支持 start/duration 切片）/ `audio_to_mp3_data_url`（整条或按 start/duration 切片）/ `image_to_data_url`（关键帧）。不含业务规则。 |
| `analysis_model.py` | 读取模型 schema，定义结果字段组、runtime projection、契约版本和 raw/normalized/final 生命周期；不承载业务判断。 |
| `llm/analysis_contract.py` | 结果外壳与标准化结果骨架的边界校验；不承载阶段业务规则，也不复制字段清单。 |
| `llm/json_codec.py` | LLM JSON fence、尾逗号和未转义引号的容错解析；不承载 schema 或业务规则。 |
| `llm/product_profile.py` | Step-0 产品地基、短视频证明计划与 S4 证明合同的归一化；不反向依赖 `parse.py`。 |
| `llm/parse.py` | 阶段 Flag 和最终结果 schema normalize；保留 `parse_json_text`、产品地基函数等兼容导出。含 `STAGES`、`is_effective_voiceover` 等被 `postprocess` 复用的基础接口。 |
| `llm/payload.py` | `build_*_payload` 系列。阶段一 `build_video_fact_payload`（固定 canonical 帧/窗口转写）和 `build_video_fact_recovery_payload`（一次定向原生片段）；阶段二 `build_stage_group_judgment_payload`（四个边界明确的纯文本小组请求）和只读 `build_stage_synthesis_payload`；Phase C `build_stage_review_payload`（结构信号触发的原生视频切片）。 |
| `llm/stage2_projection.py` | 将锁定 Stage1 事实与 Stage2 小组语义结果投影为 `stage_handoff_status`、证据摘要、时间范围和报告兼容字段；不发模型请求，不负责主流程编排。 |
| `llm/pipeline.py` | 主入口：`run_video_fact_extraction`、`run_segmented_stage_pipeline`、`run_large_model_analysis`、`finalize_analysis_result`。分段 Stage2 依次收口四个阶段组并调用 `stage2_projection`，再执行只读 Stage3 综合；所有外部结果经过同一个 finalizer 写回 analysis。Phase C 只应用受限阶段 patch。运行目录保留 raw/validated/final/postprocess 产物，用于区分模型原文、规范化结果和确定性后处理。 |
| `llm/s4_visual_verifier.py` | 只保留历史 S4 verifier 产物和离线 fixture 的合同/结果兼容；模块不再包含 provider 调用。S3/S4 原生视频连续性复核统一走 Phase C。 |

**事实采集、分段判断与只读综合架构**：

| 阶段 | 函数 | 输入 | 产出 | 意图 |
|------|------|------|------|------|
| 一：事实抽取 | `run_video_fact_extraction` → Stage1-A/B → `build_video_fact_recovery_payload`（C，最多一次）→ Stage1-D | A 固定使用 canonical 帧/时间线图 + 窗口安全 ASR/OCR；C 只向目标窗口发送原生片段；B/D 只读 Canonical 账本 | A/C 原子 `evidence_units` + B/D `stage_evidence_checks`，补观察后锁定 | 视觉模型只观察，判断模型才投影资格；原生视频不重新扫描并覆盖主账本 |
| 二：分段对比判断 | `build_stage_group_judgment_payload` → `run_segmented_stage_pipeline` | 每个阶段组的锁定 facts + 必要感官材料 | 阶段事实状态 / relation / model gap | 每个阶段组独立失败与收口，避免完整大对象互相拖累 |
| 三：只读综合 | `build_stage_synthesis_payload` | 已收口的阶段结果 | 全局结论 / 建议草稿 | 不能修改阶段事实、relation、model gap 或 resolver severity |
| C：低置信回看 | `build_stage_review_payload` → `stage_patches[]` | 目标阶段锁定 facts + 对应阶段原生视频片段 | 仅局部 patch | 只修改目标阶段允许字段，硬限制预算和次数 |

关键约束：Stage1 补观察完成后 facts 才锁定并成为"唯一事实源"。Stage2 不接收视频，
**不可新增或改写 facts，也不能用 `functions` 或自由文本补回未资格化阶段证据**；阶段二 temperature=0 保证可复现；
Phase C 由独立的确定性证据检查触发，模型低置信不能单独触发，最多回看 2 个阶段、最多 1 次，不做无限 agent loop；
ffmpeg 不可用时定向原生视频复核失败并保持 `unknown`，不得回退成整片复扫或把页面化证据误称为连续原生视频理解。
音频采用两层合同：本地确定性硬质检可进入报告；语气、BGM、音效的细微商业贡献只作观察，永不进入执行分或 severity。

职责：

- 构建多模态分析请求。
- 使用 Keychain/env 读取 API key。
- 调 OpenAI-compatible endpoint。
- 请求体和临时响应只存在于短生命周期临时目录；不把认证材料或完整请求写入运行产物。
- 解析和修复 JSON。
- 规范化 `analysis_result.json`。
- 校验三步分析契约：整体感知、产品可见度/模块适配/闭环、证据支撑的对标改造（具体校验在 `postprocess/` 包）。
- 维护 `video_understanding.evidence_units -> stage_analysis.*_evidence_ids` 的证据绑定。

约束：

- Prompt 必须同时载入内置三步分析流程、商业评判框架、目标市场知识库与完整 `structure_library_full.md`，并先完成整片判断与事实清单再归因。
- 视频证据分析不得臆测真实卖点、人群适配、价格策略或最终 GMV 排序；策略增强分析才可结合已确认业务输入下完整成交判断。
- 每个阶段必须给出结构库官方模块编号、适配判断、任务完成度、差距类型和口播表现；缺少即判分析结果无效。
- `time_range` 必须是模型理解后的真实阶段时间，而不是机械照抄参考范围。
- 有效口播是信息核心，画面只证明实际可见内容；静音视频改以画面/字幕组织分析。
- 分段生产路径不对完整分析对象做模型 repair；阶段组失败直接产出 typed unknown/degraded。仅兼容读取/旧入口边界可按已配置 provider profile 执行一次允许的 repair，失败则 fail loud。
- `llm/__init__.py` 不主动 re-export 子模块，下游必须显式 import 子模块路径。

### 3.7 `postprocess/` 包 — 分析结果修补与校验

按"职责性质"（确定性派生、审计、局部修复、通用校验、市场/品类专项）拆分；默认冻结路径使用局部阶段 patch，旧整对象修补仅保留兼容读取/调用边界，直到 Phase 5 明确删除。

| 子模块 | 职责 | 行为语义 |
|------|------|--------|
| `postprocess/utils.py` | 通用工具：SRT 读取、evidence_unit 查找、时间关系。 | 纯函数 |
| `postprocess/derive.py` | 唯一 severity resolver；只消费 model severity 与受限 floor/ceiling 约束。 | 不允许其他模块直接写 severity |
| `postprocess/repair_stages.py` | 目标阶段 `stage_patches[]` 的路径、角色、证据 ID 和快照校验。 | 非法 patch 拒绝，合法 patch 后重跑目标链 |
| `postprocess/repair_evidence.py` | S3/S4 硬事实机械一致性检查。 | 只写审计状态，不重写语义状态 |
| `postprocess/repair.py` | 旧结果修补兼容路径。 | 不属于默认分段 Stage2 的整对象修复入口 |
| `postprocess/validate.py` | 通用证据、维度、转写归属和阶段所有权校验。 | 硬错误显式失败；不把缺失伪装成 absent |
| `postprocess/claims_my.py` | 马来西亚（MY）市场 KKM/kelulusan 认证主张专项。扩市场时新增 `claims_xx.py` 平级文件。 | 修改 data |
| `postprocess/health_rewrite.py` | 健康品类（维生素 / 营养补充 / 儿童牙膏）合规重写。含 2 个会抛 SystemExit 的 validate_*。扩品类时新增 `xx_rewrite.py` 平级文件。 | 修改 data + 抛 SystemExit |
| `postprocess/chain.py` | `apply_postprocess_chain`：共享的确定性后处理编排。 | 编排 |

职责：

- 校验三步分析契约：整体感知、产品可见度/模块适配/闭环、证据支撑的对标改造。
- 校验口播归属：标杆与达人转写不得被模型交叉写入对方证据单元。
- 对有口播的阶段建立按阶段时间绑定的口播证据，无法从画面验证时显式标记 `voice_only`。
- 校验阶段顺序、证据时间对应关系和认证信息唯一归属。
- 对已能明确归属的信息做确定性归位：产品身份/卖点归产品引出、效果反馈归效果呈现、认证/机构背书归信任放大、购买指令归 CTA；未发现证据的阶段必须显式标为空缺或待复核。

约束：

- 包级 `__init__.py` 只 re-export `apply_postprocess_chain`，其他函数显式 import 子模块路径。
- KKM 等认证信息按功能唯一归 S5，不得跨阶段重复引用；即使与产品引出同画面或出现在开头，也不能替代 S2 的产品身份/解决方案承接。
- 新增市场或品类专项一律新建平级文件，不修改 `claims_my.py` / `health_rewrite.py`，保持每个文件单一规则集。

### 3.8 `prompt.py` — analysis_input.md 装配

职责：

- 把 analysis dict + 关键帧 manifest + 转写 + 翻译 + 商业评判框架 + 目标市场知识库 + `structure_library_full.md` + 内置三步分析流程装配成 LLM 输入包 `analysis_input.md`。
- 提供 `speech_status` / `render_*_markdown` 等 prompt 装配辅助。

说明：

- 从 `flayr.py` 拆出（解决 5.1 节标记的 risk）。
- 凝聚度：prompt 装配是独立子系统，与 harness 编排分离。
- 变更频率：prompt 内容每次 LLM 调优都要改；harness 几乎不动；二者频率差几个数量级。

### 3.9 `report.py` — HTML 报告

职责：

- 读取结构化 analysis 数据。
- 通过阶段引用的 `evidence_units` 时间段选取达人/标杆画面。
- 渲染 `report.html`。
- 报告资源只能解析到当前运行目录内的真实文件；解析会跟随并检查符号链接，拒绝目录外逃逸、外部视频 URI 和超出嵌入预算的图片。相同图片在单次报告内复用已编码结果，报告最终仍受 `max_report_bytes` 限制。

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

### 3.10 `utils.py`

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
  ├─ asr.py
  │   └─ 写 transcript.txt
  ├─ translation.py
  │   └─ 通过 llm.api 调模型，写 transcript.zh.txt
  ├─ video_evidence.py
  │   └─ 写 selection_report / contact_sheets / timeline_views / transcript_packed
  ├─ prompt.py
  │   └─ 写 analysis_input.md
  ├─ llm/pipeline.py
  │   ├─ llm/payload.py    构造请求
  │   ├─ llm/api.py        HTTP 调用 + 临时目录传输
  │   ├─ llm/analysis_contract.py  结果结构契约
  │   ├─ llm/parse.py      JSON 解析 + schema normalize
  │   ├─ Stage1-A/B/C/D     原子观察、首次资格、一次定向补观察、定向重投影
  │   ├─ Stage2 groups       S1+S2 / S3+S4 / S5 / S6 独立判断
  │   ├─ Stage3 synthesis    只读阶段结果的综合
  │   └─ postprocess/        投影、validate、resolver、局部 patch
  │       └─ finalizer 写 analysis_result.json / manifest
  ├─ flayr.py
  │   └─ 写出分析结果与报告
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

## 5. 近期建议

1. 保持当前 report-first 产品稳定，不急着接入 `compose.py`。
2. 继续保持 `analysis_result.json` 与报告 schema 分离，避免分析结果被展示层字段污染；新增或修改分析字段时，先更新 `references/analysis-output-schema.json` 与 `analysis_model.py` 的投影合同，再接入解析、后处理和报告。
3. ~~如果继续拆代码，优先拆 `prompt.py`，把 `analysis_input.md` 构造从 harness 中移出。~~（已完成）
4. 如果要外发报告，再做 HTML 图片内嵌或 report assets 打包。
5. 阶段目录已收口到 `stage_catalog.py`；真实阶段边界仍由模型按功能识别，目录中的时间仅用于预处理和报告占位回退。后续新增阶段或改回退窗口只能修改该目录。
6. `postprocess/claims_my.py` 和 `health_rewrite.py` 是市场/品类硬编码的妥协。未来扩品类/扩市场应新建平级文件（`claims_xx.py` / `xx_rewrite.py`），不要往现有文件塞；积累到 3-4 个后考虑抽象成 `references/category-policies/*.yaml` 配置层。
7. `validate_stage_ownership` 与 `validate_evidence_alignment` 内含 MY 市场 KKM 硬编码，未来抽到 `claims_my.py` 的 validate 区，让 `validate.py` 保持纯通用校验。

---

## 8. 视频级商业门控与商业优先级规范

视频级商业门控位于 S1-S6 之外，只识别会同时污染多个阶段的根本问题。它不得改写
`stage_analysis[].severity`，也不得删除阶段原判断；只能通过因果字段解释阶段问题受何种根因影响。

### 8.1 三个固定门控

1. `selling_point_route`：主卖点是否适合短视频证明，且是否真的给出对应证明信号。
2. `focus_coherence`：单品多 SKU/变体是否保持单一焦点，或形成清楚的比较与选择逻辑。
3. `attention_cleanliness`：是否存在持续抢占注意力、又不参与产品任务的高显著物体。

V1 不为单个品类维护专属证明目录或合理动作清单。卖点路线复用
`short_video_proof_plan` 和通用 `proof_mode`；合理动作由“是否参与产品任务”这一观察字段约束。

### 8.2 事实与判定边界

- Stage1 只输出观察事实：卖点画面/口播占比、证明信号、变体身份与占比、选择解释、注意力竞争物。
- 三项观察必须分别输出 `gate_observation_status=complete|unknown`；缺字段、数据形状不合法或未完成扫描只能是 `unknown`，不得由空数组推断为通过。注意力扫描还必须提交 `attention_scan_audit`，明确检查录音/拍摄设备和前景非任务物体；任一项可见却未给竞争物明细时仍为 `unknown`。
- `temporal_evidence_mode` 由实际请求能力写入：`full_temporal | focused_temporal | static_only | unknown`。
- `single_focus` 单元中，单个变体视觉占比达到 70% 才生成 `primary_variant_id` 并确认归属。
- `explicit_comparison` 可不设 `primary_variant_id`；只要至少两个变体身份明确且比较目的明确，归属仍可确认。
- 视觉占比与口播占比分开保存，不得互相替代。
- `variant_visual_shares` / `variant_speech_shares` 的 key 必须属于 `variant_ids`，数值在 0-1 且每侧总和不超过 1.05；不一致时归属无效。
- 静态证据不能证明持续运动或无序反复切换；证据不足输出 `unknown`，不得进入阻断结论。
- 单侧绝对判断使用该侧能力；比较判断使用两侧较弱的时序能力。

### 8.3 Impact 定义

- `blocking`：用户无法理解产品核心价值、无法判断核心证据属于哪一 SKU，或核心证明区域被持续遮挡。
- `major`：显著削弱理解或注意力，但产品核心价值仍可识别。
- `minor`：存在干扰或非首选路线，但没有造成核心信息丢失。
- `pass`：门控通过。
- `unknown`：证据不足；不进入商业优先级。

模型默认生成、低置信度的产品地基不得单独触发 `blocking`。卖点路线 P0 必须同时满足：
受信任的 `proof_contract_source=operator|curated`、运营/策展来源的高置信锚点、达人主路线偏离或未证明，
且 S3/S4 的绝对证明状态缺失或薄弱。模型自报 `selection_source` 不能抬升来源权限。

`primary_candidate_id` 表示整条短视频的商业主路线，可落在 S2-S5；`s4_anchor_candidate_id` 只负责 S4 的单一效果测量，二者不得混用。当前可信运营入口是 `--primary-selling-point`：它必须唯一对应 candidate 才能把来源提升为 operator。

### 8.4 因果标注

- `stage_analysis[].affected_by_global_issues`：该阶段受哪些全局根因影响。
- `improvements[].root_cause_ids`：该建议应追溯到哪些全局根因。
- 不做文本去重。报告保留阶段原结论，并提示应先处理根因。

### 8.5 确定性商业优先级

商业优先级只由 postprocess 计算，不接受模型自由排序：

1. P0：证据支持的全局 `blocking`。
2. P1：阶段差距 `large`。
3. P2：全局 `major`。
4. P3：阶段差距 `medium`。
5. P4：全局 `minor`。
6. P5：存在可执行建议的阶段差距 `small`。

同层全局门控顺序：卖点路线 > 焦点一致性 > 注意力洁净度；再按影响阶段数降序、置信度降序、稳定 id 排序。

同层阶段顺序：缺失/方向错误 > 证明无效 > 执行薄弱 > 细节问题；仍相同时按
S1 > S4 > S3 > S6 > S2 > S5 排序。

`commercial_priority_summary` 取排序第一项。报告在 S1-S6 之前展示可行动的全局根因；没有根因时不显示空区块。

### 8.6 S1-S6 跨模态综合合同

`landing_met` 在缺少跨模态字段的历史结果中继续服务 severity 兼容路径。新主分析启用跨模态综合合同后，
S1 执行质量由多渠道组合后的 `integrated_effect` 决定，不让 landing 二元值或四维命中数覆盖强视觉、
强口播等渠道间的合理补偿。

单一来源为 `scripts/flayr_core/multimodal.py`。主分析、Repair 与 Phase C 使用同一合同，分别保留
`visual`、`speech`、`text`、`sound_rhythm` 四个渠道的影响和证据，再输出：

- `dominant_channel`：真正承担该阶段核心任务的主导渠道。
- `cross_channel_relation`：增强、互补、中性、冲突或干扰。
- `integrated_effect`：渠道组合后的净效果，而不是最弱项或等权平均。
- `compensation_applied`：强渠道是否实际弥补了弱、缺失或轻度负向渠道。

模型负责在锁定事实内做跨渠道关系判断；代码负责枚举归一、证据归属、自洽门禁和阶段硬边界。
渠道可替代性不是六条散落规则，而是 `MULTIMODAL_CHANNEL_REQUIREMENTS` 中的一条统一轴：

| 等级 | 含义 | 阶段 |
|---|---|---|
| `any_channel_sufficient` | 任一渠道可完成信息传达，但仍须完成该阶段任务 | S1、S2 |
| `required_evidence_with_amplification` | 指定主证据必须成立，其他渠道只能增强 | S3、S4、S6 |
| `source_grounded` | 可信来源必须真实存在；清晰展示可增强，通用氛围不能补来源 | S5 |

各阶段必要信号为：

1. S1 任一渠道都可主导留人，强视觉可以补偿普通或缺失口播；冲突口播和显著干扰仍会降低净效果。
2. S2 必须完成产品身份、出现理由和 S1 承接，其他渠道只能增强表达。
3. S3 必须有真实使用过程与关键动作，口播、字幕、声音节奏不能替代演示。
4. S4 必须有可见效果，描述性口播不能把不可见结果说成成立。
5. S5 必须有可信来源和相关信任主张，氛围不能替代背书来源。
6. S6 必须有购买邀请或行动指令，价格、赠品、字幕和声音只能放大促单动作。

“存在”不按字面或单帧判断，也不新增统一秒数阈值。代码复用各阶段已校准的结构化事实：S3 的真实动作、
目标接触、应用变化与关键连续性；S4 的可见差异与模块约束；S5 的来源可信度、具体性、产品相关性和
可核验性；S6 的结尾购买邀请、行动路径和利益放大器。技术上闪现但观众无法有效接收的内容仍按弱执行处理。

`derive.py` 先按各阶段结构化 flags 得到硬条件执行分，再由跨模态净效果做融合；S1 直接使用净效果，
S3 只有真实过程闭环完整时才允许多渠道表现把基础演示提升为出色，其他阶段均不得越过既有硬条件。
没有新字段的存量结果保持旧路径。Phase C 更新阶段时先移除旧多模态结论，确保新切片必须产生新判断。
`severity_derivation.multimodal_integration` 保存两侧主导渠道、渠道关系、净效果和补偿状态，供评估与排错。
