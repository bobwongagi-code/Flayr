# Flayr 分析结果 QA 规则

> v2.0 | 2026-05-31
>
> 校验对象：`analysis_result.json` 归一化后的结构化结果。
>
> 校验时机：LLM 返回后、写入 `analysis.json` 和渲染 `report.html` 之前。
>
> 当前实现位置：`scripts/flayr_core/prompt.py`、`scripts/flayr_core/llm/payload.py`、`scripts/flayr_core/llm/parse.py`、`scripts/flayr_core/llm/pipeline.py`、`scripts/flayr_core/postprocess/validate.py`、`scripts/flayr_core/postprocess/repair.py`、`scripts/flayr_core/postprocess/claims_my.py`、`scripts/flayr_core/postprocess/health_rewrite.py`。
>
> 字段唯一真相源：`references/analysis-output-schema.json`。

---

## 1. 当前 QA 流程

Flayr 现在不是单独的 `qa_check(result, meta)` 架构。当前路径是：

```text
LLM raw JSON
  -> parse_json_text
  -> normalize_analysis_result
  -> apply_postprocess_chain
  -> validate_evidence_alignment / validate_stage_ownership
  -> health safety validate
  -> clamp_result_time_ranges
  -> validate_quality_contract
  -> analysis_result.json
```

`QA-RULES.md` 现在有两种生效方式：

- prompt 自检：`prompt.py` 和 `llm/payload.py` 会把本文件写入分析输入，要求模型输出前自检。
- 代码自检：`pipeline.py` 在最终写出前调用 `validate_quality_contract`，阻断硬错误并写入软警告。

`SystemExit` 表示阻断错误，会触发一次 LLM repair；普通修补函数只修改 data 后继续；软问题进入 `qa_warnings`，报告仍可输出。

---

## 2. 规则状态定义

| 状态 | 含义 |
|------|------|
| 已阻断 | 当前代码会抛 `SystemExit`，触发 repair 或失败 |
| 已修补 | 当前代码会确定性改写结果，不触发 repair |
| 已警告 | 当前代码写入 `qa_warnings`，不阻断 |
| 待实现 | 文档已定义，但还没有稳定实现 |

## 2.5 规则准入与防过拟合

人工 ground truth 是告警信号，不是直接写进代码的法律。任何从单个案例复盘得到的新规则，进入主流程前必须先过准入门槛，避免为了压中已知样本而破坏泛化。

### 2.5.1 新规则准入门槛

新规则至少满足以下条件，才允许影响 severity：

1. 概念先于样本成立：规则必须能用短视频结构、用户决策或证据链逻辑解释，而不是只解释某一条视频为什么错。
2. 适用范围清楚：规则要么覆盖至少 3 个品类/内容类型，要么明确声明为某类内容的专用规则。
3. 反例保护明确：必须写清什么情况下不触发，避免把正常创作方式误判为差距。
4. 证据可复核：报告里必须能看到支撑该规则的画面、口播、字幕或结构化事实。
5. 留出样本验证：不得只用产生该规则的 ground truth 样本验收，必须用未参与规则设计的 holdout 样本验证。

### 2.5.2 规则升级路径

新观察按以下顺序进入系统：

1. trace 字段：先记录事实或风险信号，不改变 severity。
2. warning/report 字段：确认对阅读报告有帮助后，可进入 `qa_warnings` 或报告提示。
3. severity 规则：只有在 holdout 样本上稳定提升判断质量，且误伤率可接受时，才进入执行分、下限或红线。

### 2.5.3 ground truth 使用边界

- ground truth 可以用来发现漏判模式、设计候选字段、定义验收样本。
- ground truth 不得单独决定阈值、权重、红线或阶段边界。
- 如果一个规则需要反复加例外才能压中 ground truth，应退回 trace 层，重新验证概念是否成立。

### 2.5.4 当前 S3 evidence-reception 规则状态

`process_framing_met` 不是“画面必须拍全”的打分规则，而是 S3 使用过程证据是否可接收的候选观察字段。它当前只作为 trace/审计字段，不直接影响 S3 severity。

设计边界：

- 不以单个样本作为规则来源。`kakwanreview` 只能作为告警样本，不能单独决定字段、阈值或下限。
- 不惩罚合理局部特写。美妆上脸、护肤涂抹、清洁局部刷洗、维修细节都可能需要局部镜头；问题不是“没拍全”，而是用户是否能看清产品作用对象、关键动作、卖点发生区域。
- 不用形态代替功能。完整远景不天然高分，局部特写不天然低分；必须回到 S3 功能：产品如何被真实使用、核心卖点如何在动作中被证明。
- 升级前必须做 holdout 验证。至少覆盖清洁、美妆/护肤、小家电、儿童/日用品等多类样本，并检查误伤：合理局部特写不得被误判，弱画面不得因“动作存在”被误判为强演示。

