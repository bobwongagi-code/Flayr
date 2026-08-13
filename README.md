# Flayr · TikTok 带货短视频分析与提升工具

> 使用说明 | 2026-05-31

Flayr 接收一条**爆款参考视频**和一条**达人视频**，结合连续画面、转写、本地音频质检和多模态模型逐段对比差距，产出一份可直接执行的提升报告（HTML）和改进版视频的拍摄/剪辑建议。Flayr 不生成替换视频、达人音色视频或 AI 示意视频。

---

## 一、能力概览

| 能力 | 说明 |
|------|------|
| 视频转写 | 北京 MaaS Fun-ASR 在线转写，支持东南亚语言（马来语/泰语/印尼语）自动识别，提供句级与词级口播时间戳 |
| 视频理解 | canonical frames/时间线作为可审计主证据；原生视频只用于一次定向连续性复核 |
| 音频边界 | 音量、静音和峰值风险是可复核硬质检；语气/BGM/音效只作观察，不进入差距等级 |
| 结构化分析 | 对照 Chimera 6 槽位结构库（S1-S6），逐段对比达人与爆款差距 |
| 改进建议 | 按 GMV 杠杆排序的提升点，含话术、画面和执行建议 |
| HTML 报告 | 可视化主报告，含关键结论、差距概览、阶段拆解与提升点 |

---

## 二、两阶段分析架构

生产推荐路由固定为 `qwen3-vl-plus` 负责视觉观察，`qwen3.7-plus` 负责资格、比较、综合与世界知识判断。两者共用同一份 Evidence Ledger、resolver 和 Phase C 补丁合同，不存在 VL 专属的第二套事实或判断系统。`qwen3.6-plus` 只保留为人工指定的 judgment 备份，使用时仍与 `qwen3-vl-plus` 配对，不会在 3.7 失败时自动接管；`qwen3-vl-flash` 已退役并由 provider 边界拒绝。

Flayr 用**两阶段 pipeline + 一次性回看**，而非一次性看完整视频：

```
阶段一：单视频事实抽取（fact extraction）
  对达人、标杆分别跑 Stage1-A：
  canonical 关键帧/时间线 + 窗口安全 ASR + OCR（provider 支持时可附独立音频）
  → 产出带时间戳的 evidence_units（画面/口播/字幕/音频事实）
  若资格或连续性仍未闭合，Stage1-C 对目标时间窗最多补观察一次
  facts 一旦锁定即为"唯一事实源"（防止达人/标杆串证据）

阶段二：对比判断（comparison）
  只喂入冻结后的两条 facts 文字（事实基线），不再附带视频
  → 按 S1-S6 功能阶段横向对比，产出 severity、key_conclusions、改进建议
  感官素材仅辅助判断，不可新增/改写 facts

Phase C：低置信阶段回看（只触发一次）
  只有代码发现覆盖、资格、连续性或 resolver 冲突时触发（最多 2 个 S1-S6 阶段）；模型自报低置信不能单独触发
  → 代码按该阶段真实时间窗切标杆/达人原生视频画面，并附同窗 Fun-ASR 文本
  → 第二次只重判这些阶段，并重新走现有 postprocess/validate
  不做无限多轮，也不允许模型继续索要素材
```

设计理由：抽帧主账本提供更高的阶段覆盖和可审计性；原生视频实验没有带来 Stage2 净准确率收益，因此只用于少数连续性/资格复核，不建立第二套事实系统。
详见 `ARCHITECTURE.md`。

---

## 三、目录结构

