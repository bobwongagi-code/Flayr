# CREAMY 事实修订后的四份判断

2026-09-23，零调用复核。人工参照为 S3 `benchmark_better/small`、S4 `benchmark_better/medium`；原运行 DEGRADED，人工记录为 `draft_visible/pending`，本表仅用于定位问题，不作生产准确率统计。

| 已保存响应 | S3 | S4 | S4 对达人效果的实际表述 |
| --- | --- | --- | --- |
| [原事实 r1](../runs/diagnostics/joint-cohort-audit-20260907/fact-probe-preflight/paired-live/01_creamy_c2_baseline-r1/response.json) | `benchmark_better/small`；达人使用状态 `complete` | `benchmark_better/small`；达人 `verified/clear`，标杆 `verified/strong` | 达人有“自然红润妆效”，只是部位、色号展示较少。 |
| [修订事实 r1](../runs/diagnostics/joint-cohort-audit-20260907/fact-probe-preflight/paired-live/02_creamy_c2_revised-r1/response.json) | `benchmark_better/large`；达人使用状态降为 `partial`，理由引用“红润度提升不明显” | `benchmark_better/large`；达人 `result_only/subtle`，标杆 `verified/strong` | 明确说达人有双颊妆效，但妆色淡、提色不明显；没有写成“完全无效果”。 |
| [原事实 r2](../runs/diagnostics/joint-cohort-audit-20260907/fact-probe-preflight/paired-final-live/02_creamy_c2_baseline-r2/response.json) | `benchmark_better/small`；达人使用状态 `complete` | `equivalent/none`；双方都 `verified/clear` | 认为双方都有清晰红润妆效、整体相当。 |
| [修订事实 r2](../runs/diagnostics/joint-cohort-audit-20260907/fact-probe-preflight/paired-final-live/01_creamy_c2_revised-revised-final-r2/response.json) | `benchmark_better/medium`；理由再次引用“红润度提升不明显” | `benchmark_better/large`；此响应未输出 S4 结构化状态 | 说达人妆色淡、提色不明显，同时把差距称为“质变级”；仍未声称完全无效果。 |

两次原事实请求的 SHA256 同为 `eb1f7053` 开头，两次修订事实请求同为 `d6915ee1` 开头。两臂的请求 diff 没有改判断指令，只改了 C2/C3 的动作和妆效描述，以及 S3/S4 对应资格理由：补记眼皮涂抹，把“红润明显”改为“红润提升不明显”。因此它不是只改变一个画面事实的实验；两次重复也发生在不同执行批次，不能据此量化因果收益。

修订 r1 还有一处内部矛盾：达人 S4 被标为 `effect_evidence_state=result_only`，但同一对象同时为 `result_only_without_process=false`、`process_linked_effect=true`、`effect_attribution_supported=true`。按[当时的 S4 状态定义](analysis-output-schema.json)，`result_only` 表示结果存在但缺少因果桥；该响应又声明因果桥存在。冻结版本的 `validate_s4_effect_flags` 校验枚举、布尔值和证据 ID，未检查这一组语义关系。修订 r2 没有 S4 结构化字段，不能声称两次都复现了同一资格状态。

后处理的硬事实检查其实已把 `result_only` 与 `process_linked_effect=true`、`result_only_without_process=false` 记为 `state_conflict`，但该标记只阻止确定性 floor 上调，不能拒绝模型原有的 `large`；因此上段所说的缺口是**最终发布门禁**，不是完全没有冲突检测。零调用扫描本地 51 份 `analysis_result.json`，另找到 PAPA FEEL C1 原件及其重放件两份产物也有 `result_only` 与 `process_linked_effect=true` 同存；它们是同一案例，不是两条独立复现。旧产物只作历史审计，不回写旧基线。

## 裁决

四份响应不支持“模型把有限效果直接当成完全不存在”：修订后的两次理由都承认达人有较淡的妆效。它们显示了两个尚未拆开的变化：修订 r1 先把达人的 S4 资格从 `verified` 降到内部自相矛盾的 `result_only`，随后给 `large`；S3 也把效果弱带进使用阶段，第一次把 `complete` 降成 `partial`。第二次仍给 S4 `large`，却未输出资格字段。由此不能裁定是资格降级还是幅度映射单独造成了错误，更不能用本例直接拟合一条新的 medium/large 规则。

本例不批准修改 Stage2 定档规则或加入新判例。`result_only` 与因果桥标志的矛盾是可独立检查的技术合同缺口；若修校验，应单独验证它只拒绝自相矛盾的响应，并明确这只能提高产物可信度，不能算作准确率提升。

后续技术修复 `0fee1a2` 已只对上述两种确定性矛盾增加最终质量拒绝，`ffe7388` 在干净工作树重新冻结。用保存的原事实 r1 与修订事实 r1 零调用核对，前者通过、后者被拒绝；不回写四份原始响应，也不据此重算业务准确率。
