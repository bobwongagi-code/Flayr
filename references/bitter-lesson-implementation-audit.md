# Bitter Lesson 落地审计

状态：`implementation complete; adversarial review passed; ready for commit`
规格：[`bitter-lesson-frozen-spec.json`](bitter-lesson-frozen-spec.json)

## 实施前审计

| 议题 | 当前风险 | 冻结决定 |
|---|---|---|
| Provider 失败是否拖累其他阶段 | 单个响应失败可能触发整条链路重跑 | 只将受影响阶段组标记为 typed unknown，保留已完成组 |
| Stage3 是否能改证据字段 | 模型可能覆盖时间、ID、gap 和优先级 | Stage3 只写 prose/target_stage，机械字段由代码投影 |
| 缺失与 unknown 是否可合并 | 报告可能把未知显示为失败或 medium | `unknown`、`blocked`、`not_applicable`、`not_comparable`、`legacy` 分开 |
| 旧整对象入口是否保留 | 生产路径可能绕过分段合同 | 必须显式 `--legacy-import`，导入结果只作 audit-only degraded，不发布当前 severity |
| 旧结果导入是否触发当前 provider | 历史审计可能被当前 ASR/OCR/翻译配置阻断或产生新调用 | legacy import 只做本地确定性预处理，ASR/OCR/翻译标记 `not_requested_legacy_import` |
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

## 当前 runtime-fix 批次改动边界

本次任务明确授权修复冻结教训在真实运行链路中的缺口，因此不再沿用前一批“只改门禁、不改 pipeline”的范围。当前批次由
`references/bitter-lesson-runtime-fix-scope.json` 单独声明：最多 24 个文件、1500 行新增、400 行删除，并精确列出允许的生产路径。
旧的冻结批次范围仍保留，不能通过修改旧 scope 来掩盖本次越界。

本批次没有调用模型、没有重跑视频、没有修改运行产物；所有行为变更由 fixture、离线重放和单元测试验证。

本批次未进行 `scripts/flayr_core/llm/pipeline.py` 的全量拆分。该文件的历史体量仍是独立的维护债务；本批次仅抽离 provider artifact、验证顺序和门禁边界，避免把高风险结构重构与运行语义修复混在同一批次。后续若处理该债务，必须单独建批、先做字段引用穷举和离线回放，再逐步迁移。

执行：

```bash
PYTHONPATH=scripts python3 scripts/verify_bitter_lesson_contract.py
FLAYR_SCOPE_SPEC=references/bitter-lesson-runtime-fix-scope.json \
FLAYR_SCOPE_BASE_REF=HEAD \
PYTHONPATH=scripts python3 scripts/check_change_scope.py --base-ref HEAD --spec references/bitter-lesson-runtime-fix-scope.json
```

## 实施后复盘模板

| 实施前疑点 | 实际实现判断 | 是否新增自由发挥 |
|---|---|---|
| 阶段组失败如何隔离 | 由 stage group artifact 和 typed unknown 隔离 | 否 |
| Stage3 机械字段所有者 | 由 Stage2 结果确定性投影 | 否 |
| 重放身份 | provider artifact 校验 payload/model/URL/response hash | 否 |
| retry 记录 | provider metadata 持久化 request id、attempt、reason、usage | 否 |
| 旧 JSON 入口 | 默认拒绝；显式 legacy import 后强制 audit-only degraded | 否 |
| legacy import 的预处理副作用 | 只生成本地媒体/证据目录，不启动当前 ASR、OCR、翻译 provider | 否 |
| 辅助 provider 调用 | Step-0、Phase C、S4、OCR、翻译等均落 durable artifact，并支持严格技术重放 | 否 |
| 未知 severity | 缺失/非法值保持 `None`，不再隐式变成 `medium` | 否 |
| Step-0 失败 | 默认阻断；只有显式 `--allow-degraded` 才允许继续 | 否 |
| 真实样本顺序 | `--verification-stage` 缺少前置 passed marker 时直接拒绝 | 否 |
| 变更范围 | CI 使用完整历史和真实 base SHA，读取当前 runtime-fix scope | 否 |

本批次没有调用模型或真实视频。结构化行为变更均有代码契约测试覆盖；提交前必须再执行一次对抗式 review，重点尝试绕过 legacy、replay、scope、unknown 和 Step-0 门禁。

## 对抗式复盘

- 冻结规格校验现在锁定四层的精确读写边界、四类类型定义和八个不变量集合；仅保留字段存在检查不足以阻止语义漂移。
- `run-tests.sh` 固定执行规格校验和变更范围门禁；CI checkout 使用完整历史，并将 PR base/push before SHA 传给 scope gate，不能再用空的 `HEAD` diff 冒充检查。
- 契约测试覆盖成功 provider、失败 provider、精确重放、Stage1 账本完整性、unknown 不发布、机械字段不由 Stage3 写入、legacy import、辅助 provider artifact 和验证顺序门禁。
- 本地全量 unittest、compileall、diff 检查、提示词可达性、分析契约和字段所有权审计应全部通过；质量门禁若因环境缺少 `ruff` 不能运行，必须在提交结果中明确列出，不能宣称全绿。
