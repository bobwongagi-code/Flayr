# Flayr 模型与事实链路评估协议

## 目的

这份协议把“模型判断错了”拆成可定位的事实层、判断层和运行层问题。它只服务于校准、机制回归和盲测，不会把评估结果写入生产 `analysis_result.json`，也不会把人工 GT 放进模型请求。

## 当前数据能支持的根因判断

1. **数据协议不统一。** 新 7 组使用 `human_initial` 的临时 severity 投影，`none` 被映射为 `small`；历史标签没有完整的方向字段。旧的单一 severity 分数不能区分“标杆更好”“达人更好”“双方持平”和“无实质差距”。
2. **判断层仍可能混入抽取误差。** 原有 `model_independent` 结果是“模型自己抽事实，再基于自己的事实判断”，它不是纯 judgment-only。抽取和判断必须分别留存，不能只看最终 severity。
3. **S4 是结构性弱点。** 已有共同样本中，人工标为 large 的 S4 没有被两个模型正确识别；这应先按“证据结构/效果证明不足”诊断，不能归结为单纯档位偏差。
4. **S5 的错误是混合类型。** Qwen3.6 的 0/5 不是同一种错误，其中同时存在方向错误、档位错误和评估口径差异。以后不能只报告一个二元准确率。
5. **运行完整性独立于语义准确率。** 合同失败、截断和超时不能静默折算为 0 分，也不能从报告中消失；它们必须进入单独的 operational denominator。
6. **抽取合同存在人为上限风险。** 当前评估合同明确记录每阶段最多 4 个引用、每侧最多 12 个抽取单元、文本长度上限和输出预算。任何修改上限都必须做单变量对照实验，不能事后从失败样本反推模型能力。

这些结论的统计解释仍受样本量、GT 协议和 source commit 未完全冻结的限制。它们可以指导修复和诊断，不能直接作为生产模型 promotion 证据。

## 已有结果的正确解读

当前能复核的配对结果应分层阅读：

| 人工 GT / 样本层 | Qwen3.6 | Qwen3.7 | 解释 |
|---|---:|---:|---|
| 新 7 组、38 个有效阶段格 | 20/38 = 52.63% | 17/38 = 44.74% | 相对干净的临时 GT 子集，但不是 blind promotion 证据 |
| 共同 9 组、50 个有效阶段格 | 24/50 = 48.00% | 22/50 = 44.00% | 4 个 S5 `not_applicable` 格已排除，方向标签仍不完整 |
| S4 中 GT=`large` 的 6 个格 | 两个模型均未命中 | 两个模型均未命中 | 结构性失败信号，不能被整体均值掩盖 |

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

历史 v1 artifact 可以用于诊断和兼容读取，但不能与 v2 artifact 静默合并成“当前模型表现”。需要重新运行的地方必须显式标记 schema、source commit、source identity 和协议 hash。

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

- `gap_accuracy`：只在 GT 为有效 `none/small/medium/large`、模型有可解析 gap 的格子计算。
- `relation_accuracy`：只在 GT 提供合法 relation、模型提供合法 relation 的格子计算。
- `exact_direction_and_gap_accuracy`：方向和大小同时正确。
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

它们只能说明阶段/时间覆盖，不能证明文本语义真实。真正的语义精确率仍需人工复核。工具同时输出每阶段事实质量覆盖率及 S3/S4 的字段分布。

模型失败、合同失败或缺失产物不会进入抽取召回/精确率的可评分分母；它们只计入 `model_failure_or_missing`，全部人工事件仍保留在 `required_key_events` 中供审计。

## 合同上限

所有数值限制必须同时出现在 prompt、validator 和 `compact_request_metadata.json`：

- 输出预算：运行时实际 `output_budget`；
- 每阶段每侧最多 4 个 `evidence_ids`；
- 每个角色最多 12 个抽取单元；
- 每条抽取 `information` 最多 240 字；
- reason、rationale、decision basis 的字符上限。

提高 4 或 12 之前，必须做同一输入、同一模型参数、只改变上限的对照，并同时报告样本级失败率和单条错误率。旧结果不因新合同自动重写或重新判定。

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