```
Flayr/
├── scripts/
│   ├── flayr.py                  # CLI 主入口
│   ├── batch_analyze.py          # 批量作业、断点续跑与限并发
│   ├── evaluate_analysis.py      # 分析结果与人工 GT 对照
│   ├── manage_validation_cohort.py # 冻结/校验/消费 blind cohort（不调模型）
│   ├── verify_analysis_contracts.py # S1-S6 与跨模块契约门
│   └── flayr_core/               # 核心模块包
│       ├── video.py asr.py       # 在线转写 + 抽帧 + 抽音频
│       ├── translation.py        # 转写翻译
│       ├── prompt.py             # analysis_input.md 装配
│       ├── artifacts.py          # 帧/时间区间选取
│       ├── video_evidence.py     # 去重审计、联系表、timeline 证据视图
│       ├── analysis_model.py      # 结果领域模型、字段投影和生命周期合同
│       ├── report.py             # HTML 报告渲染
│       ├── llm/                  # LLM 调用层
│       │   ├── api.py            #   HTTP 调用 + 视频/音频/图片转 data URL
│       │   ├── analysis_contract.py # 结果最小运行时契约
│       │   ├── json_codec.py     #   JSON 文本容错解析
│       │   ├── product_profile.py #  产品地基与证明合同归一化
│       │   ├── payload.py        #   请求 payload 构造（两阶段）
│       │   ├── parse.py          #   响应解析 + 归一化
│       │   └── pipeline.py       #   分析主入口 + 分段编排/最终收口
│       └── postprocess/          # 结果后处理流水线
│           ├── chain.py          #   流水线编排（说明书式）
│           ├── repair.py         #   内容修补
│           ├── validate.py       #   通用校验
│           ├── claims_my.py      #   MY 市场认证主张专项
│           └── health_rewrite.py #   健康品类合规重写
├── QA-RULES.md                   # 分析结果校验规则
├── structure_library_full.md     # Chimera 结构库（32 模块定义，进 LLM 输入）
├── references/                   # 分析知识库（进 LLM 输入）
│   ├── analysis-output-schema.json   # 模型输出契约（字段唯一真相源）
│   ├── observation-guide.md          # 视频观察指引（看视频的方法）
│   ├── commercial-judgement-framework.md
│   ├── brand_propositions.json      # 冻结命题与痛点键
│   ├── ground-truth-labels.md/.json # 人工 GT 理由版/机器版
│   ├── market-knowledge-my.md
│   ├── validation-inputs.json        # 主验证集与留出集的视频输入清单
│   └── commerce-translation-guidelines.md
├── assets/report.html            # 报告模板
└── runs/                         # 每次分析的输出目录
```

---

## 四、快速开始

### 依赖

```bash
# Python 3.11+
# ffmpeg, ffprobe（视频重编码 + 抽帧 + 抽音频）
# 在线 Fun-ASR 使用 curl 调用；ffmpeg 负责提取/压缩音频
# 可选报告增强：python3 -m pip install -r requirements-dev.lock
```

Python 依赖、外部工具边界和升级规则见 [DEPENDENCIES.md](DEPENDENCIES.md)。
源码版本和发布流程见 [VERSION](VERSION) 与 [RELEASE.md](RELEASE.md)。

### 基本用法

```bash
python3 scripts/flayr.py \
  --benchmark-video 爆款.mp4 \
  --creator-video 达人.mp4 \
  --product-name "儿童牙膏" \
  --judgment-model qwen3.7-plus \
  --vision-model qwen3-vl-plus \
  --llm-api-url https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions \
  --max-total-wall-time 3600 \
  --llm-api-key-env DASHSCOPE_API_KEY \
  --verification-stage production \
  improve
```

### 关键参数

| 参数 | 说明 |
|------|------|
| `--benchmark-video` | 爆款参考视频路径 |
| `--creator-video` | 达人视频路径 |
| `--product-name` | 产品名称 |
| `--judgment-model` | Step-0、Stage1-B、Stage2/Stage3、综合与文本判断模型；当前推荐 `qwen3.7-plus` |
| `--vision-model` | OCR、Stage1-A、Stage1-C、Phase C 与视频身份观察模型；当前推荐 `qwen3-vl-plus`，必须与 `--judgment-model` 同时提供 |
| `--llm-model` | 旧单模型兼容入口；同一模型承担全部职责，不能与双模型参数混用。仅用于历史严格回放，不是生产备份或推荐路径 |
| `--llm-api-url` | 已批准供应商的 Chat Completions 端点；当前网络策略允许 OpenAI、DashScope 官方域名和登记的北京 MaaS Qwen 端点 |
| `--max-total-wall-time` | 单次运行总墙钟上限；默认 1800 秒，慢模型验证可显式提高，例如 3600 秒 |
| `--llm-api-key-keychain-service` | macOS Keychain 服务名（或用 `--llm-api-key-env` 走环境变量） |
| `--llm-include-images` | 默认启用：完整 Step-0 + 单视频事实抽取 + 分段 Stage2/Stage3 + Phase C；`--no-llm-include-images` 仅作为已拒绝的旧路径标志保留 |
| `--asr-api-url` | 在线 Fun-ASR endpoint；默认使用北京 MaaS 地址 |
| `--asr-model` | 在线 ASR 模型；默认 `fun-asr-flash-2026-06-15` |
| `--asr-language` | ASR 语言提示；默认 `auto` |
| `--asr-api-key-env` | 在线 ASR 使用的 key 环境变量；默认 `DASHSCOPE_API_KEY` |
| `--ocr-mode auto/on/off` | 字幕 OCR 轨。默认 `auto`：复用分析模型的视觉能力和 key；`off` 可关闭 |

