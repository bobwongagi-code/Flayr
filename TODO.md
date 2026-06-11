# Flayr 待办与决策记录

> 来源：2026-06-09 一轮分析链梳理 session。集中沉淀延后项 + 已否决提案，
> 便于将来写 spec §0 时统一收编，也避免重复提已否决的方案。

## 本轮新增基建（已落地，2026-06-09）

为支撑"大量视频处理 + 实验"，已建：

- **传输层流式**：`api.py:call_llm_api` 改 SSE 流式（curl `--http1.1 --no-buffer`），分块拼装 +
  `[DONE]`/`finish_reason` 判完整 + 截断整次重试。根治大响应在网络层被静默截断（非流式下
  6000+ token 响应近乎确定性截断）。下游 `fetch_json_completion` 再加一层"内容不可解析就重取"。
- **预处理复用**：`flayr.py --reuse-preprocessing`，复用 `_preprocess.json` 跳过抽帧/转写/OCR。
  实验迭代（同视频改 prompt/代码）和 LLM 失败补跑直接进 LLM 步。
- **脱离式批处理 runner**：`scripts/batch_analyze.py`，nohup 起一次不被 harness cull；
  状态写 `runs/_batch/status.json`（廉价轮询）；断点续跑（跳过已 done）；故障隔离；限并发。
  用法：`nohup python3 scripts/batch_analyze.py jobs.json --concurrency 1 >runs/_batch/runner.log 2>&1 &`
  作业清单模板见 `runs/_jobs_rootify.json`。

> 环境教训：harness 跟踪的后台任务会 cull 长时/无输出进程；长批量作业必须走 nohup 脱离 + 状态轮询。

## 待办（按触发条件）

### 1. stabilize_stage_severity 整体去过拟合（大）
现状：S2/S3/S4/S5 的稳定化规则全是对着 runs 里唯一一条儿童牙膏视频长出来的正则关键词
（`creator_has_functional_effect`=按压/用量、`mentions_sensory_gap`=闻/香/口味、
`has_real_endorsement` 的否定处理等）。换品类大概率失效。
方向：把这些语义判断从正则改为**模型输出的结构化标记**（同 `product_visible` 的做法）——
模型按框架定义逐 stage 标 task_completion / 是否有外部背书 / 功能是否达成，代码只做确定性消费。
触发：集中重构时做，别零敲。

**回归集已就绪（2026-06-09 跑出 3 个跨品类真实样本）**，过拟合实锤：

| 样本 | 品类 | 牙膏正则命中 | 判定 |
|---|---|---|---|
| `runs/sample-are_xie` | 保健品（生理期营养） | 无 | ✅ 无关键词重叠→不触发→干净 |
| `runs/sample-kakwanreview` | 马桶刷（清洁工具） | 按压/用量/闻香/口味 | ❌ **误触发**：马桶刷"按压按钮"撞 `按压` 正则，灌入对马桶刷无意义的牙膏文案 |
| `runs/sample-tashadiyana` | 儿童牙膏 | 按压/用量/防蛀/闻香/口味/牙膏 | ✅ 同品类→正确触发 |

结论：`creator_has_usage_demo` 等靠关键词巧合，在③同品类对、①无重叠品类对、但②**碰巧共享关键词的品类误触发**。重构用这 3 个样本当回归集验证。

**2026-06-10 危害升级实锤**：kakwan S3 被牙膏规则强压成 small，人工定标应为 medium（镜头语言差距）——
过拟合不只污染文案，已实际造成 severity 判定错误。

**Ground truth 已建**：`references/ground-truth-labels.md`（18 阶段全判 + 事实裁决 + 误判账目 + 框架判例）。
回归 = 输入视频 + 该标签，**绝不 diff runs/ 存档输出**（存档全部产自认证修正前代码）。

**重构终态宪法（一句话）**：stabilize 只做一致性修复（severity↔task_completion↔gap 文本矛盾收敛）
和归属搬运；一切品类/商业判断归模型（prompt 框架），代码不再做。
推论：partial 档代码不替模型定级、只查矛盾，禁止新增 "partial→medium" 这类映射规则。

