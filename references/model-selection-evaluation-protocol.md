# Flayr 模型与事实链路评估协议

## 目的

这份协议把“模型判断错了”拆成可定位的事实层、判断层和运行层问题。它只服务于校准、机制回归和盲测，不会把评估结果写入生产 `analysis_result.json`，也不会把人工 GT 放进模型请求。

## 当前数据能支持的根因判断

1. **数据协议不统一。** 新 7 组使用 `human_initial` 的临时 severity 投影，`none` 被映射为 `small`；历史标签没有完整的方向字段。旧的单一 severity 分数不能区分“标杆更好”“达人更好”“双方持平”和“无实质差距”。
2. **判断层仍可能混入抽取误差。** 原有 `model_independent` 结果是“模型自己抽事实，再基于自己的事实判断”，它不是纯 judgment-only。抽取和判断必须分别留存，不能只看最终 severity。
3. **S4 是结构性弱点。** 已有共同样本中，人工标为 large 的 S4 没有被两个模型正确识别；这应先按“证据结构/效果证明不足”诊断，不能归结为单纯档位偏差。
4. **S5 的错误是混合类型。** 旧报告中的 Qwen3.6“0/5”不是同一种错误，其中同时存在方向错误、档位错误和评估口径差异；v2 重评分进一步确认其中有 `not_applicable` 与 legacy 合同表达缺口。以后不能只报告一个二元准确率。
5. **运行完整性独立于语义准确率。** 合同失败、截断和超时不能静默折算为 0 分，也不能从报告中消失；它们必须进入单独的 operational denominator。
6. **抽取合同存在人为上限风险。** 当前评估合同明确记录每阶段最多 4 个引用、每侧最多 12 个抽取单元、文本长度上限和输出预算。任何修改上限都必须做单变量对照实验，不能事后从失败样本反推模型能力。

这些结论的统计解释仍受样本量、GT 协议和 source commit 未完全冻结的限制。它们可以指导修复和诊断，不能直接作为生产模型 promotion 证据。

## 已有结果的正确解读

旧报告里的分数必须与当前 v2 重评分分开保存，不能把旧 projection 当成当前语义结论。

### 旧 projection（仅作历史对照）

| 人工 GT / 样本层 | Qwen3.6 | Qwen3.7 | 解释 |
|---|---:|---:|---|
| 新 7 组、38 个有效阶段格 | 20/38 = 52.63% | 17/38 = 44.74% | 相对干净的临时 GT 子集，但不是 blind promotion 证据 |
| 共同 9 组、50 个有效阶段格 | 24/50 = 48.00% | 22/50 = 44.00% | 4 个 S5 `not_applicable` 格已排除，方向标签仍不完整 |
| S4 中 GT=`large` 的 6 个格 | 两个模型均未命中 | 两个模型均未命中 | 结构性失败信号，不能被整体均值掩盖；该表使用旧 severity projection |

### v2 合同重评分（2026-08-06）

输入是同一批 9 组共同样本、54 个阶段格，其中 4 个 `not_applicable`，50 个 GT 有效格。
其中 36 个格可以进行语义差距比较，14 个格是旧 severity-only 结果无法表达 GT=`none` 的合同表达缺口。

| 指标 | Qwen3.6 | Qwen3.7 | 解释 |
|---|---:|---:|---|
| 语义差距准确率 | 16/36 = 44.44% | 12/36 = 33.33% | 排除 14 个合同表达缺口后的当前可比结果 |
| 合同感知差距准确率 | 16/50 = 32.00% | 12/50 = 24.00% | 把旧合同无法表达 `none` 计为不可表达错误 |
| 合同表达缺口 | 14/50 = 28.00% | 14/50 = 28.00% | 旧 artifact 的结构限制，不是模型语义错误 |
| 方向准确率 | 不可计算 | 不可计算 | 9 组 GT 没有可评分 `stage_relations` |

