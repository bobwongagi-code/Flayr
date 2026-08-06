# Stage1 证据与阶段分析合同

## 目的

Flayr 的阶段判断不是从视频摘要直接生成的。正确的数据关系是：

```text
原始视频与时间线
  -> Acquisition：采集声、画、字幕、OCR、ASR、镜头和时间信息
  -> Evidence：生成可回溯、不可变的原子事实
  -> Qualification：按同一份 S1-S6 合同判断事实是否足够支持某阶段
  -> Judgment：只消费已资格化事实，比较双方并给出模型判断
  -> Resolution：只应用已验证的收窄约束，不创造事实
  -> Report：展示状态、差距和证据，不重新分析
```

这份合同解决的是一个跨阶段、跨模型的系统问题，不是某个样本的补丁：**证据没有采到时，后续不能把空白、未知或冲突当作“没有发生”，也不能用邻近阶段、自由文本或后处理猜测补回。**

## Stage1 的三个输出

### A. 原子观察

`video_understanding.<role>.evidence_units[]` 是唯一事实源。每条事实至少保留：

- 稳定唯一 `id`；
- 原始 `time_range` 和可解析的时间边界；
- `visual_fact`、`voiceover`、`subtitle_fact`、`audio_fact` 等实际观察；
- `evidence_strength`：`direct / explicit / inferred / absent`；
- 需要时保留 `uncertain`、来源角色、产品和变体信息。

Stage1 还必须带一份代码生成的 `stage1_acquisition`。它只记录本次抽取实际拿到的
视觉、口播、字幕和音频输入状态、时间边界精度、视频时长和机器错误；它不记录任何
“这个阶段存在/不存在”的模型判断。这样 `stage_evidence_checks.status=absent` 不能单凭
模型自报的 `coverage=complete` 成立：对应采集通道没有闭合时，代码会把它降为
`unknown` 并阻断后续分析。

对帧采样输入，`visual_input_timestamps` 必须由管线从本次实际发送的、可读取的 canonical
帧清单生成；任意模型或调用方自行填写的时间不能制造视觉覆盖。阶段证据的时间范围只有在与
本次实际发送帧相交时才可能获得正事实资格，时间线总览图没有精确时间戳，只能作为上下文，不能
单独证明一个时间范围内发生了动作。`stage_coverage` 对采样输入只作诊断，不被解释成每个 S1-S6
都已完整观察；原生连续视频才可以声明全时段视觉覆盖。

采集通道的 `coverage` 必须区分 `full` 和 `sampled`。原生连续视频、完整音频或完整
时间线才可记为 `full`；口播还必须同时有词级时间戳和实际提供给模型的
`transcript_windowed` 消费视图，`boundary_precision` 才能记为 `word`。只有分段级 ASR
或只有词级索引但窗口视图未生成时，口播可以保留为审计材料，但不能给任何阶段的正面或负面
断言提供精确归属。即使 canonical frame manifest 中每一帧路径和时间戳都有效，
它仍然只是 `sampled`，因为离散帧不能证明采样间隔内没有发生目标动作。`sampled` 只有在
被判断阶段确实有直接采样输入时，才能支撑一个被直接观察到的正事实；如果阶段没有任何
直接帧，即使模型输出了带时间范围的视觉事实，也只能进入 `unknown`。`sampled` 也不能
支撑“全片没有发生”的 `absent`。这条规则适用于 S1-S6，不针对某个阶段或某个样本单独放宽。

Stage1 不能输出或推导 `severity`、双方比较、差距、商业优先级、建议、报告结论或 `stage_evidence_links`。这些字段即使嵌套在别的对象中，也必须在 active contract 下拒绝，而不是静默丢弃。

### C. 独立语义覆盖审计

`stage1_coverage_audit` 是管线自有的第二次扫描结果，不是模型可以回显的事实字段。它与 primary
抽取使用同一份原始素材和同一份注册表，但不接收 primary 的 `evidence_units`、阶段判断或双方比较，
避免“根据第一次漏掉的内容再解释第一次为什么没漏”的循环。它可以追加候选原子事实，但不能删除、改写
或覆盖 primary 事实。

审计对每个 S1-S6 返回：

- `status=found`：本次独立扫描发现候选事实；
- `status=clear`：在 `coverage=complete` 的完整扫描中没有发现该阶段 required signals；
- `status=unknown`：素材、时间边界、通道或预算不足；
- `status=conflict`：扫描结果自身存在无法闭合的冲突。

只有 `coverage=complete` 才能让 `found/clear` 具有资格作用。审计发现候选时，必须把候选事实追加到同一
个不可变 evidence set，再由代码重新运行 Stage1 qualification。审计与 primary 不一致时不由顺序决定：

