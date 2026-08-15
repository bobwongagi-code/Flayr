# 旧 16 组 GT 人工复核清单

本表只整理 `ground-truth-labels.md` 中已有的人工原话，不改写权威 GT。旧协议把 `none` 和部分
`NA` 记入 `small`，因此下列 36 格必须在冻结评分前确认。`工程建议`只是待复核假设：

- `small`：原话明确存在可见小差距；
- `none`：原话更像双方都执行且表现相当；
- `NA`：原话明确双方都未执行，或阶段不涉及；
- `无法判断`：现有文字不足以安全区分。

任何 `none`/`NA` 建议都需要领域专家确认。其他格至少抽查 5 个，覆盖多个阶段；一处解释不符即
暂停整批迁移。确认前所有格继续保持 `legacy_ambiguous`，不进入冻结分母。

| 样本 | 阶段 | 工程建议 | 现有人工原话或判断依据 | 复核状态 |
|---|---:|---|---|---|
| are_xie | S3 | 无法判断（`none`/`NA`） | “双方均未展示吞服动作，成分罗列符合品类特性” | 待确认 |
| are_xie | S6 | small | 复议已采纳 small；达人 CTA 过度硬推，但并非无 CTA | 待抽查 |
| kakwanreview | S4 | none | “双方均展示冲净效果” | 待确认 |
| kakwanreview | S5 | NA | “双方均未涉及”；低客单价日用品未设置独立背书 | 待确认 |
| tashadiyana | S2 | small | 达人高效引出产品并展示外观，原结论明确维持 small | 待抽查 |
| tashadiyana | S5 | NA | “双方均未涉及” | 待确认 |
| tashadiyana | S6 | none | “达人 CTA 不弱于标杆” | 待确认 |
| carslan-b0 | S5 | 无法判断 | 原表只说明标杆含专业化妆师身份，未单独说明双方 S5 差距 | 待确认 |
| carslan-b0 | S6 | 无法判断 | 原表没有足够的 S6 阶段级原话 | 待确认 |
| simplus | S5 | 无法判断 | 旧表只给出 small，没有阶段级理由 | 待确认 |
| simplus | S6 | 无法判断 | 旧表只给出 small，没有阶段级理由 | 待确认 |
| wukoubo-c0 | S2 | small | 2026-06-24 复看明确改为 small | 待抽查 |
| wukoubo-c0 | S5 | NA | 旧表明确标注“不涉及” | 待确认 |
| wukoubo-c0 | S6 | NA | 无口播样本，旧表明确标注“不涉及” | 待确认 |
| wukoubo-c1 | S5 | NA | 旧表明确标注“不涉及” | 待确认 |
| wukoubo-c1 | S6 | NA | 无口播样本，旧表明确标注“不涉及” | 待确认 |
| youkoubo-c0 | S5 | NA | 旧表明确标注“不涉及” | 待确认 |
| youkoubo-c0 | S6 | none | “双方都说了促单” | 待确认 |
| carslan-b1 | S5 | 无法判断 | 旧表只给出 small，没有阶段级理由 | 待确认 |
| carslan-b1 | S6 | 无法判断 | 旧表只给出 small，没有阶段级理由 | 待确认 |
| colorkey-b1 | S5 | 无法判断 | 旧表只给出 small，没有阶段级理由 | 待确认 |
| colorkey-b1 | S6 | 无法判断 | 旧表只给出 small，没有阶段级理由 | 待确认 |
| colorblu-c0 | S1 | small | 达人钩子合格但弱于标杆胶水效果钩子 | 待抽查 |
| colorblu-c0 | S2 | 无法判断 | 只记录“自然引出产品”，未明确双方是否存在差距 | 待确认 |
| colorblu-c0 | S6 | none | “双方均有价格实惠、配件齐全等促单” | 待确认 |
| colorblu-c1 | S2 | 无法判断 | 只记录“镜头已展示产品，自然衔接”，未明确双方差距 | 待确认 |
| colorblu-c1 | S6 | none | “双方均有价格实惠、配件齐全等促单” | 待确认 |
| carslan-powder-c0 | S2 | small | 原话“差距不大” | 待抽查 |
| carslan-powder-c0 | S3 | small | 达人有上脸过程，但不如标杆细致、专业 | 待抽查 |
| carslan-powder-c0 | S5 | none | “双方仅是肤质自述的弱软背书” | 待确认 |
| carslan-powder-c0 | S6 | small | 达人 CTA 明显、标杆口播不清，原话明确达人更强 | 待抽查 |
| carslan-powder-c1 | S2 | small | 原话“差距不大” | 待抽查 |
| carslan-powder-c1 | S5 | small | 标杆有肤质自述的弱软背书，原标签记录小差距 | 待抽查 |
| colorkey-lip-c0 | S1 | none | “双方均以使用与效果切入” | 待确认 |
| colorkey-lip-c0 | S2 | small | 双方均展示产品，但标杆色彩搭配更契合且“差距有限” | 待抽查 |
| colorkey-lip-c0 | S6 | none | “双方中规中矩” | 待确认 |