如果后续拆分该字段，只能先进入 trace，例如：

- `target_context_visible`：使用对象/场景上下文是否足以理解产品作用对象。
- `key_action_trackable`：关键动作是否连续、清楚、非严重运动模糊。
- `proof_area_visible`：核心卖点发生的区域是否清楚可见。
- `local_closeup_only`：是否只有局部特写且缺少必要上下文。

这些字段必须先用于报告审计与错误分析；只有跨样本证明稳定有效，才允许进入 warning 或 severity。

---

## 3. P0 结构契约

### Q01 顶层结果必须可归一化

规则：

- `analysis_result` 必须是 JSON object。
- 必须能归一为 schema 所需的顶层字段：`one_line_verdict`、`one_line_summary`、`executive_summary`、`holistic_assessment`、`key_conclusions`、`product_visibility`、`loop_closure`、`video_understanding`、`stage_analysis`、`improvements`。
- `stage_analysis` 必须为 6 项。
- `improvements` 必须为 1 到 5 项。

处理：已阻断。

实现位置：

- `scripts/flayr_core/llm/parse.py::parse_json_text`
- `scripts/flayr_core/llm/parse.py::normalize_analysis_result`

验收：

```bash
python3 -m py_compile scripts/flayr_core/llm/parse.py
python3 scripts/dev_test_stage2.py runs/20260531-143521-improve --dry
```

### Q02 stage_analysis 顺序固定为 S1-S6

规则：

- 阶段必须按 `S1 Hook`、`S2 产品引出`、`S3 使用过程`、`S4 效果呈现`、`S5 信任放大`、`S6 CTA` 输出。
- 不允许缺 S5；没有独立内容时也要保留阶段，并明确 `missing` 或无独立设计。

处理：已阻断。

实现位置：

- `scripts/flayr_core/llm/parse.py::normalize_analysis_result`
- `scripts/flayr_core/postprocess/validate.py::validate_evidence_alignment`
- `scripts/flayr_core/postprocess/validate.py::validate_quality_contract`
- `scripts/flayr_core/postprocess/validate.py::validate_module_ids`

### Q03 字段缺失先兜底，再由 warnings 暴露

规则：

- 部分文本字段缺失时允许 `normalize_analysis_result` 填占位，避免报告崩。
- 关键分析维度缺失时写入 `qa_warnings`。

处理：已警告。

实现位置：

- `scripts/flayr_core/llm/parse.py::required_text`
- `scripts/flayr_core/postprocess/validate.py::validate_analysis_dimensions`

后续改进：

- 如果报告已稳定，可以逐步把 `Q03` 中的核心字段从警告提升为阻断。
- 闭环是否已审计以代码生成的 `computed_loop_closure.source=proposition_trace` 为准；
  旧 `loop_closure.note` 只做兼容展示。`audit_status=partial|broken` 表示审计发现断点，不能误报为“缺少审计”。

---

## 4. P0 证据契约

### Q04 evidence_units 是唯一事实源

规则：

- 阶段二不得新增、删除或改写 `video_understanding.{benchmark,creator}.evidence_units`。
- Phase C 回看也只能修 `stage_analysis`，不能改 facts。

处理：已阻断 + prompt 约束。

实现位置：

- `scripts/flayr_core/llm/pipeline.py::_process_llm_result`
- `scripts/flayr_core/llm/payload.py::build_llm_comparison_payload`
- `scripts/flayr_core/llm/payload.py::build_stage_review_payload`

### Q05 每个阶段必须引用存在的 evidence_id

规则：

- 每个 stage 都必须有 `benchmark_evidence_ids` 和 `creator_evidence_ids`。
- 引用 id 必须存在于对应视频的 `evidence_units`。

处理：已阻断。

实现位置：

- `scripts/flayr_core/postprocess/validate.py::validate_evidence_alignment`

### Q06 阶段引用证据必须与阶段时间相交

规则：

- 非静音视频中，阶段引用的 evidence_unit 时间必须与该阶段 `benchmark_time_range` / `creator_time_range` 相交。
- 静音或无有效口播视频允许用占位证据表达“该阶段仅画面/字幕支撑”。

处理：已阻断 + 已修补。

