# Bitter Lesson 落地审计

状态：`implementation complete; uncommitted pending explicit authorization`  
规格：[`bitter-lesson-frozen-spec.json`](bitter-lesson-frozen-spec.json)

## 实施前审计

| 议题 | 当前风险 | 冻结决定 |
|---|---|---|
| Provider 失败是否拖累其他阶段 | 单个响应失败可能触发整条链路重跑 | 只将受影响阶段组标记为 typed unknown，保留已完成组 |
| Stage3 是否能改证据字段 | 模型可能覆盖时间、ID、gap 和优先级 | Stage3 只写 prose/target_stage，机械字段由代码投影 |
| 缺失与 unknown 是否可合并 | 报告可能把未知显示为失败或 medium | `unknown`、`blocked`、`not_applicable`、`not_comparable`、`legacy` 分开 |
| 旧整对象入口是否保留 | 生产路径可能绕过分段合同 | 仅兼容导入可用；默认 text-only 入口直接拒绝 |
| 真实视频何时运行 | 真实调用会同时改变多个变量 | 固定顺序：fixture → offline replay → fake provider → ordinary → boundary |

实施前没有未决的规格问题。任何新出现的语义判断必须先补入冻结规格，不能在代码中临时决定。

## 旧代码反例

在实现前，使用基线提交 `46ac066` 建立临时反例测试，验证默认的
`llm_include_images=False` 旧入口不会拒绝 text-only 请求。旧代码结果为
`FAIL: SystemExit not raised`，并继续打印 `LLM dry run: request payload constructed in memory`。
同一反例在当前代码上通过，说明这条门禁不是只测试当前实现的镜像。

## 独立验收顺序

1. `tests/test_bitter_lesson_contract.py`：固定 fixture 和字段边界。
2. `scripts/replay_finalization.py`：无 provider 调用的离线回放。
3. `tests/test_stage_group_artifacts.py`：fake provider 的完整生命周期。
4. 普通样本：只在前 3 层通过后运行。
5. 边界样本：最后运行，不作为开发调试器。

契约测试文件的哈希写入冻结规格。测试被修改时，门禁会失败，必须先审计并显式更新规格，而不能让实现者顺手放宽测试。

## 本批次改动边界

- 最大 8 个文件、900 行新增、150 行删除。
- 只允许修改冻结规格、实施审计、门禁脚本、测试、测试入口和 README。
- 禁止修改真实 pipeline、生产 CLI、schema、真实运行产物和本地媒体。
- 本批次不调用模型、不重跑视频、不改变业务判定规则。

执行：

```bash
PYTHONPATH=scripts python3 scripts/verify_bitter_lesson_contract.py
PYTHONPATH=scripts python3 scripts/check_change_scope.py --base-ref HEAD
```

## 实施后复盘模板

| 实施前疑点 | 实际实现判断 | 是否新增自由发挥 |
|---|---|---|
| 阶段组失败如何隔离 | 由 stage group artifact 和 typed unknown 隔离 | 否 |
| Stage3 机械字段所有者 | 由 Stage2 结果确定性投影 | 否 |
| 重放身份 | provider artifact 校验 payload/model/URL/response hash | 否 |
| retry 记录 | provider metadata 持久化 request id、attempt、reason、usage | 否 |
| 变更范围 | 由 scope gate 读取冻结预算和路径清单 | 否 |

本批次没有新增规格外判断。分层独立提交因当前任务没有授权提交而保持为待提交状态；代码门禁和测试已经落地，后续提交必须按层完成或明确说明原因。

## 对抗式复盘

- 冻结规格校验现在锁定四层的精确读写边界、四类类型定义和八个不变量集合；仅保留字段存在检查不足以阻止语义漂移。
- `run-tests.sh` 固定执行规格校验和变更范围门禁；范围门禁以 `HEAD` 为批次基线，超出路径或行数预算时先拆批次。
- 契约测试覆盖成功 provider、失败 provider、精确重放、Stage1 账本完整性、unknown 不发布、机械字段不由 Stage3 写入和旧入口反例。
- 未安装 `ruff`，因此 `scripts/run-quality-gates.sh` 本轮无法完整执行；全量 unittest、compileall、diff 检查、提示词可达性、分析契约和字段所有权审计均已通过。