## 当前计数

- 建议保留 `small`：10 格；
- 候选 `none`：8 格；
- 候选 `NA`：7 格；
- 现有原话无法安全判断：11 格；
- 已确认迁移：0 格。

这张表用于降低人工复核负担，不得被解析为新的权威 `human_gap` 或 `stage_relations`。

## Relation 最小复核集

旧标签没有独立 relation 轴。下面只挑选人工原话已明确指出强弱方向的 15 格，覆盖 S1-S6；它们
用于满足冻结协议“至少 12 个 relation 格”的最低人工复核入口，不代表其余方向已经迁移。

| 编号 | 样本 | 阶段 | 工程建议 | 现有人工原话或判断依据 | 复核状态 |
|---|---|---:|---|---|---|
| R01 | are_xie | S1 | benchmark_better | 达人未用生理期痛点做情感连接 | 待确认 |
| R02 | are_xie | S2 | benchmark_better | 达人未讲清价值就过度强调省钱 | 待确认 |
| R03 | are_xie | S4 | benchmark_better | 达人缺用户反馈与效果具象化承诺 | 待确认 |
| R04 | are_xie | S5 | benchmark_better | 标杆有 KKM 政府认证，达人无背书 | 待确认 |
| R05 | kakwanreview | S1 | benchmark_better | 标杆用脏图激发厌恶感，达人平铺直叙 | 待确认 |
| R06 | kakwanreview | S2 | benchmark_better | 达人缺传统刷子掉毛痛点对比 | 待确认 |
| R07 | kakwanreview | S3 | benchmark_better | 达人取景不完整，标杆展示完整 | 待确认 |
| R08 | kakwanreview | S6 | benchmark_better | 标杆 CTA 干净明确，达人 CTA 极短且敷衍 | 待确认 |
| R09 | tashadiyana | S1 | benchmark_better | 达人缺开箱惊喜与童声 BGM 的儿童氛围 | 待确认 |
| R10 | tashadiyana | S4 | benchmark_better | 标杆用闻香动作反复具象化水果味，达人仅口头描述 | 待确认 |
| R11 | carslan-b0 | S1 | benchmark_better | 达人钩子效果不明显，双标杆执行更强 | 待确认 |
| R12 | colorblu-c0 | S1 | benchmark_better | 达人钩子合格但弱于标杆胶水效果钩子 | 待确认 |
| R13 | colorblu-c0 | S3 | benchmark_better | 达人没有使用过程 | 待确认 |
| R14 | colorblu-c0 | S4 | benchmark_better | 达人只有口播、没有效果展示 | 待确认 |
| R15 | carslan-powder-c0 | S6 | creator_better | 达人 CTA 明显，标杆口播不清 | 已确认（原GT已有） |

确认 relation 时必须同步写入 `ground-truth-labels.json` 的 `stage_relations`。只改本表状态不会让该格
进入评分；表格和权威 GT 不一致时冻结校验会失败。
