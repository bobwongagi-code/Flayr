# S4 档位单变量实验准入筛查（2026-09-23）

本次只读已有事实包、模型产物和人工理由；未重跑视频、未调用模型。筛查条件是事实与资格可核、relation 正确、错误集中在 gap，并且有同一冻结身份下可比较的正确 medium/large 对照。`draft_visible` 人工复核只用于诊断，不视为盲测晋级 GT。

**裁决：目前没有可直接进入同期 A/B 的干净样本组。** 内容上有候选，但技术状态、版本身份或资格字段仍有缺口；不能把历史响应当作新实验的 baseline。S4 自洽门禁已在 `0fee1a2` 独立提交，并于 `ffe7388` 在干净工作树重新冻结；这只改变产物接收，不证明档位更准。

| S4 格子 | 当前可用结论 | 准入限制 |
| --- | --- | --- |
| [BOTARE TISU](../runs/20260910-similar-format/botare-tisu/human-review.md)：人工 `creator_better/medium`，模型 `creator_better/large` | 双侧资格与方向基本相符，可诊断单侧合格展示为何被放大成 large | S3/S4 provider 组虽完成，整组因 Stage3 类型错误 `FAILED/DEGRADED`；人工为 `draft_visible`，不能用现有整组结果作正式 A/B |
| [NIUMO C1](../runs/20260910-similar-format/niumo-c1-sg4/human-review.md)：人工 `benchmark_better/small`，模型 `benchmark_better/medium` | 人工明确把插线后显示充电认作弱效果；可诊断判重 | 原生 v7 Stage1 的 `partial/direct`、`result_difference=missing` 相容；Stage2 的 `effect_type=none` 与 `result_only` 也不是硬冲突。但 B8 同时记录插线与充电状态、`effect_attribution` supported，Stage2 却写 `result_only_without_process=true`、`process_linked_effect=false`。因果桥资格仍有跨产物解释张力，不能作为资格已干净的纯 gap 格 |
| [Aavini 护脚霜](s4-expert-review.md)：人工 `benchmark_better/large`，旧模型 `benchmark_better/small` | 双侧都有结果证据，焦点/可见性差异可用于历史诊断 | 旧产物代码身份 `250c359`，不符合当前[冻结源](semantic-baseline-freeze.md)，不可直接并入新 A/B |
| [Frosty Pearl 猫砂](expert-gap-calibration.json)：人工和旧模型均为 `benchmark_better/medium` | 内容上可作正确 medium 控制，S4 双侧均有合格事实 | 旧代码身份、v6 资格合同与 `improve` 运行模式；仅诊断，尚无当前 v7 输入包 |
| [Dr.WOW 海绵](../runs/20260910-similar-format/drwow-magic-sponge-sg4/human-review.md)：人工和模型均为 `benchmark_better/medium` | 近期 medium 弱控制 | 人工是整组“都对了”确认，无独立 S4 理由；原运行是 v6 事实包、`analysis_completed_unpublished`，无 `_SUCCESS`。另有 v7 资格补查响应，但其输入是旧事实的实验投影，不是原生完整 v7 事实包 |
| [Colorblu C0](legacy-gt-current-schema-reconfirmation.md)：后续人工复核给 S4 `large`，理由明确指向标杆；旧模型给 `benchmark_better/large` | 内容上可作 large 控制候选 | 旧代码身份和 v6 合同；原始迁移清单的 `legacy_magnitude_only` 描述旧 GT，并未被改写。后续人工复核没有单列结构化 relation，且仅供非盲诊断，不可把旧清单或旧模型输出当作独立方向确认 |

[CREAMY 的四份响应](creamy-four-response-audit-20260923.md)同时改变了资格状态和档位，修订 r1 还有 S4 内部矛盾；PAPA FEEL C1/C2 和 CARSLAN 粉饼 C0 则有待核的事实或资格分歧。它们不能填补上述档位实验的分母。

当前主工作区仍有未提交的语义/prompt 候选，冻结校验在该工作区不通过；隔离树的冻结校验和质量门禁通过。[v7 资格补查清单](../runs/diagnostics/gap-calibration-20260920/qualification-live/manifest.json)的 10 次响应已完成，但[实验输入包](../runs/diagnostics/gap-calibration-20260920/ab-input-corrected/manifest.json)明确标为 `prepared_offline_corrected_not_admitted`：除 NIUMO 原生 v7 输入外，其余目标阶段将资格响应叠加在旧 v6 事实上。不能把“资格补查完成”记作“完整当前事实包已准入”。

今后若重开 S4 档位实验，须先在同一当前合同下取得可核的事实与资格输入；Colorblu 的后续人工文字可作为非盲诊断来源，若要求逐格独立结构化 relation，仍需另行确认。BOTARE 的旧 S3/S4 provider 响应、Frosty/Colorblu 的旧产物不能直接成为任一实验臂。新的资格调用、同期双臂调用及生产替换均未在本次筛查中授权。
