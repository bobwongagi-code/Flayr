# Cohort Rejection Ledger

本台账只记录机制回归或 cohort 运行中被拒绝/拦截的根因，不记录 severity 正确性，也不把这些样本转为 blind promotion。`purpose` 与 `evaluation_role` 的历史含义保持不变。

同一个样本可以有多条记录；不同根因不得合并。

| sample | 阶段/侧别 | 状态 | 根因分类 | 具体事实 | 防线/处理 | 结果用途 |
|---|---|---|---|---|---|---|
| `are_xie` | S4，空证据状态 | 已修复 | 合同/校验器 bug | 合法的 `effect_type=none`、`effect_evidence_state=none`、`evidence_ids=[]` 被统一非空规则拒绝 | 状态感知的空证据合同；commit `0374ad5` | 机制回归，不用于 severity 或 blind accuracy |
| `are_xie` | benchmark S4 | 已拦截并固化回归 | 证据归因错误 + repair 校验缺口 | B3=`13.8s - 25.5s`（S5）被引用到 S4=`25.5s - 38.3s`；S4 窗口事实应为 B4 | 通用时序检查 `evidence_temporal_mismatch`；fixture：`tests/fixtures/are_xie_s4_temporal_mismatch.json` | 只验证 repair 能正确标记 `state_conflict`，不决定 severity |

## 分类约定

- 合同/校验器 bug：已有合同或不变量表达错误，修复不改变业务判断。
- 证据归因错误：模型或上游归因把事实绑定到错误阶段；由通用事实校验拦截，不针对样本调 prompt。
- 本台账不等同于人工 GT，也不构成 blind cohort 结果。