v2 输出协议为 `human_model_alignment_v2`。因此，48.00%/44.00% 仍可作为旧 projection
的历史参照，但当前根因分析和后续模型比较应使用语义差距准确率、合同表达缺口和运行状态
三个维度，不能只引用一个 topline 百分比。

S4 的结构性问题在 v2 中仍然清晰：6 个 GT=`large` 的格全部被两个模型漏掉；Qwen3.6
在 S4 的 7 个语义可比格中命中 1 个，Qwen3.7 命中 0 个。S5 的旧“0/5”也不能继续
作为单一结论引用：在当前 GT 中 4 个格是 `not_applicable`，剩余 5 个里 2 个属于旧
合同表达缺口，因此语义可比的分母是 3；Qwen3.6 为 0/3，Qwen3.7 为 1/3。

新 7 组的配对检验 `p≈0.549` 只能表示当前样本不足以区分两个模型，不能表示两个模型等价。共同 9 组的合计分数也不能直接用于生产选型，因为 GT 协议尚未完成冻结，且旧 artifact 与新合同版本并存。

## 根因到修复的映射

| 根因 | 可观测证据 | 修复层 |
|---|---|---|
| `none`、`small` 和方向被压成一个 severity | `human_gap` 中存在 `none`，旧 `stages` 只能输出 `small/medium/large` | GT loader 保留 `none`，模型输出拆成 `relation + gap_magnitude`，legacy 结果只能标记为 magnitude-only |
| 抽取错误与判断错误混在同一个最终分数 | model-independent 旧结果同时包含模型自抽事实和判断 | 保存 raw extraction、锁定事实包、再运行独立 judgment-only，并分别统计 |
| S4 效果证明不足被整体准确率掩盖 | 6 个 GT-large S4 格均未被两个模型识别 | S4 单阶段 recall、`proof`、`causal_link`、`visibility` 和错误类型单独输出 |
| S5 错误不是单一类型 | 方向错误、档位错误、口径差异同时存在 | 方向、大小、两轴同时错误分别计数；S5 继续 audit-only |
| 详细模型更容易撞隐藏上限 | 每阶段 4 条引用、每侧 12 单元曾导致合同失败 | 上限同时写入 prompt、validator、metadata；变更前做单变量对照 |
| 运行失败被误读成语义错误 | 截断、超时、合同失败与准确率混在一起 | `failure_class`、合同错误码、运行分母独立记录；失败不折算语义 0 分 |
| 粗粒度或边界外时间信息污染事实 | SRT/窗口泄漏和跨阶段引用 | 保留 raw 审计数据，消费侧只使用 window-safe 数据；阶段/时间匹配只作为代理指标，并允许 `null` |

## 冻结后的执行顺序

1. **冻结 GT。** 人先完成 `human_initial`，明确每个阶段的总体差距、方向、大小、关键理由、`not_applicable` 和 `uncertain`；模型结果在 GT 冻结前不得反馈给标注者。
2. **先验事实对齐。** 用同一批视频和同一 source identity 对每个模型的 raw extraction 做阶段/时间代理对齐；有 `key_events` 才计算召回与代理精确率，没有就输出 `null`。
3. **再测判断。** 将同一模型产出的、通过 v2 合同的事实包锁定后喂给 judgment-only；同时保留端到端 model-owned facts -> judgment 结果，避免把两种能力混为一谈。
4. **分歧复核。** 模型理由只是待核实线索。模型提出人工未记录的新事实时，回视频独立核实；纯权重分歧进入第二人工盲标，不用“谁说得更像真的”裁定。
5. **最后才谈选型。** 只有 fresh blind cohort 同时满足事实召回、方向/大小准确率、S3/S4 结构指标、运行完整性、成本和稳定性门槛，才允许设计 promotion；当前脚本永久写 `promotion_eligible: false`。

### S4 两步归因与 S5 audit