实现位置：

- 阻断：`scripts/flayr_core/postprocess/validate.py::validate_evidence_alignment`
- 修补：`scripts/flayr_core/postprocess/repair.py::materialize_spoken_stage_evidence`
- 修补：`scripts/flayr_core/postprocess/repair.py::fill_missing_evidence_references`

### Q07 口播归属以 transcript.srt 为准

规则：

- 阶段 quote 必须来自该视频转写。
- 不得把标杆口播写到达人，或把达人口播写到标杆。
- 有时间戳口播时，阶段边界必须服务于真实口播时间，而不是固定 0-3s / 3-6s。

处理：已阻断 + 已修补。

实现位置：

- 阻断：`scripts/flayr_core/postprocess/validate.py::validate_transcript_attribution`
- 修补：`scripts/flayr_core/postprocess/repair.py::bind_timed_transcript_quotes`
- 修补：`scripts/flayr_core/postprocess/repair.py::deduplicate_stage_quotes`

### Q08 画面证据必须对齐引用的 evidence_unit

规则：

- `creator_visual_evidence` / `benchmark_visual_evidence` 必须来自被引用 evidence_unit 的 `visual_fact` / `subtitle_fact`。
- 口播提及但画面不能验证时，必须标记 `voice_only`，不能写成画面已展示。

处理：已修补。

实现位置：

- `scripts/flayr_core/postprocess/repair.py::ground_stage_visual_evidence`
- `scripts/flayr_core/postprocess/repair.py::downgrade_unverified_sensitive_claims`

### Q09 time_range 必须可解析且不越界

规则：

- 阶段、提升点、基底帧时间必须能解析为秒数。
- 时间不得超出对应视频时长。
- 阶段编号表示功能职责，不等于六段严格不重叠的时间轴：S1/S2 可在承接点重叠，S3/S4 可共用同段过程与效果证据，S5 信任材料可出现在任意位置但只归 S5。
- S2/S3 大面积重叠时必须用 `merged_with_s3=true` 明确声明；否则提示复核边界。

处理：已阻断 + 已警告 + 已修补。

实现位置：

- 已修补：`scripts/flayr_core/postprocess/repair.py::clamp_result_time_ranges`
- 阻断/警告：`scripts/flayr_core/postprocess/validate.py::validate_quality_contract`
- 阻断/警告：`scripts/flayr_core/postprocess/validate.py::validate_stage_time_coherence`
- 阻断/警告：`scripts/flayr_core/postprocess/validate.py::validate_product_visibility`

---

## 5. P0 商业判断契约

### Q10 severity 必须先有依据再定级

规则：

- `gap` 必须表达：达人做了什么 -> 标杆做了什么 -> 对购买意愿的影响。
- severity 按购买意愿影响定级，不按画面差异大小定级。

标尺：

| severity | 判断锚点 |
|----------|----------|
| large | 直接影响购买意愿的硬伤，如 Hook 留不住人、核心卖点讲错、CTA 缺失 |
| medium | 削弱说服力但不致命，如卖点讲了但不突出、场景代入不足 |
| small | 细节瑕疵、达人持平或达人更优 |

处理：prompt 约束 + 已修补。

实现位置：

- `ANALYSIS-PROMPT.md`
- `scripts/flayr_core/llm/payload.py::build_llm_payload`
- `scripts/flayr_core/postprocess/repair.py::stabilize_stage_severity`

### Q11 达人持平或更优时 severity 必须是 small

规则：

- gap 写“无明显差距”“达人略优”“达人更直接”等含义时，不得给 `medium` 或 `large`。
- 标杆无独立 CTA、达人有明确购买指令时，S6 判 `small`。

处理：已修补。

实现位置：

- `scripts/flayr_core/postprocess/repair.py::stabilize_stage_severity`

### Q12 S3 只判断 how-to，感官效果归 S4

规则：

- S3 只回答用户能不能看懂怎么用。
- 闻香、口味、膏体质感、前后效果、孩子反应等归 S4。

处理：已修补。

实现位置：

- `scripts/flayr_core/postprocess/repair.py::stabilize_stage_severity`

### Q13 Top 提升点必须跟随最终商业判断

规则：

- `improvements` 不能把达人优势阶段列为高优先级。
- CTA 如果最终判为 `small`，不应再把 CTA 作为 Top 改进。
- 排序优先级按最终 stage severity 和 GMV 杠杆收敛。
- 最终为 `large` 的阶段必须至少有一条对应提升点；模型建议早于确定性推导生成而发生漏项时，先告警，不得静默让 medium 建议压过 large 缺口。

