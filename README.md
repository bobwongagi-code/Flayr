# Flayr · TikTok 带货短视频分析与提升工具

> 使用说明 | 2026-05-31

Flayr 接收一条**爆款参考视频**和一条**达人视频**，用全模态大模型（omni）"看画面 + 听声音"理解两条视频，逐段对比差距，产出一份可直接执行的提升报告（HTML）和一个改进版视频的拍摄/剪辑计划。

---

## 一、能力概览

| 能力 | 说明 |
|------|------|
| 视频转写 | Whisper 本地转写，支持东南亚语言（马来语/泰语/印尼语）自动识别，提供权威口播时间戳 |
| 全模态理解 | omni 模型（qwen3.5-omni-plus）原生吃视频：连续画面 + 完整音轨（BGM/语气/音效），自定位变化点 |
| 结构化分析 | 对照 Chimera 6 槽位结构库（S1-S6），逐段对比达人与爆款差距 |
| 改进建议 | 按 GMV 杠杆排序的提升点，含话术、画面、AI 改造 prompt |
| 提案样片 | Top 提升点自动切达人原片 3-5 秒，配本地话术和改造理由，嵌入 HTML 报告 |
| HTML 报告 | 可视化主报告，含关键结论、差距概览、阶段拆解、提升点与提案样片 |
| 改进视频计划 | improved_video_plan.json，供后续剪辑/AIGC 使用 |

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
│   ├── dev_test_stage2.py        # 阶段二独立测试工具（复用阶段一产物，调 prompt 用）
│   └── flayr_core/               # 核心模块包
│       ├── video.py whisper.py   # 转写 + 抽帧 + 抽音频
│       ├── translation.py        # 转写翻译
│       ├── prompt.py             # analysis_input.md 装配
│       ├── artifacts.py          # 帧/时间区间选取
│       ├── video_evidence.py     # 去重审计、联系表、timeline 证据视图
│       ├── proposal_clip.py      # Top 提升点提案样片结构化与原片切片
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
│   ├── analysis-output-schema.json   # 输出契约（字段唯一真相源）
│   ├── observation-guide.md          # 视频观察指引（看视频的方法）
│   ├── commercial-judgement-framework.md
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
```

### 基本用法

```bash
python3 scripts/flayr.py \
  --benchmark-video 爆款.mp4 \
  --creator-video 达人.mp4 \
  --product-name "儿童牙膏" \
  --whisper-model /path/to/ggml-large-v3-turbo-q5_0.bin \
  --llm-model qwen3.5-omni-plus \
  --llm-api-url https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions \
  --llm-api-key-keychain-service VidLingo.Qwen \
  improve
```

### 关键参数

| 参数 | 说明 |
|------|------|
| `--benchmark-video` | 爆款参考视频路径 |
| `--creator-video` | 达人视频路径 |
| `--product-name` | 产品名称 |
| `--llm-model` | 全模态模型名称（推荐 qwen3.5-omni-plus） |
| `--llm-api-url` | OpenAI 兼容 API 端点 |
| `--llm-api-key-keychain-service` | macOS Keychain 服务名（或用 `--llm-api-key-env` 走环境变量） |
| `--llm-include-images` | 默认启用：完整 Step-0 + 单视频事实抽取 + omni 对比链；`--no-llm-include-images` 仅保留给旧文本路径兼容调试 |
| `--whisper-model` | Whisper 模型文件路径 |
| `--skip-whisper` | 跳过转写（用于调试） |
| `--ocr-mode auto/on/off` | 字幕 OCR 轨。默认 `auto`：检测到 DashScope 配置和 key 且非 dry-run 时自动开启；`off` 可关闭 |

### 提案样片 AI 后端（可选）

默认只生成达人原片切片，不调用视频生成模型。需要 AI 示意样片时显式打开：

```bash
python3 scripts/flayr.py ... improve \
  --proposal-video-backend dashscope-i2v \
  --proposal-video-resolution 720P \
  --llm-api-key-keychain-service VidLingo.Qwen
```

| 参数 | 说明 |
|------|------|
| `--proposal-video-backend none` | 默认值，不调用 DashScope，仅输出原片切片 + 文案提案 |
| `--proposal-video-backend dashscope-i2v` | 用 Wan 图生视频生成 AI 示意样片；默认模型 `wan2.6-i2v-flash`，可直接使用本地关键帧 base64 |
| `--proposal-video-backend dashscope-s2v` | 用 `wan2.2-s2v` 数字人口播样片；要求公网可访问的正脸图和音频 URL |
| `--proposal-video-submit-only` | 只提交任务，不等待生成完成；报告显示 task_id |
| `--proposal-face-image-url` / `--proposal-line-audio-url` | `dashscope-s2v` 的公网素材兜底 URL |

说明：官方 `wan2.2-s2v` 只接受公网 HTTP(S) 图片和音频，不能直接吃本地文件；`dashscope-i2v` 更适合当前本地报告链路。

> 注：ffmpeg 不可用时，阶段一自动降级为"关键帧抽帧 + 完整音频"，不中断。

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
[7] 生成提案样片（proposal_clips.json + 3-5 秒达人原片切片）
  ↓
[8] 渲染报告（report.html）+ 改进计划（improved_video_plan.json）
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
| `llm_stage_review_request.json` / `llm_stage_review_response.json` | Phase C 低置信阶段回看请求与响应（仅触发时存在） |
| `video_facts_{benchmark,creator}.json` | 阶段一单视频事实清单 |
| `improved_video_plan.json` | 改进视频的拍摄/剪辑计划 |
| `proposal_clips.json` / `proposal_clips/*.mp4` | Top 提升点提案样片数据与达人原片切片 |
| `proposal_clips/proposal_*_ai.mp4` | AI 示意样片（仅启用 `--proposal-video-backend` 且任务成功时存在） |
| `transcript.txt` / `.srt` / `.zh.txt` | 转写与翻译 |
| `frames/` `focus_frames/` | 抽取的关键帧 |
| `frames/selection_report.*` | 全片帧去重审计，记录每帧 keep/drop 原因 |
| `contact_sheets/` | Hook、CTA、S1-S6 的顺序联系表 |
| `timeline_views/` | Hook、CTA 的帧序列 + 波形 + 口播证据图 |
| `transcript_packed.*` | 带时间戳的紧凑口播索引 |
| `video_evidence_audit.json` | 二级证据视图自检结果 |

---

## 七、设计原则

1. **全模态主导**：判断环节必须能看画面、听声音，不退化成读文字摘要
2. **关注变化点**：高密度连续帧交给模型自己找变化点，而非人工均匀抽帧替它决定看哪
3. **事实与判断分离**：阶段一锁定事实防串供，阶段二在事实基线上做感官判断
4. **按证据形态切换主骨架**：有口播用口播时间线，无口播则切到字幕/OCR、画面变化、镜头轨和音频节奏
5. **Fail-loud**：依赖缺失、API 失败立即报错，不静默降级
5. **证据可追溯**：每个结论都绑定时间点和画面/口播证据
6. **GMV 导向**：所有建议围绕停留、信任、下单转化
7. **本地化**：话术用达人原语言，适配东南亚市场