S4 的低分先通过隔离实验拆成两层，不直接改生产 severity：

1. `s4_fact_state`：只基于锁定的 Stage1 facts，分别输出 creator/benchmark 的
   `effect_evidence_state`、`visibility`、`proof`、`causal_link` 和同侧 S4 evidence IDs；
2. `s4_judgment`：读取同一模型、同一 source digest 的已完成第一步产物，只输出
   `relation`、`gap_magnitude`、`confidence` 和简短 `decision_basis`，不能重新抽事实或改写状态。

第一步产物必须带 `source_digest` 和 `model`，第二步会拒绝跨运行或跨模型拼接。两步结果都是
`promotion_eligible: false` 的诊断 artifact，不会写入 `analysis_result.json` 或触发 resolver。

S5 使用独立的 `s5_audit` 合同，明确区分 `explicit_absence`、`product_claim_or_offer`、
`credible_source` 和 `uncertain`。缺字段不能变成 `explicit_absence`，只有产品主张/优惠也不能
冒充独立可信来源；S5 仍保持 `audit-only`。

对应的单样本入口是：

```text
scripts/evaluate_compact_model.py --variant s4_fact_state ...
scripts/evaluate_compact_model.py --variant s4_judgment --s4-state-path ... ...
scripts/evaluate_compact_model.py --variant s5_audit ...
```

批量运行时，`s4_judgment` 的 `--s4-state-root` 必须按
`<sample_id>/<model>/s4_fact_state_evaluation.json` 提供第一步产物。没有通过状态、运行身份和
模型身份校验的产物不得进入第二步。

生产结果的既有 S4 四态和 Batch B `evidence_strength` 仍由生产 validator/derive 负责；本节新增
的是可回放的分层诊断链，不把未经 blind 验证的诊断结果直接提升为生产规则。

历史 v1 artifact 可以用于诊断和兼容读取，但不能与 v2 artifact 静默合并成“当前模型表现”。需要重新运行的地方必须显式标记 schema、source commit、source identity 和协议 hash。

当前 9 组重评分中的 `carslan-b0`、`tashadiyana` 仍来自旧 calibration severity 标签，缺少
完整 `human_initial` 和 `stage_relations`；它们只能用于兼容性/差距大小诊断，不能被误写成
完全同协议的 blind GT。`youkoubo-c0/S3` 与 `are_xie/S5` 的永久回归 fixture 仍需在
`clean_current` 输入上完成明确的重新核验；本次评分合同修复不会替代那项 fixture 复核。

## 冻结链路

```text
原始视频
  -> 统一采集：视觉、在线 ASR、OCR、时间边界
  -> canonical fact pack
       ├─ 与人工 key_events 对齐：召回与阶段/时间代理精确率
       ├─ 同一份锁定事实喂给 judgment-only 模型
       └─ 保留 model-owned facts -> judgment 的端到端链路
人工 GT
  -> 只进入离线 evaluator，不进入任何模型 prompt
```

### Blind GT 合同

新 blind cohort 的阶段标签必须以 `human_gap` 和 `stage_relations` 为权威来源；旧的
`stages` 只能作为兼容投影，不能作为 blind 评分输入。每个有效阶段同时记录：

```text
human_gap       = none | small | medium | large | uncertain | not_applicable
stage_relations = benchmark_better | creator_better | tie | uncertain
```

`not_applicable` 和 `uncertain` 必须在 `stage_label_statuses` 中显式标记并说明原因。
如果同时保留兼容字段 `stages` 或 `relations`，它们必须分别与 `human_gap` 和
`stage_relations` 一致；`stage_oracles.relation` 也必须与阶段方向一致。每个
`top_root_causes` 必须至少引用一条 `key_event`。
可评分的 `none` 只能与 `tie/uncertain` 组合；非 `none` 的差距不能与 `tie` 组合。
每个有效阶段还必须有单侧执行 oracle、决策事件引用和理由。事件时间范围必须是有限、
非负且 `start < end` 的秒数区间；`expected_state=absent` 的事件必须带有 `terms_any`，
用于统计模型是否错误地声称该事实存在。

