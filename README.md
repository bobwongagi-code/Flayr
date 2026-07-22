# Flayr · TikTok 带货短视频分析与提升工具

> 使用说明 | 2026-05-31

Flayr 接收一条**爆款参考视频**和一条**达人视频**，结合连续画面、转写、本地音频质检和多模态模型逐段对比差距，产出一份可直接执行的提升报告（HTML）和改进版视频的拍摄/剪辑建议。Flayr 不生成替换视频、达人音色视频或 AI 示意视频。

---

## 一、能力概览

| 能力 | 说明 |
|------|------|
| 视频转写 | Whisper 本地转写，支持东南亚语言（马来语/泰语/印尼语）自动识别，提供权威口播时间戳 |
| 视频理解 | 多模态视觉模型读取连续画面；口播语义来自 Whisper，音轨由本地技术质检补充 |
| 音频边界 | 音量、静音和峰值风险是可复核硬质检；语气/BGM/音效只作观察，不进入差距等级 |
| 结构化分析 | 对照 Chimera 6 槽位结构库（S1-S6），逐段对比达人与爆款差距 |
| 改进建议 | 按 GMV 杠杆排序的提升点，含话术、画面和执行建议 |
| HTML 报告 | 可视化主报告，含关键结论、差距概览、阶段拆解与提升点 |

---

## 二、两阶段分析架构

Flayr 用**两阶段 pipeline + 一次性回看**，而非一次性看完整视频：

```
阶段一：单视频事实抽取（fact extraction）
  对达人、标杆各跑一次 omni 调用：
  原生视频（ffmpeg 重编码 fps=3 + 降分辨率，含音轨）→ 让模型自定位变化点
  → 产出带时间戳的 evidence_units（画面/口播/字幕/音频事实）
  facts 一旦锁定即为"唯一事实源"（防止达人/标杆串证据）

阶段二：对比判断（comparison）
  喂入两条 facts 文字（事实基线）+ 每条 evidence 对应的关键帧 + 切片音频
  → 让判断环节能"看着证据、听着声音"评估声画质感与情绪强度
  → 按 S1-S6 功能阶段横向对比，产出 severity、key_conclusions、改进建议
  感官素材仅辅助判断，不可新增/改写 facts

Phase C：低置信阶段回看（只触发一次）
  如果阶段二输出 low_confidence_stages，或代码发现证据不足（最多 2 个 S1-S6 阶段）
  → 代码按该阶段真实时间窗切标杆/达人原生视频片段（含音轨）
  → 第二次只重判这些阶段，并重新走现有 postprocess/validate
  不做无限多轮，也不允许模型继续索要素材
```

设计理由：阶段一锁定事实防串供，阶段二重获感官避免"读文字摘要做判断"；Phase C 只补“代表帧不足以判断”的少数阶段。
详见 `ARCHITECTURE.md`。

---

## 三、目录结构

```
Flayr/
├── scripts/
│   ├── flayr.py                  # CLI 主入口
│   ├── batch_analyze.py          # 批量作业、断点续跑与限并发
│   ├── dev_test_stage2.py        # 阶段二独立测试工具（复用阶段一产物，调 prompt 用）
│   ├── evaluate_analysis.py      # 分析结果与人工 GT 对照
│   ├── manage_validation_cohort.py # 冻结/校验/消费 blind cohort（不调模型）
│   ├── verify_analysis_contracts.py # S1-S6 与跨模块契约门
│   └── flayr_core/               # 核心模块包
│       ├── video.py whisper.py   # 转写 + 抽帧 + 抽音频
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
│       │   └── pipeline.py       #   分析主入口 + 校验/repair 编排
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
# whisper-cli (whisper.cpp) + 模型文件
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
  --whisper-model /path/to/ggml-large-v3-turbo-q5_0.bin \
  --llm-model qwen3.6-plus \
  --llm-api-url https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions \
  --llm-api-key-env FLAYR_LLM_API_KEY \
  improve
```

### 关键参数