处理：已修补。排序由确定性后处理完成；最终 `large` 缺项由一次纯文本补全补齐，失败保持主分析可用并保留 Q13。

实现位置：

- `scripts/flayr_core/postprocess/repair.py::stabilize_improvement_priorities`
- `scripts/flayr_core/llm/pipeline.py::maybe_reconcile_final_improvements`

---

## 6. P0 安全与本地化契约

### Q14 健康品类建议必须合规

规则：

- 维生素、营养补充品、儿童牙膏等健康品类，建议话术不得声称治疗疾病、调节激素、改善月经、排出血块、保证效果、未核验年龄段或绝对化功效。
- 标杆中出现高风险表达时，只能作为合规风险指出，不能复制为达人建议。

处理：已阻断 + 已修补。

实现位置：

- 阻断：`scripts/flayr_core/postprocess/health_rewrite.py::validate_recommendation_safety`
- 修补：`scripts/flayr_core/postprocess/health_rewrite.py::sanitize_health_recommendations`
- 修补：`scripts/flayr_core/postprocess/health_rewrite.py::sanitize_child_toothpaste_recommendations`

### Q15 达人建议话术必须使用本地语言

规则：

- `creator_script` 用达人口播语言或目标市场语言。
- 中文只能放在 `creator_script_zh`。

处理：已阻断。

实现位置：

- `scripts/flayr_core/postprocess/health_rewrite.py::validate_creator_script_language`

### Q16 MY 市场认证信息归属唯一

规则：

- KKM / kelulusan / 认证信息不得归入 Hook。
- 第三方机构背书（监管认证 KKM/Halal/SIRIM、行业协会、评测中心/实验室、高校研究、
  调研咨询、疾病防治中心等类型）是外部背书，按**功能**归入 S5 信任放大，不归 S2
  （即便它在视觉上与产品引出同框出现）。
- 关联性门槛：机构的数据/实验/研究要在证明**本产品价值**才算背书；仅提机构名字、
  赞助或合作 logo、无证明本产品价值的数据，不算背书、也不归 S5。
- 自述功效或无第三方支撑的口播是卖点，不算认证背书。
- 口播提及但画面未显示时必须标 `voice_only`。

处理：已阻断 + 已修补。

实现位置：

- 阻断：`scripts/flayr_core/postprocess/validate.py::validate_stage_ownership`
- 修补：`scripts/flayr_core/postprocess/claims_my.py::reconcile_certification_ownership`
- 修补：`scripts/flayr_core/postprocess/claims_my.py::discard_unreferenced_certification_claims`

后续改进：

- `validate_stage_ownership` 仍含 MY 硬编码。扩展到其他市场前，应把阻断逻辑迁到 `claims_my.py` 或未来的 `claims_xx.py`。

---

## 7. P1 Phase C 契约

### Q17 低置信阶段只能由模型声明，代码不猜

规则：

- 第一遍 stage2 可输出 `low_confidence_stages`。
- 只接受 `S1` 到 `S6`，最多 2 个。
- 只有代表帧/切片音频不足以支撑 severity 时才声明。

处理：已实现。

实现位置：

- `scripts/flayr_core/llm/payload.py::build_llm_comparison_payload`
- `scripts/flayr_core/llm/pipeline.py::extract_low_confidence_stages`

### Q18 Phase C 只回看一次

规则：

- 回看素材为对应阶段的标杆/达人原生视频切片，含音轨。
- 第二遍只重判指定阶段。
- 不允许模型继续索要素材，不做多轮循环。

处理：已实现。

实现位置：

- `scripts/flayr_core/llm/api.py::video_to_data_url`
- `scripts/flayr_core/llm/payload.py::build_stage_review_payload`
- `scripts/flayr_core/llm/pipeline.py::maybe_refine_low_confidence_stages`
- `scripts/flayr_core/llm/pipeline.py::apply_stage_review_updates`

验收：

```bash
python3 - <<'PY'
import json
from pathlib import Path
from scripts.flayr_core.llm.payload import build_stage_review_payload
run = Path('runs/20260531-143521-improve')
analysis = json.loads((run / 'analysis.json').read_text(encoding='utf-8'))
facts = {
    'benchmark': json.loads((run / 'video_facts_benchmark.json').read_text(encoding='utf-8')),
    'creator': json.loads((run / 'video_facts_creator.json').read_text(encoding='utf-8')),
}
result = json.loads((run / 'dev_stage2_result_postprocessed_01.json').read_text(encoding='utf-8'))
payload = build_stage_review_payload('qwen3.5-omni-plus', analysis, facts, result, ['S4'])
content = payload['messages'][1]['content']
print(sum(1 for item in content if isinstance(item, dict) and item.get('type') == 'video_url'))
PY
```