**逐条处置清单（动手时按此执行，不止删牙膏）**：
- S3/S4 牙膏三件套（按压/闻香/功能效果正则）→ 删；
- `creator_not_worse`（中文正则，对马来语 gap 文本失明）→ 改读结构化字段；
- S6 CTA 检测（beli/cart 关键词）→ 待定，按样本检验；
- `has_real_endorsement` 否定正则（临时方案，打过地鼠）→ 被 endorsement tag 取代后删；
- `stabilize_improvement_priorities`、Q11 一致性契约 → 留。

**生死测量（删特例前的 go/no-go 门禁，预注册阈值后再跑）**：
1. 模型在 partial 阶段、品类权重 prompt 下的 severity 稳定性 + 与标签一致率
   （partial 占绝大多数，删特例后 S4 即模型原始判断；此量不过则保留薄兜底）；
2. task_completion 跨 run 稳定性；
3. 品类权重指令对 severity 的执行一致性（与 1 是不同测量目标，删特例前两个都要量）。
方法注意：`--repeat --reuse-existing` 量的是"冻结 facts 的条件方差"，低估生产总方差
（阶段一方差与 Phase C 效应不在内）；预先决定接受条件方差还是花钱测端到端。
阈值开跑前写死（如每阶段众数 ≥4/5、零 large↔small 对跳、标签一致率下限），不许测完再解释。

**子项：第三方背书检测改结构化标记（regex 做不了，规格已定）。**
现有两个 regex 消费者都该换成模型标记：`claims_my.py` 认证归属检测、`repair.py:has_real_endorsement`。
regex 无法表达"机构的数据是否在证明本产品价值"这种关系判断。
2026-06-10 已落地**生产端**：tag 定为 **evidence_unit 级** `third_party_endorsement`（比原计划的
stage/role 级更优：facts 锁定后单一来源、可同时服务认证搬运与 S5 检测、与 product_visible 同模式），
schema/fact prompt/normalize 已接通。**消费端（两处 regex 退役）仍归门禁后执行**——
先在 live run 观察 tag 质量，再切消费者。
**背书定义（已写入 commercial-judgement-framework S5 / prompt / QA Q16）**：
第三方背书 = ① 机构类型（监管认证 / 行业协会 / 评测中心实验室 / 高校研究 / 调研咨询 /
疾病防治中心）+ ② 关联性门槛（该机构的实验/数据/研究在**证明本产品价值**）同时成立；
仅提机构名字、赞助、合作 logo 而无证明本产品价值的数据 ≠ 背书；自述功效 ≠ 背书。

### 2. S4 severity 不稳（中，随 #1 解决）
现状：5 次重复 small/medium 跳变，根因为 #1 那套预存 S4 sensory 规则 × 模型方差。
注意：在 #1 完成前 QA-RULES §10 的 S4 稳定性预期不成立——此不稳是预存 WIP 引入，非分析链改进引入。

### 3. Phase C 回看质量（升级：首次真实执行即产出幻觉，2026-06-10 实证）
原待办（覆盖暗坑）：回看 severity 会被 `stabilize_stage_severity` 二次覆盖——随 #1 终态自然消解
（stabilize 改读 task_completion 后，回看更新 task_completion 即可生效）。
新增（kakwan S6 实证）：Phase C 首次真实触发即幻觉——把无明确 CTA 的达人判为
"明确告知链接在购物车"（实际口播是 "review pun ada dekat background"，whisper 转糊成
rintik/beg kau 提供了错误锚点），将教科书级 large 翻成 small。
确定性触发器标低置信是**对**的，回看反而帮倒忙。要修三处：
1. 回看 system prompt 删除"回看后如果达人持平或更优，必须给 small"的倾向性压力
   （疑似诱导模型为"持平"编造证据）；
2. 回看输出接地约束：声称的口播事实必须能对上转写或标 voice_only，叙事结论受 #6 校验；
3. 输入规范（写入 spec §0）：切片时间窗、缓冲秒数、prompt 告知"边界可能有误差、按功能判断"。