Web worker 使用同一条路由：同时设置 `FLAYR_JUDGMENT_MODEL=qwen3.7-plus` 与
`FLAYR_VISION_MODEL=qwen3-vl-plus`；只设置其中一个会在任务启动前失败。仅在两者都未设置时，
才读取旧的 `FLAYR_LLM_MODEL` 单模型兼容变量，不会自动回退到 `qwen3.6-plus`。

需要人工观察 3.6 时，显式使用 `--judgment-model qwen3.6-plus --vision-model qwen3-vl-plus`；
这只是一次新的、可审计的模型选择，不是运行失败后的自动 fallback。

> 注：在线 Fun-ASR 是 compare/improve 的语音证据依赖；调用失败时默认返回非零，不会发布为完成状态。只有显式使用 `--allow-degraded` 才会继续生成降级报告，并写入 `degraded` 状态；不会伪造缺失的转写或证据。

---

## 五、工作流程

```
视频输入
  ↓
[1] 在线转写 + 抽帧 + 抽音频（Fun-ASR + ffmpeg）
  ↓
[2] 翻译（可选，LLM）
  ↓
[3] 阶段一：canonical 帧/时间线 + ASR/OCR 抽取事实；必要时 Stage1-C 定向看一次原生片段
  ↓
[4] 阶段二：只读冻结 facts 做分组判断，不附视频
  ↓
[5] Phase C 可选回看（独立结构信号触发，原生片段只生成受限事实补丁一次）
  ↓
[6] 校验 + 修补（postprocess chain + QA-RULES）
  ↓
[7] 渲染报告（report.html）
  ↓
输出到 runs/<时间戳>/
```

---

## 六、输出产物

| 文件 | 说明 |
|------|------|
| `report.html` | 主报告，可直接在浏览器打开 |
| `analysis.json` | 完整分析数据 |
| `analysis_result.json` | LLM 分析结果（归一化和统一后处理后） |
| `raw_model_response.json` / `validated_normalized_result.json` / `final_derived_result.json` | LLM 原始、校验规范化和最终派生结果 |
| `analysis_replay_context.json` / `postprocess_provenance` | 绑定确定性重放所需的分析上下文、输入哈希和规范化结果哈希；缺失或哈希不匹配时不得重放 |
| `postprocess_change_log.json` | 后处理字段变更、规则、证据和字段来源记录 |
| `video_facts_{benchmark,creator}.json` | 阶段一单视频事实清单 |
| `stage1_provider_{role}_{A|B|C}*.json` | Stage1-A/B/C provider 原始 JSON、完整请求身份、响应哈希、重试与 usage 元数据；可用 `--stage1-replay-from` 严格重放 |
| `stage2_provider_{GROUP}.json` | Stage2/Stage3 provider 原始 JSON、请求身份、响应哈希、重试与 usage 元数据；可用 `--stage2-replay-from` 严格重放 |
| `provider_asr.json` | Fun-ASR 原始响应、请求身份、响应哈希和执行来源；可随主运行严格技术重放 |
| `provider_compact_eval.json` | 独立 compact/cohort/control provider 原始响应；各评估入口支持严格技术重放 |
| `transcript.txt` / `.srt` / `.zh.txt` | 转写与翻译 |
| `frames/` `focus_frames/` | 抽取的关键帧 |
| `frames/analysis_manifest.json` / `analysis_stage_frames.json` | 由镜头、字幕、变化点和词级 ASR 边界共同生成的 canonical 模型输入帧集 |
| `frames/selection_report.*` | 全片帧去重审计，记录每帧 keep/drop 原因；不替代 canonical manifest |
| `contact_sheets/` | canonical Hook、CTA、S1-S6 的顺序联系表 |
| `timeline_views/` | canonical Hook、CTA 的帧序列 + 波形 + 口播证据图，并记录帧来源 |
| `transcript_packed.*` | 带时间戳的紧凑口播索引 |
| `video_evidence_audit.json` | 二级证据视图自检结果 |

