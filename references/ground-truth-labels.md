# 回归集 Ground Truth 标签

> 来源：2026-06-10 用户（领域专家）人工定标，覆盖 3 个跨品类样本全部 18 个阶段，
> 含两次原生重听裁决。用途：stabilize 重构（TODO #1）回归集的"预期"部分。
>
> ⚠️ 回归只对"输入视频 + 本标签"，**绝不 diff runs/ 存档输出**——三个存档
> analysis.json 全部产自"认证归 S5"修正（ed93dca）之前的代码，按存档对齐等于固化已修复的 bug。
> ⚠️ 本文件是人工判断、不可由代码再生成（故不放 .gitignore 的 runs/）。

## 输入视频

| 样本 | 达人视频 | 标杆视频 |
|---|---|---|
| are_xie | `…/达人内容能力提升/are_xie/达人视频.mp4` | `…/are_xie/爆款视频.mp4` |
| kakwanreview | `…/kakwanreview/达人视频.mp4` | `…/kakwanreview/标杆视频.mp4` |
| tashadiyana | `…/tashadiyana/达人视频.mp4` | `…/tashadiyana/爆款视频-无小孩出镜.mp4` |

（根路径：`/Users/wangbo5/Documents/Rootify/Rootify电商项目/达人内容能力提升/`）

## 标签

### are_xie（女性生理期保健品 Pentavite，ms）

| 阶段 | 标签 | 当时系统输出 | 判定要点 |
|---|---|---|---|
| S1 | large | large ✓ | 未用生理期痛点做情感连接，错失黄金 3 秒 |
| S2 | medium | medium ✓ | "过度强调省钱"抓得对。判例：非刚需品未讲清价值就谈省钱，隐含"默认已日常消费保健品"的盲区 |
| S3 | small | small ✓ | 双方均未展示吞服动作，成分罗列符合品类特性 |
| S4 | large | large ✓ | 缺用户反馈/效果具象化承诺 |
| S5 | **large** | medium ✗ | 标杆有 KKM 政府认证（位置靠前，但功能即 S5 背书——"看功能不看位置"）vs 达人零背书。认证→S5 修正后应自愈，回归验证点 |
| S6 | medium（**归因必须重写**） | medium，归因✗ | 系统称"视频在有效 CTA 前结束"是事实错误：转写 23-40 行近半为 CTA（bag kuning×3、direct HQ×4、beli×4、check out sekarang、jangan tunggu）。正确归因：CTA 过度硬推 + S1-S4 卖点链断裂导致 CTA 无效；标杆是 S1-S5 讲顺后顺水推舟 |

### kakwanreview（一次性马桶刷，ms）

| 阶段 | 标签 | 当时系统输出 | 判定要点 |
|---|---|---|---|
| S1 | large | large ✓ | 标杆用脏图激发厌恶感，达人平铺直叙 |
| S2 | medium | medium ✓ | 缺"传统刷子掉毛"痛点对比 |
| S3 | **medium** | small ✗（牙膏规则强压） | 镜头语言差距：达人画面歪斜、马桶只见一边、最后 1 秒才放回；标杆全貌完整、弹出过程充分。**过拟合危害=判定错误的实锤** |
| S4 | small | small ✓ | 双方均展示冲净效果 |
| S5 | small | small ✓ | 低客单价日用品背书必要性低，双方均未涉及 |
| S6 | **large** | small ✗（Phase C 幻觉） | 标杆有干净 CTA（"Kalau nak beli, boleh order dekat bag kuning tu" + 价格反差铺垫）；达人无任何明确 CTA。达人结尾实为 "Dia punya review pun ada dekat background ni"（用户原生重听确认；whisper 曾转糊为 rintik / beg kau ni，Phase C 据此幻觉出"明确告知链接在购物车"）。CTA 缺失即 severity 标尺 large 的字面例子 |

### tashadiyana（儿童泡沫牙膏，ms）

| 阶段 | 标签 | 当时系统输出 | 判定要点 |
|---|---|---|---|
| S1 | medium（定性=**氛围感不足**） | medium，定性"视觉冲击弱"欠准 | 儿童牙膏要的是氛围（开箱惊喜+童声 BGM），不只是视觉冲击 |
| S2 | small（**结论重写**） | small ✓，结论欠准 | 达人宣传图+话术实为"高效引出产品/品牌 + 外观好看"（外观也是卖点），非"信息密度更高" |
| S3 | small | small ✓ | 同品类，按压/用量措辞贴切 |
| S4 | large | large ✓ | 标杆闻香动作+反复"Wangi"直观展示水果味；达人仅口头"sedap" |
| S5 | small | small ✓ | 双方均未涉及 |
| S6 | small | small ✓ | 达人 CTA 不弱于标杆 |

## 系统误判账目（severity 3/18 错，其中 2 个为自家机器所致）

| 错误 | 元凶 |
|---|---|
| kakwan S3 small（应 medium） | 牙膏过拟合规则强压（`set_stage_small`），压掉模型判断 |
| kakwan S6 small（应 large） | Phase C 回看幻觉 + 回看 prompt"持平必须给 small"的倾向压力。注：确定性触发器标低置信是**对**的，回看反而帮倒忙 |
| are_xie S5 medium（应 large） | 旧代码认证归 S2（已修 ed93dca），回归时验证自愈 |

另有同根的双向叙事错误（gap/summary 叙事文本不对照转写证据校验，见 TODO #6）：
are_xie S6 假阴性（CTA 铺天盖地却写"无 CTA"）/ kakwan S6 假阳性（无 CTA 却写"明确告知购物车"）。

## 待写入框架的判例（spec §0 时收编）

1. **省钱卖点的隐含前提**：仅对"日常已消费/刚需补货"动机成立；非刚需品未讲清价值前谈省钱是卖点选择偏差。
2. **CTA 过度重复 = 硬推，应扣分**：现行框架只说"给指令归 S6"，未覆盖"指令过多反而负面"。
3. **结构先验**：S1 Hook 与 S6 CTA 位置相对固定，S2-S5 中段浮动——作为"看功能不看位置"的辅助先验。
4. **弱 CTA ≠ 有效 CTA**：一句含糊带过、观众无感的 CTA 不等于完成促单功能；有无之外还要判强度。
5. **镜头语言/取景完整性**（歪斜、主体只见局部）是 S3 执行性差距的有效维度（→ observation-guide 待补）。