```text
primary unknown + audit found + required signals complete -> 允许重新资格化
primary absent  + audit found + required signals complete -> 允许重新资格化，但留下审计痕迹
primary absent/present 与 audit 相反且无法闭合             -> conflict，阶段阻断
audit clear/unknown/partial                                -> 不把未知转换成 absent
```

这条规则是 S1-S6 共用的，不为 S4 或某个历史样本单独加例外。独立审计也不是“准确率自动提升器”：两次扫描
都没有采到事实时，阶段仍然是 `unknown/blocked`；这会降低短期可用率，但保留了后续人工或重新采集的入口，
避免在缺证据时生成看似确定的分析。

### B. 阶段资格投影

`stage_evidence_checks[]` 不是第二份事实库，而是把原子观察投影到功能阶段的资格结果。每个阶段必须有一条记录：

- `status`：`present / absent / unknown / conflict`；
- `coverage`：`complete / partial / unknown`；
- `evidence_ids`：只能引用本侧真实原子事实；
- `observed_signals`、`missing_signals`、`observed_disqualifiers`；
- `signal_bindings`：每个已观察信号分别绑定到本阶段的原子 `evidence_ids`；阶段总证据列表不能替代逐信号绑定；
- `reason` 和必要的 `evidence_strength` 自检摘要。

`present` 必须有完整覆盖、全部 required signals、每个 required signal 的有效 `signal_bindings`、真实证据 ID、`direct/explicit` 原子强度和所需渠道。`absent` 必须有完整覆盖，并明确缺少 required signals，且不能存在支持性 signal binding。`partial`、`unknown`、冲突、预算超限或渠道缺失只能进入 `unknown/conflict`，不能降格成 `absent`。

`signal_bindings` 是 S1-S6 共用的泛化约束。例如 S4 不能因为一条证据同时出现在阶段列表中，就默认它同时证明“结果可见”和“结果由本品操作造成”；这两个信号必须分别绑定。一个原子事实可以支持多个信号，但每次绑定都必须留下可追溯关系。

Stage1 完成后由代码对全部规范化观察字段、`stage1_acquisition`、`stage1_coverage_audit`、`evidence_units` 和 `stage_evidence_checks` 生成 `evidence_set_sha256`，并标记 `evidence_set_status=frozen`。从这一刻起，Stage2、Repair、Phase C 和 Resolver 都只能读取它；任意事实内容、时间、归属、采集能力、覆盖审计、门控观察或阶段资格变化都会使运行失败。阶段链接是可变的判断投影，不进入事实哈希。

## Stage1 到后续分析的唯一交接

代码为每个 S1-S6 生成 `stage_evidence_gate`，不接受模型回填。它同时记录 creator/benchmark 两侧的资格状态、允许引用的 ID、事实集摘要和比较范围：

| gate status | 含义 | 后续行为 |
|---|---|---|
| `grounded` | 两侧均为 `present` 或完整 `absent` | 可以形成有证据支撑的阶段比较 |
| `blocked` | 任一侧 `unknown`、`conflict`、预算未闭合、采集不完整、采集通道不可用或冻结摘要无效 | 阶段标记 `evidence_blocked`；模型 severity 只保留在审计字段，不作为有证据结论展示 |
| `not_applicable` | 比较合同确认双方均未涉及该功能 | 不生成阶段差距 |
| `not_comparable` | 商品关系或共同任务不允许比较 | 不生成阶段差距 |
| `legacy` | 至少一侧没有 active Stage1 合同 | 仅保留历史审计读取；报告不显示为当前阶段结论，不能与新合同的 grounded 结果混为同一统计口径 |

模型的 `model_severity` 可以为了审计留存；它不等于 `grounded` 的最终结论。报告层遇到 `blocked` 显示“未分析/待核验”，不能把缺证据的模型档位当成真实 GT。

`stage_evidence_gate.creator/benchmark.diagnostics` 是代码生成的解释字段。它只回答“为什么这一侧当前可用或被阻断”，不重新判断视频事实；`primary_unknown` 表示主抽取没有形成确定资格，`primary_qualification_gate` 表示阶段信号没有逐项绑定到本阶段原子证据或其他资格投影不成立，`acquisition_gate` 表示输入能力、覆盖或边界不闭合，`coverage_audit_gate` 表示独立覆盖审计不可用或与主投影不一致，`snapshot_invalid` 表示冻结摘要失效。报告和历史分析应按这些稳定代码统计，不应从自由文本 `reason` 反推根因。

阶段证据引用使用独立的 `stage_evidence_links[]`：

```text
stage_id + role + evidence_id + relation + linking_reason + confidence
```