---

### 分层 GT 验证

新 blind 样本必须先完成人工 `human_gap`、`stage_relations`、`key_events`、`stage_oracles` 和 `decision_gt`，再冻结 cohort。旧 `stages` 只能作为兼容投影：

```bash
python3 scripts/manage_validation_cohort.py freeze \
  --sample <sample-id> \
  --provider <provider-id> \
  --judgment-model qwen3.7-plus \
  --vision-model qwen3-vl-plus \
  --api-url <compatible-api-url> \
  --temperature 0 \
  --output runs/validation/<cohort-id>.lock.json
```

冻结时还必须显式锁定完整模型执行配置：生成上限、top-p/seed、response format、stop 序列、传输重试、完成尝试次数，以及 connect/read/low-speed/overall timeout；缺少 `FLAYR_VALIDATION_ROOT`、代码提交、prompt/schema/evaluator/GT/video identity 或模型配置 hash 时，freeze 会返回 `CohortFreezeStatus BLOCKED`。

`evaluate_analysis.py --cohort-lock ...` 会分别报告预处理可用性、Stage1 事实召回、Stage2
证据使用/判断、derive oracle 回放、Phase C 净收益和 Top-N 商业根因。cohort 结果一旦打开或用于
修改规则，须执行 `manage_validation_cohort.py spend`，该批样本以后只作 `seen_validation` 回归。

验证清单中的视频路径使用 `${FLAYR_VALIDATION_ROOT}` 占位符。运行冻结或评测前，需在本地环境设置该变量；真实视频目录不应写入仓库或作业清单。

### 代码修复后的回放

确定性后处理只使用已经验证的 canonical 结果和同一次运行的
`analysis_replay_context.json`、`analysis_input.md`。运行：

```bash
python3 scripts/replay_finalization.py <source-run> <new-output-dir>
```

这个命令在 provenance 缺失、规范化结果、分析上下文或输入哈希不一致时直接失败，
不会读取视频、调用 ASR 或调用 LLM。Stage1/Stage2 provider 结果则分别使用
`--stage1-replay-from` / `--stage2-replay-from`；请求身份变化时必须语义重跑，不能静默混用。

---

## 七、设计原则

1. **模态分工明确**：视觉模型负责可见事实和定向视频复核；在线 Fun-ASR 是口播语义权威源；判断模型只读冻结事实，不假装直接看过或听过原视频
2. **关注变化点**：预算内自适应基础帧叠加镜头、字幕、局部变化和词级口播边界，模型消费统一的 canonical manifest
3. **事实与判断分离**：阶段一锁定事实防串供，阶段二在事实基线上做感官判断
4. **按证据形态切换主骨架**：有口播用口播时间线，无口播则切到字幕/OCR、画面变化、镜头轨和音频节奏
5. **状态明确**：可选依赖缺失记录 `degraded`；已请求的 API、模型输出或 schema 失败返回非零；没有完成模型分析时，对比/改进默认失败，只有显式 `--allow-degraded` 才能继续
6. **证据可追溯**：每个结论都绑定时间点和画面/口播证据
7. **GMV 导向**：所有建议围绕停留、信任、下单转化
8. **本地化**：话术用达人原语言，适配东南亚市场

## 八、Bitter Lesson 落地门禁

Flayr 的结论来自证据供应链，不是一次模型调用。冻结的层边界、字段类型、不变量、
非目标和验收顺序位于 [`references/bitter-lesson-frozen-spec.json`](references/bitter-lesson-frozen-spec.json)。
实现前后的判断清单位于 [`references/bitter-lesson-implementation-audit.md`](references/bitter-lesson-implementation-audit.md)。

测试前会验证冻结规格和契约测试哈希：

```bash
PYTHONPATH=scripts python3 scripts/verify_bitter_lesson_contract.py
```

开始一次代码批次前，必须用冻结的文件和行数预算检查范围；超出预算就拆批次，不能在同一批次继续扩张：

```bash
PYTHONPATH=scripts python3 scripts/check_change_scope.py --base-ref HEAD
```

真实视频的验收顺序固定为 `fixture -> offline replay -> fake provider -> ordinary sample -> boundary sample`。
真实视频不再同时承担开发调试、回归验证和最终验收三种角色。