`stage_relations` 只描述“哪一侧更好/是否持平”的最终比较方向；它不替代 evidence_id 的
阶段归属、时间重叠或 `evidence_temporal_mismatch` 检查。后者仍是证据事实层的独立时序
诊断，两者是互补关系，不是同一字段的两种写法。

### 阶段标签

新评估使用两个独立轴：

```text
relation       = benchmark_better | creator_better | tie | uncertain
gap_magnitude  = none | small | medium | large | uncertain
```

旧 `severity` 结果仍可离线读取，但只能作为 legacy magnitude；它不能表示 `none`，也不能推导方向。对于 `none`、`not_applicable`、`uncertain` 和缺失标签，评估器保持显式状态，不自动投影成 `small`。

### 事实质量

新的 raw-video extraction 合同要求每个 evidence unit 带 `fact_quality`：

```text
subject       = correct | incorrect | uncertain | not_applicable
visibility    = clear | partial | obscured | uncertain | not_applicable
composition   = central | supporting | weak | uncertain | not_applicable
completion    = complete | partial | none | uncertain | not_applicable
proof         = direct_comparison | result_only | claim_only | none | uncertain | not_applicable
causal_link   = supported | weak | unsupported | uncertain | not_applicable
```

S3 重点看 `subject`、`visibility`、`composition`、`completion`；S4 重点看 `visibility`、`proof` 和 `causal_link`。这些字段是事实质量诊断，不直接替代人工 GT severity。

## 统计口径

### Judgment

- `gap_accuracy`：v2 语义差距准确率；只在 GT 为有效 `none/small/medium/large`、模型有可解析 gap，且不是 legacy severity-only 无法表达 `none` 的格子计算。
- `contract_aware_gap_accuracy`：合同感知差距准确率；保留所有可解析的 GT/模型 gap，并把 legacy severity-only 对 GT=`none` 的格子作为合同表达错误。
- `contract_representation_gap_rate`：GT 有效格中，模型结果因旧 severity-only 合同无法表达 `none` 的比例。
- `relation_accuracy`：只在 GT 提供合法 relation、模型提供合法 relation 的格子计算。
- `exact_direction_and_gap_accuracy`：只在两个轴都可评分的格子中计算方向和大小同时正确。
- 每阶段输出 `semantic_gap_accuracy`、`relation_accuracy`、错误类型分布和 `gt_large_recall`；`gt_large_unavailable_cells` 单独记录模型失败或不可解析，不把它静默算成漏判。
- `direction_error`：方向错、大小对。
- `magnitude_error`：大小错、方向对或 GT 未提供方向。
- `direction_and_magnitude_error`：两个轴都错。
- `contract_representation_gap`：legacy severity-only 输出无法表达 GT 的 `none`。
- `prediction_unavailable`：模型对必需轴返回 `uncertain`、缺失或不可解析；这不是语义错误，另计入模型可用性和运行分母。
- 模型失败、合同失败、超时、缺失输出进入 `model_failed_or_missing_cells`，不伪装成语义错误。

所有结果写入 GT cell 总数、有效 GT 数、NA、uncertain、缺失、模型失败和实际 scored denominator。
方向轴另行记录 GT 缺失、uncertain、非法值和可评分格子，避免把“没有方向标签”误报为模型方向判断错误。

### Extraction

人工 `key_events` 与模型 evidence unit 通过 `role + stage function + 正时间重叠`做机械匹配。输出指标明确命名为：

- `temporal_stage_recall_proxy`
- `temporal_stage_precision_proxy`

`expected_state=present` 的事件进入召回分母；`expected_state=absent` 的事件单独进入
`absence_respected_rate`，不会被当成“待召回事实”。