### 4. spec §0：素材包 + 两阶段架构契约（中，开工第一步）
把现状写成契约：预处理产物清单（各轨/帧/标记，谁生成、是否可选、失败如何降级）、
两阶段事实抽取 + video_facts 字段、analysis-output-schema 为字段唯一真相源。
原则：只描述实际存在的东西，不写愿景。好处：挡掉"基于过时结构/臆想决策"的提案。
2026-06-10 追加内容清单：endorsement tag 语义判例（含边界例：自称用三年、评论截图、赞助 logo）、
Phase C 输入规范（见 #3）、stabilize 终态宪法（见 #1）、QA §10 PASS 重定义原则
（4 样本 × 预注册阈值 × 标签一致率；旧 PASS 围绕旧 stabilize + 单一牙膏 run 校准，重构后不适用）、
框架判例五条（见 ground-truth-labels.md 末节，含省钱前提、CTA 过度=硬推、S1/S6 位置先验等）。
各文档（framework/prompt/QA）只引用不复制，spec 为唯一真相源。
顺带重审 prompt"severity 至少出现 2 种"的强制差异化（人为方差源候选）。

### 5. 死代码确认与清理（小）
`prompt.py:render_stage_frame_markdown` 未被 `write_analysis_input` 调用（已有 TODO 标注）；
确认 `artifacts.get_stage_frame_entries` 是否仅服务它。按规范需确认后再删。

### 6. 叙事文本与证据一致性校验（新缺陷类，2026-06-10 双向实证）
gap/summary 叙事字段从不对照转写证据：are_xie S6 假阴性（转写近半是 CTA 却写"视频在有效
CTA 前结束"）/ kakwan S6 假阳性（无 CTA 却写"明确告知购物车"）——同一缺陷类的两个方向。
quote 字段有 `validate_transcript_attribution` 管，叙事文本无人管；`align_timed_cta` 修了
quote 但 gap 不跟着改，产出"证据是一串购买指令、gap 却说没有 CTA"的自相矛盾结果。
方向：轻量一致性检查（如 S6 gap 声称"无 CTA"但 quote 含购买指令 → 警告/重写），
或并入 #1 让 gap 要点由结构化字段生成。

### 7. 观察指引补"镜头语言/取景完整性"维度（小）
歪斜、主体只见局部（kakwan S3 实证：马桶只拍到一边）当前 per-frame 维度抓不到，
却是 S3 执行性差距的有效信号。补进 observation-guide §2 抽帧观察框架。

## 下次开工顺序（2026-06-10 复盘定稿）

1. ~~人工定标 3 样本~~ ✅ → `references/ground-truth-labels.md`；
2. ~~spec §0~~ ✅ → `ARCHITECTURE.md §0`（含判例 / Phase C 输入规范 / 终态宪法 / QA 重定义原则）；
   同批落地：Phase C 三修（#3 前半）、Q19 叙事一致性软警告（#6 v1）、框架判例五条、
   观察指引镜头语言（#7）、endorsement tag 生产端（unit 级）、死代码清理（#5）；
3. ~~生死测量~~ ✅ 已跑（2026-06-11 00:06，15/15 成功）→ **GATE RESULT: NO-GO**
   （runs/_gate/status.log 102-165 行）。T2 挂 7 阶段、T3 对跳 4 处、T4 一致率 10/18、
   T6 2/5、T7 失败。三个 large↔small 错位全部溯源到 2026-06-09/10 的 prompt 判例注入
   （S5 背书内容→全线膨胀；两极逻辑→压掉 tasha S4 真 large；硬推判例→tasha S6 误判 large）。
   **判例是全局校准旋钮，新增判例必须带品类适用条件 + 过 gate 回归。**
   亮点：are_xie S1-S4 稳准、kakwan S4/S6 双中（模型裸判即识破缺 CTA，Phase C 幻觉坐实）。
   待复议：are_xie S6 模型 5/5 稳定 small vs 标签 medium（稳定偏离，需用户再判）。