期望输出：`2`。

### Q19 S6 叙事文本与口播证据一致性

规则：

- S6 叙事（gap/gap_summary/summary/key_message）声称某方"缺少 CTA"，但该方证据
  （quote/visual_evidence/所引 evidence_unit）含明确购买指令 → 警告（假阴性，实证 are_xie S6）。
- 叙事声称某方"明确告知/清晰购买路径"，但其证据未见购买指令 → 警告（假阳性/疑似脑补，
  实证 kakwan S6 Phase C 幻觉）。
- 子句级主语匹配（达人/标杆），避免"标杆有、达人无"互相误伤。

处理：已警告（不阻断）。

实现位置：

- `scripts/flayr_core/postprocess/validate.py::validate_narrative_evidence_consistency`

---

### Q20 产品命题引用与跨阶段关系一致性

规则：

- 各阶段只能引用 `product_proposition_contract.stages.Sx.allowed_ids` 中的命题 ID。
- S2 可引用后续要由 S3/S4 证明的 selling ID，用于说明“产品为什么是答案”；这只建立承接关系，不能替代使用演示或效果证明。
- flag 声称命中本品锚点时，必须给出至少一个有效 `proposition_ids`；旧结果缺引用只警告，不阻断。
- `s3_s4_relationship` 必须与两侧 S3/S4 结构化 flags 一致，不能一边写“过程产生效果”一边又标无过程或无效果。
- `stage_standard_delivery` 作为模型兼容字段保留，并与代码按单侧 flags 计算的 `computed_stage_standard_delivery` 对照；不一致只警告，不参与 severity。

处理：已警告（不阻断）。

实现位置：

- `scripts/flayr_core/proposition_contract.py`
- `scripts/flayr_core/postprocess/proposition.py::materialize_proposition_trace`
- `scripts/flayr_core/postprocess/proposition.py::materialize_quality_audits`

---

## 8. 当前缺口

### G01 统一 QA issue 对象未实现（暂不做）

旧版文档设想了 `QAIssue` 和 `qa_check(result, meta)`，当前没有单独实现。现在的错误通道是：

- `SystemExit`：阻断并触发 repair。
- `qa_warnings`：软警告。
- 确定性 repair：直接改 data。

建议：暂不新增大而全 `qa_check` 引擎。现在已有 `validate_quality_contract` 作为主流程收口；只有当 UI 或报告需要结构化展示 QA issue 时，再抽 `QAIssue` 对象。

### G02 QA warnings 尚未在报告中突出展示

当前 `qa_warnings` 会写进 `analysis_result.json`，但报告展示还不是重点区域。

建议：后续如果运营侧需要，可在 `report.py` 增加“需人工复核”提示区，仅展示 P0 warning，不展示内部技术细节。

---

## 9. 推荐实施顺序

### 当前已完成

- P0 结构可归一化。
- P0 evidence 引用存在与时间相交。
- P0 跨视频口播污染阻断。
- P0 认证信息归属收敛。
- P0 健康品类合规阻断与改写。
- P0 severity 稳定性和 Top 提升点优先级收敛。
- P1 Phase C 一次性回看。

### 下一步建议

1. 让真实案例继续跑，观察 `qa_warnings` 是否能准确暴露阶段边界和产品可见度问题。
2. 如运营侧需要，在报告顶部增加“需人工复核”区，读取 `analysis_result.qa_warnings`。
3. 暂不做独立 `qa_check` 引擎，避免和现有 validate/repair 双轨。

---

## 10. 验收命令

基础验收：

```bash
python3 -m py_compile scripts/flayr.py scripts/dev_test_stage2.py scripts/flayr_core/*.py scripts/flayr_core/llm/*.py scripts/flayr_core/postprocess/*.py
python3 -m json.tool references/analysis-output-schema.json >/dev/null
python3 scripts/dev_test_stage2.py runs/20260531-143521-improve --dry
```

稳定性验收：

```bash
python3 scripts/dev_test_stage2.py runs/20260531-143521-improve --repeat 5 --reuse-existing
```

期望：

- `finish_reason_all_stop = true`
- S3 severity 稳定。
- 其他阶段 severity 不被带偏。
- `ambiguity_count = 0`
- 验收结果 `PASS`