它们只能说明阶段/时间覆盖，不能证明文本语义真实。真正的语义精确率仍需人工复核。工具同时输出每阶段事实质量覆盖率及 S3/S4 的字段分布。

S3 的阶段质量诊断重点是 `subject`、`visibility`、`composition`、`completion`；S4 的重点是
`visibility`、`proof`、`causal_link`。这些质量字段用于定位抽取事实的缺口，不替代人工阶段
差距标签。

模型失败、合同失败或缺失产物不会进入抽取召回/精确率的可评分分母；它们只计入 `model_failure_or_missing`，全部人工事件仍保留在 `required_key_events` 中供审计。

### 配对源身份

抽取结果与 model-independent judgment 只有在原始抽取源摘要、视频角色顺序和视频文件
SHA256 全部一致时，才允许进入配对语义汇总。模型独立判断产物自身的 `source_digest`
是派生事实包摘要，不能直接当作原始视频摘要；评估器通过输入旁车中的
`source_extraction` 对账原始源身份。身份不匹配或身份信息不完整的样本保留在逐样本审计中，
但从语义准确率、召回率和精确率汇总中排除；缺失/失败产物仍按运行失败统计。

## 合同上限

诊断实验中的数值限制必须同时出现在该实验的 prompt、validator 和
`compact_request_metadata.json`；它们不自动成为生产主链路的限制：

- 输出预算：运行时实际 `output_budget`；
- 紧凑评估变体中每阶段每侧最多 4 个 `evidence_ids`；
- 紧凑评估变体中每个角色最多 12 个抽取单元；
- 每条抽取 `information` 最多 240 字；
- reason、rationale、decision basis 的字符上限。

生产主链路不使用未声明的固定证据数量上限：Stage1 应覆盖时间线上的关键观察，
Stage2 只能引用已经资格化的阶段证据；只有各字段合同明确声明的长度、传输和总预算
才构成硬限制。若输出预算导致事实覆盖不足，必须写入 `evidence_budget_exceeded`
并进入恢复或未决路径，不能通过静默截断伪装成完整证据。

`compact_eval` 脚本仍支持显式传入 `--max-stage-evidence-ids 8` 作为诊断实验参数，
但这不会改变生产主链路，也不能与旧结果混用。4→8 实验必须使用同一输入、同一模型
参数，只改变上限，并同时报告样本级合同失败率、单条引用错误率、新增引用的事实覆盖
情况和判断结果变化；在实验完成前，不得据此修改生产默认值。提高 12 也遵循同一原则。
旧结果不因新合同自动重写或重新判定。

## Promotion 边界

当前离线评估脚本固定写入 `promotion_eligible: false`。只有在以下信息都齐全后，才允许另行设计 promotion：

1. 同一批样本的 frozen 视频身份、GT 协议、代码提交和 prompt/schema hash；
2. 多品类、多样本的全新 blind cohort；
3. 事实召回/代理精确率、方向、gap、运行完整性和成本/延迟同时达标；
4. S3/S4 的大档失败不再被一个整体准确率掩盖；
5. 模型抽取与 judgment-only 的收益能够分别归因。

## 工具

```text
scripts/evaluate_human_model_alignment.py
```

该工具只读保存的模型 artifact 与人工 GT，不发起网络请求。它可以同时评估 raw extraction 和 model-independent judgment；没有人工 `key_events` 时，会把抽取召回率和阶段/时间精确率代理都输出为 `null`，不会把模型单元数量当成召回率或错误率。

对已经生成的生产分析结果，可使用以下离线审计入口，不发起模型调用：

```text
scripts/audit_analysis_chain.py --manifest <manifest.json> --output <audit.json>
```

它只报告 S4 硬事实冲突、S4 evidence 时间/阶段归属错误、S5 来源状态分布和
`evidence_strength` gate 状态，不修改任何原始结果。