4. **← 当前位置（NO-GO 后的新路线）**：
   a. ~~修 task_completion 枚举~~ ✅（2026-06-11 fb3b9cc）：prompt 14a/schema/ANALYSIS-PROMPT
      强制三值+达人侧语义；parse.normalize_task_completion shim（38 单测）。
      **回扫结果（16/18 稳定）**：映射后完成度判断 16/18 阶段众数≥4/5（12 个满票），
      vs severity 仅 11/18——"模型事实稳、加权判断飘"论点定量成立，gating 地基恢复。
      唯二不稳均为 S4（are_xie missing/partial、tasha complete/partial）：与 severity 对跳、
      Phase C 动机三线合一，S4=系统真不确定性热点，交回看机制不强求字段层解决。
      遗留：模型 raw 是否真输出枚举（T5 raw 合规）待下次 gate live run 确认（与 4b 共用一轮）。
      标签复议：are_xie S6 medium→small（用户 2026-06-11，T4 基线变 11/18）。
   b. ~~prompt 校准迭代~~ ✅已跑（2026-06-11 round2，15 调用）→ **仍 NO-GO，净倒退**：
      一致率 11/18→9/18、对跳 4→6 处。四靶仅 are_xie S5 方向修正（T7 过但 2/5 不稳）、
      tasha S4 s→m 半步；代价：are_xie S2（信任溢入）/S4、tasha S5（极性 bug 复发：达人更优
      判 large）三处原本正确的被打偏。kakwan S5 划算感归 S2 指令被无视。
      **两轮量化结论：prompt 判例调 severity 不收敛**——每条规则全局扩散，修一伤二。
      raw 层乱、后处理层 kakwan S4/S5 全中——护栏含金量被反向证明。
      极性 bug 系统性（tasha S6 两轮、S5 round2）：模型反复把"达人显著更优"判 large。
      T5 枚举合规仍≈1/5 且词表在膨胀（no_gap/different_focus 新词）——14a 指令基本无效。
   c. stabilize 维持现状不删（known-devil；kakwan S3 误触发已知，由标签记录在案）。
   d. **→ 架构提案（用户已采纳方向，2026-06-11）**：severity 改为从稳定事实层确定性推导，
      不再由模型直出。用户从业务侧独立梳理出同构设计（V2.1：E×L 公式+低置信自检），三条
      定稿原则：① E 由代码从稳定事实推导，模型不直出；③ 品类痛点清单进权重表（数据），
      模型只报卖点事实，命中查表；④ 事实不支撑则不判断（S5 双方无真背书→均未涉及）。
      **离线校验已完成（scripts/dev_derive_severity.py，零 LLM 成本，对两轮门禁 30 份 raw）**：
      一致率 round1 14/18、round2 16/18（模型直判基线 11/18、9/18），对跳 6→1。
      round2 过 T4 预注册线（≥15/18）。剩余 4 个失分**全部记账到事实层**：
      kakwan S3（镜头语言提取盲区→observation-guide）、kakwan S6（2026-06-11 母语同事三遍
      重听终裁：达人确有一句极弱敷衍 CTA——模型听觉平反，缺的是强度分级；已加 E=1.5 敷衍档，
      medium vs large 邻级差留给权重数据积累去填，用户裁决不现在拧）、r1 时代 are_xie S4/
      kakwan S2 事实文本不稳（r2 已自愈）。推导层零失分。
      用户三裁决（2026-06-11）：弱 CTA 敷衍档 E≈1.5；清真=马来市场准入基线不构成差异化背书
      （判例已入 commercial-judgement-framework.md）；权重表靠后续对比数据积累渐进优化，不一次定死。
      管线落地需新增事实字段（替换离线代理）：gap_direction 枚举（替代 severity 二值化代理）、
      S4 verification_mode（动作演示/口头宣称——tasha S4 实测方向词五次两极漂移而画面事实
      五次一致，必须锚定观察事实）、背书层级（权威认证 vs 清真等基线合规——判例待用户确认）、
      钩子痛点命中。L_link×L_product 标量不够表达，已合并为品类×阶段权重表 W。
      ⚠️ 诚实声明：权重 W 在同一批 18 标签上调出，此为可行性上限证明非泛化验证，
      需新样本做出样本外测试。
      注：此为终态宪法的修订——"商业判断归 prompt"两轮实测不成立，判断政策应为代码中的
      品类权重表，模型只供事实。
   e. **管线落地 ✅（2026-06-11）**：阶段 2 新增两侧独立执行分 creator/benchmark_execution
      （0/0.5/1/2，先打分再对比）+ 顶层 category_profile（模型报品类事实，权重政策在代码）；
      新建 postprocess/derive.py（原型 W 表 + E=标杆−达人 + S5 背书门槛/S4 演示差分/S1 痛点
      差分 + 红线 + 逐阶段算法溯源 severity_derivation），挂在 stabilize_stage_severity 之后、
      improvement 重排之前；字段缺失/异常优雅降级保留模型 severity（存量数据回放验证不变）。
      ~~下一步：带新字段重跑门禁验证执行分稳定性~~ ✅已跑（2026-06-11 round3，15 调用+2 废件）。
   f. **round3 结果（执行分实弹验收）**：
      - 途中破案并修复：stage2 payload 的"输出要求"字段清单硬编码、不读 analysis_input.md——
        14b/14c 起初没进请求体（烧 2 次发现止损）；同时解释了 T5 悬案（14a 从没送达过 stage2，
        真送达后 task_completion 枚举立即全规范）。
      - **执行分事实层很稳**：90 个单侧打分序列绝大多数 5/5 全同或单步摆动（0.5↔1），
        方向翻转全程仅 1 处；E 的极性事故结构性消失。
      - 双口径：实际推导 9/18（severe 2、对跳 1）；**痛点命中 oracle 口径 15/18、severe 0**
        ——失分 6 个全在 C 系数词法匹配（模型痛点词与 gap 文本语言/粒度错位，分词修复仍不可靠），
        3 个在事实层。模型直判本轮 ≈11/18 且 tasha S5/S6 仍有 l↔s 极性对跳。
      - **机器纠偏实录**：kakwan S5 模型直判 large×5（划算感误归背书第三轮复发）→ S5 背书门槛
        全部拦截纠正 small×5 正中标签；tasha S6 模型 large×3（极性 bug）→ 推导 small×5 ✓。
      - derive.py 两修：原型选择加 price_tier=low+functional→冲动品规则（模型 decision_threshold
        对马桶刷 4:1 摆，price_tier 稳）；painpoints 复合串分词。
   g. ~~下一迭代~~ ✅三项已落（2026-06-11，commit eac6b36）：① painpoint_relevance 枚举字段
      （C 去词法化）；② 执行分 0.5 锚点扩义；③ 镜头语言进 Phase A。存量重放 severe 全归零。
      另从 TabAI"六通道有效性"文档（references/fact-effectiveness-channels.md）吸收四项：
      孤证规则（S5 口播提及无画面佐证→执行分≤0.5）、"无法有效接收→0.5"原则（看不清/听不清/
      读不完）、UI 危险区+遮挡观察维度（Phase A + observation-guide）；伪精度数字（10度/0.3秒/
      confidence阈值）与六通道全量 checklist 拒收（fps3 测不了 + 指令稀释实测教训）。
   h. **Phase C 升级（待做，代码侧）**：回看触发加优先级排序（P1 S1/S6 低置信必看、
      P2 S4 声画冲突）+ **临界分值触发**——derive 的 S 落在 large 阈值邻域（如 2.3-2.7）时
      送 Phase C 复核事实再重推导。S 确定性化之后这才变得可行（4d 红利）。
      字幕停留时长可由 subtitle_track.json 时间戳确定性计算——有失分实证再建（先测后做）。