它必须引用已存在、属于该阶段最终引用列表的事实。`primary` 事实在同一侧只能归属一个主要阶段；同一事实可以作为其他阶段的 `supporting` 或 `contradicting` 证据，但不能用多个 `primary` 归属掩盖阶段边界错误。旧结果自动迁移时必须标记 `source=compatibility`，不能假装模型给过链接理由。

## S1-S6 的泛化边界

六个阶段是功能阶段，不是固定的连续时间切片。每个阶段由同一份注册表声明 required signals、可选信号、不可替代渠道、排除项和四类边界测试：自身正例、非自身反例、前一阶段混淆、后一阶段混淆。

| 阶段 | 必须证明的功能 | 常见越界 |
|---|---|---|
| S1 Hook | 陌生观众能理解的停留触发 | 用后段完整上下文倒灌开头，或把产品引出当 Hook |
| S2 Handoff | 产品身份和问题到产品的承接 | 只有产品露出，或把真实使用动作提前算进 S2 |
| S3 Usage | 目标接触、真实动作、应用变化和可追踪过程 | 只有拿持/口播，或用结果画面替代使用过程 |
| S4 Effect | 可见结果差异与本品动作的对象、时间因果连接 | 只有功效声称，或只看动作不看结果 |
| S5 Trust | 可识别来源、来源依据和产品相关性 | 把产品自述、价格、优惠、泛“很火”当独立背书 |
| S6 CTA | 面向观众的行动指令和可执行路径 | 只有推荐、价值回顾或泛泛收尾 |

这些是可解释的功能合同，不是从 9 个样本拟合出来的阈值。样本只能用来验证召回、精确率、边界一致性和误差分布，不能单独创造一条新规则。

## 历史错误如何转成通用控制

| 历史错误类型 | 通用控制 |
|---|---|
| S4 有动作但没有可验证效果，或把结果与过程混淆 | S3/S4 独立 required signals、阶段链接和 `unknown` 闸门；不能用邻段证据补齐 |
| S1 窗口泄漏、ASR 粗段跨越多个阶段 | windowed transcript 与 raw transcript 分离；阶段时间仅消费窗口安全转写；越界和跨阶段链接硬失败 |
| S5 将产品主张、软背书、硬来源混为一谈 | Stage1 只记录 source facts；S5 资格要求来源主体、依据、关联性，未知不等于 absence |
| S6 口播/字幕缺失或结尾证据漏采 | `evidence_budget_exceeded` 和缺阶段检查统一触发一次有预算的补观察；失败保留 unknown |
| VL/模型输出两侧内容混合、证据引用错角色 | 每侧独立 evidence ID 空间、链接角色校验、跨视频/跨阶段引用拒绝 |
| OCR、ASR、镜头或原生视频能力失败 | 采集能力状态写入运行产物；缺失能力只产生降级/unknown，不产生占位事实或确定性严重度 |
| 详细模型撞隐藏证据数量上限 | 上限必须是公开合同；超限显式标记并触发全阶段补观察，代码不得静默截断 |
| 后处理改写时间或新增“修复事实” | active facts 冻结摘要；下游只可改阶段链接和报告字段，事实变化直接失败 |

## 验收与数据分析口径

历史数据和新数据均应按同一层次拆解，而不是只看最终 severity：

1. **采集层**：每种能力是否成功、覆盖了哪些时间段、是否超预算、是否有词级/帧级边界。
2. **事实层**：人工关键事实的召回率、模型事实精确率、时间误差、角色/阶段归属错误率。
3. **资格层**：每个阶段两侧的 `present/absent/unknown/conflict` 分布、`blocked` 比例、补观察成功率。
4. **判断层**：只在 `grounded` 且比较合同允许的格子中比较 severity；方向错误、档位错误和口径差异分开统计。
5. **解析层**：floor/ceiling 只对显式硬事实生效，记录触发、跳过、冲突和顺序无关性。

同模型的独立覆盖审计只是一个有预算的漏采集防护，不是语义正确性的证明，也不能替代人工关键事实 GT。任何召回率、精确率、阶段准确率或模型选型结论，都必须在独立标注或 fresh blind cohort 上计算；9 个历史样本只能验证机制路径和错误分类，不能直接拟合新的阶段阈值。
6. **产物层**：阶段链接完整性、证据哈希未变、报告没有把 `blocked` 显示成 severity。

因此，某阶段准确率低时先问“证据有没有采到、覆盖审计是否闭合、是否被正确资格化”，再问“模型判断是否错”；某阶段准确率高时也要检查是否只是 `absent` 偏多、样本不适用、覆盖审计未真正触发或模型结论碰巧一致。任何一层没有闭合，都不能把最终数字解释成模型能力。
