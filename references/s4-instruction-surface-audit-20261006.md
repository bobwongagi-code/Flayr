# S4 指令与引用入口零调用盘点

2026-10-06；源码起点 be6b7a9。审计与定义一致性修正单独提交，随后机械 refreeze；与工程修复分开。
没有视频重跑、模型调用、历史 GT 改动、档位改动或候选启用。

## 指令分布与检查结论

| 层次 | 实际位置/入口 | 本轮核对 |
| --- | --- | --- |
| 业务库 | structure_library_full.md，S1-S6 六节 | 使用、效果和独立来源分工；不能用口播补出视觉结果 |
| 唯一阶段合同 | stage_contract_registry.py：合同、信号及 STAGE_BOUNDARY_TESTS | S4 visual_required；真实弱结果允许，普通状态/操作不自动是结果 |
| 商品地基/资格背景 | payload.py：build_product_foundation_payload / repair；资格入口 s4_proof_context | 地基不是视频事实；推断目标不能变成 SKU 已验证功效。本轮不修改地基选择规则 |
| Stage1-A | payload.py：build_video_fact_payload | proof=result_only 指“只见结果”；原子观察标签不能直接保证阶段资格 |
| Stage1-B | payload.py：build_stage_evidence_qualification_payload | 注入完整目标合同；仅引用同侧锁定原子事实；覆盖不足不能当 absent |
| Stage1-C | payload.py：build_video_fact_recovery_payload | 新增候选事实、不直接资格化；已有使用后对象状态提示只说明候选，不保证其就是效果 |
| Stage1-D | pipeline.py：_run_stage1_qualification 调用同一 B builder | 复用合同，不是另一份手写语义定义 |
| Stage2 四组 | payload.py：build_stage_group_judgment_payload / _stage_group_flag_contract | 各阶段边界注入；S4 原允许“结果叙述”，已换共享视觉定义 |
| Phase C | phase_c_payload.py：build_stage_review_payload | 受限事实补丁；原允许“结果叙述”，已换共享视觉定义 |
| 总结/建议及可选 shadow | payload.py：synthesis / reconciliation / absolute_execution_shadow | 是下游消费和独立观察，不视为取证源；本轮不改评分和建议规则 |
| 整体分析兼容 | full_analysis_payload.py：build_llm_payload / repair | 不在当前 segmented pipeline 主调用链；保留列入盘点，不能拿其文字证明主链正确 |
| 旧总体 prompt | prompt.py | 仍有“开头算 S1、结尾算 S6”的位置规则和旧自用背书说明，与现行资格边界不一致；Stage2 builder 的 _analysis_input 不被消费，不把旧总体正文视作当前阶段判断指令；不擅自激活或修订旧语义 |
| 隔离实验 | compact_eval.py：S4 fact_state / single_pass | 两处“结果口播”定义及一处明确许可已去除，复用共享视觉定义 |
| 隔离实验校验 | compact_eval.py：_validate_s4_state_side | result_only 仍允许 proof=claim_only；不是生产资格门禁，本轮不使用这个宽松校验作为实验放行器，未修改历史实验解释 |

上述是入口清单与逐处静态核对，不是“所有歧义已解决”的证明。
确定性测试能检查已指定合同条款有没有传到请求、已知反向许可是否仍在；
不能靠搜索限定词自动证明自然语言语义完整。未来新增入口也需进入此清单和实际 builder 测试。
本轮没有将旧兼容路径的指令偷偷混入 baseline/candidate。

## 引用安全边界

复用 stage_evidence_contracts.py 中已有逻辑，不增加第二套裁判：

- normalize_stage_evidence_checks / _normalize_signal_bindings 保留非法 ID 为审计错误。
- _stage_check_issues 拦未知 ID、跨阶段引用、无证据 supported 和无效 binding，负向状态也检查。
- stage1_acquisition_issues 检查采集来源与已引用视觉时间覆盖；时间落在输入覆盖内不证明画面语义正确。
- 同侧真实 ID 仍可能被错误解释（例如搓洗 ID 被称为洗净结果），必须阅读事实与理由。
- 自由文本理由中的时间不能用来绕过 evidence_ids；本轮资格实验不新增“仅凭时间可通过”的入口。

新增回归覆盖六阶段合同边界抵达资格与 Stage2、S4 四处指令共享定义且无已知口播许可、
虚构 C999 和其他角色 B1 不能支撑当前 creator 的 S4。既有测试继续保护时间覆盖与不可变来源。

## 验证与剩余阻断

- test_stage_prompt_boundaries：7 项通过（新增 3 项）。
- test_compact_eval：44 项通过。
- test_stage_evidence_contracts：200 项通过。
- 未执行模型调用。旧 NIUMO 弱结果有人工依据，但目前未找到本轮已准入的现行输入；不能说保护集已齐。
- 当前源码变更会影响模型输入，提交身份和版本门禁见 semantic-baseline-freeze.json/md；资格候选仍未启用。
- 全规则 ruff 检查失败，包含遗留告警；只使用项目既有质量门禁选择项核验本轮，不扩范围修旧问题。
- 项目 ruff 选择项 E4/E7/E9/F821（忽略 E402）在五个变更代码/测试文件通过；git diff --check 通过。

调用方案见 s4-result-participation-minimal-plan-20261006.md。输入准入及版本收口前保持阻断，
预算仍最多 16 次实际发送，不能增加一个控制后把总量悄悄变成 20。
