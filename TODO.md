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

**子项：第三方背书检测改结构化标记（regex 做不了，规格已定）。**
现有两个 regex 消费者都该换成模型标记：`claims_my.py` 认证归属检测、`repair.py:has_real_endorsement`。
regex 无法表达"机构的数据是否在证明本产品价值"这种关系判断。改法：模型逐 stage/role 输出
`has_institutional_endorsement`（bool，按下方定义判），代码两处都读它、regex 退役。
**背书定义（已写入 commercial-judgement-framework S5 / prompt / QA Q16）**：
第三方背书 = ① 机构类型（监管认证 / 行业协会 / 评测中心实验室 / 高校研究 / 调研咨询 /
疾病防治中心）+ ② 关联性门槛（该机构的实验/数据/研究在**证明本产品价值**）同时成立；
仅提机构名字、赞助、合作 logo 而无证明本产品价值的数据 ≠ 背书；自述功效 ≠ 背书。

### 2. S4 severity 不稳（中，随 #1 解决）
现状：5 次重复 small/medium 跳变，根因为 #1 那套预存 S4 sensory 规则 × 模型方差。
注意：在 #1 完成前 QA-RULES §10 的 S4 稳定性预期不成立——此不稳是预存 WIP 引入，非分析链改进引入。

### 3. Phase C 回看 severity 被二次覆盖（中）
现状：`apply_stage_review_updates` 重跑全链，回看判出的 severity 会被 `stabilize_stage_severity` 再覆盖。
倾向方案：把 stabilize 的"下压 small"规则 gate 在 `task_completion` 上——Phase C 可改 task_completion，
从而设定品类规则的事实前提，而非两个权威对撞（详见 session 讨论）。
触发：等 Phase C 在某真实 run 首次触发、确认覆盖确实产出错 severity 后再做 + 重验。

### 4. spec §0：素材包 + 两阶段架构契约（中）
把现状写成契约：预处理产物清单（各轨/帧/标记，谁生成、是否可选、失败如何降级）、
两阶段事实抽取 + video_facts 字段、analysis-output-schema 为字段唯一真相源。
原则：只描述实际存在的东西，不写愿景。好处：挡掉"基于过时结构/臆想决策"的提案。

### 5. 死代码确认与清理（小）
`prompt.py:render_stage_frame_markdown` 未被 `write_analysis_input` 调用（已有 TODO 标注）；
确认 `artifacts.get_stage_frame_entries` 是否仅服务它。按规范需确认后再删。

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