| 参数 | 说明 |
|------|------|
| `--benchmark-video` | 爆款参考视频路径 |
| `--creator-video` | 达人视频路径 |
| `--product-name` | 产品名称 |
| `--llm-model` | 多模态视觉模型名称（推荐 `qwen3.6-plus`） |
| `--llm-api-url` | OpenAI 兼容 API 端点 |
| `--llm-api-key-keychain-service` | macOS Keychain 服务名（或用 `--llm-api-key-env` 走环境变量） |
| `--llm-include-images` | 默认启用：完整 Step-0 + 单视频事实抽取 + omni 对比链；`--no-llm-include-images` 仅保留给旧文本路径兼容调试 |
| `--whisper-model` | Whisper 模型文件路径 |
| `--skip-whisper` | 跳过转写（用于调试） |
| `--ocr-mode auto/on/off` | 字幕 OCR 轨。默认 `auto`：复用分析模型的视觉能力和 key；`off` 可关闭 |

> 注：可选预处理或本地增强依赖不可用时，系统会在运行状态中记录 `degraded` 及原因，并继续生成不依赖该能力的产物；不会伪造缺失的证据。已请求的 LLM 调用、响应解析或 schema 校验失败时，任务返回非零，不会发布为完成状态。

---

## 五、工作流程

```
视频输入
  ↓
[1] 转写 + 抽帧 + 抽音频（Whisper + ffmpeg）
  ↓
[2] 翻译（可选，LLM）
  ↓
[3] 阶段一：单视频事实抽取（omni 原生视频，各跑一次）
  ↓
[4] 阶段二：对比判断（facts + 关键帧 + 切片音频）
  ↓
[5] Phase C 可选回看（低置信阶段原生视频片段，只重判一次）
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
| `raw_model_response.json` / `validated_normalized_result.json` / `final_derived_result.json` | LLM 原始、校验规范化和最终派生结果；请求与临时响应不落盘 |
| `postprocess_change_log.json` | 后处理字段变更、规则、证据和字段来源记录 |
| `video_facts_{benchmark,creator}.json` | 阶段一单视频事实清单 |
| `transcript.txt` / `.srt` / `.zh.txt` | 转写与翻译 |
| `frames/` `focus_frames/` | 抽取的关键帧 |
| `frames/selection_report.*` | 全片帧去重审计，记录每帧 keep/drop 原因 |
| `contact_sheets/` | Hook、CTA、S1-S6 的顺序联系表 |
| `timeline_views/` | Hook、CTA 的帧序列 + 波形 + 口播证据图 |
| `transcript_packed.*` | 带时间戳的紧凑口播索引 |
| `video_evidence_audit.json` | 二级证据视图自检结果 |

---

### 分层 GT 验证

新 blind 样本必须先完成人工 `key_events`、`stage_oracles` 和 `decision_gt`，再冻结 cohort：

```bash
python3 scripts/manage_validation_cohort.py freeze \
  --sample <sample-id> \
  --model <model-id> \
  --api-url <compatible-api-url> \
  --temperature 0 \
  --output runs/validation/<cohort-id>.lock.json
```

`evaluate_analysis.py --cohort-lock ...` 会分别报告预处理可用性、Stage1 事实召回、Stage2
证据使用/判断、derive oracle 回放、Phase C 净收益和 Top-N 商业根因。cohort 结果一旦打开或用于
修改规则，须执行 `manage_validation_cohort.py spend`，该批样本以后只作 `seen_validation` 回归。

验证清单中的视频路径使用 `${FLAYR_VALIDATION_ROOT}` 占位符。运行冻结或评测前，需在本地环境设置该变量；真实视频目录不应写入仓库或作业清单。

---

## 七、设计原则

1. **全模态主导**：判断环节必须能看画面、听声音，不退化成读文字摘要
2. **关注变化点**：高密度连续帧交给模型自己找变化点，而非人工均匀抽帧替它决定看哪
3. **事实与判断分离**：阶段一锁定事实防串供，阶段二在事实基线上做感官判断
4. **按证据形态切换主骨架**：有口播用口播时间线，无口播则切到字幕/OCR、画面变化、镜头轨和音频节奏
5. **状态明确**：可选依赖缺失记录 `degraded`；已请求的 API、模型输出或 schema 失败返回非零；没有完成模型分析时，对比/改进默认失败，只有显式 `--allow-degraded` 才能继续
6. **证据可追溯**：每个结论都绑定时间点和画面/口播证据
7. **GMV 导向**：所有建议围绕停留、信任、下单转化
8. **本地化**：话术用达人原语言，适配东南亚市场