5. 重测过门禁后 → 按 #1 处置清单执行；验收按新 QA 定义，回归只对"输入 + 标签"。

## 观察后再决定（先测后做，目前数据不支持建）

- **creator_script 照搬标杆口播检查**：实测 5 条最高相似度 0.40，未发生照搬。
  若将来真观察到，做**确定性代码校验**（非 prompt 自检），仅同语言射程。
- **aigc_prompt 对锚帧的接地检查**：实测接地良好（仅 C5"购物袋"轻微外引）；
  且重合度判不了接地（改造 vs 白描天然低重合）。真要做须语义手段，非正则。

## 已否决的提案（避免重复提）

- 本地 CV 产品轨 `product_track.json`：无参考图、首帧定模板对达人视频失效 → 已改寄生 evidence_units。
- 音频分轨 / 人声分离：VidLingo 已验证不划算。
- 音频质量探针：对短视频分析无价值。
- 轻量 VL 替代主模型抽取：打碎 video+audio 跨模态融合，未必更省。
- evidence_unit 加 `stage_hint`：违反"看功能不看模式"宪法 + 锚定放大漂移 + 省的成本不存在。
- evidence_unit 加 `voiceover_tone`：信号已在 `audio_fact`；评价部分属阶段二 `voice_performance`。
- evidence_unit 加 `representative_frame_uri`：时间→帧已由代码确定性解析，模型出 URI 有幻觉风险。
- 商业权重第 0 步移到最后 / severity 对权重失明：与"品类权重进 severity"的既定决定冲突；
  可审计性已由 `task_completion`(工艺) vs `severity`(影响) 双信号在后台满足。
