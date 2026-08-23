# Flayr · TikTok 带货短视频分析与提升工具

Flayr 接收一条参考视频和一条达人视频，结合画面、口播、字幕和音频质检，按 S1-S6 功能阶段对比差距，输出可执行的拍摄、剪辑和表达建议。Flayr 生成结构化分析和 HTML 报告，不生成替换视频、达人音色视频或 AI 示意视频。

## 能力概览

| 能力 | 说明 |
|------|------|
| 视频转写 | 通过可配置的在线 ASR 服务生成句级和词级时间戳，支持传入语言提示 |
| 视频理解 | 以 canonical frames 和时间线为可审计主证据，必要时定向查看原生视频时间窗 |
| 音频质检 | 检查音量、静音和峰值风险；语气、BGM 和音效只作观察，不直接决定差距等级 |
| 结构化分析 | 按 S1-S6 功能阶段比较达人与参考视频 |
| 改进建议 | 按商业影响排序，给出话术、画面和执行建议 |
| 报告输出 | 生成业务分析报告、达人执行报告和结构化 JSON 产物 |

## 使用边界

Flayr 是辅助分析工具。relation、gap 和改进建议在发布前必须由人工确认；证据不足时应保留 `unknown` 或 `degraded` 状态，不自动发布确定结论。

## 分析架构

Flayr 使用视觉模型、判断模型和 ASR 服务分工协作。所有阶段共用统一事实账本、最终判定流程和受限复核机制，不为不同模型维护平行的事实或判断系统。

```text
Stage1：单视频事实抽取
  分别处理参考视频和达人视频
  canonical 关键帧/时间线 + 窗口化 ASR + OCR
  -> 生成带时间戳的 evidence_units
  -> Stage1-B 只基于事实做资格投影
  -> 资格或连续性未闭合时，Stage1-C 最多补看一次目标时间窗
  -> 确认后的 facts 成为后续判断的唯一事实源

Stage2：跨视频比较
  只读取两侧 facts，不重新查看完整视频
  -> 按 S1-S6 分组判断 relation、gap 和 reason
  -> Stage3 汇总 key_conclusions 与 improvements

Phase C：定向复核
  仅由覆盖、资格、连续性或最终判定冲突触发，最多复核两个阶段
  -> 查看对应时间窗并生成受限事实补丁
  -> 重新执行既有后处理与校验
  -> 不整段重写判断，也不无限索取素材
```

详细的模块边界和数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录结构

```text
Flayr/
├── scripts/
│   ├── flayr.py                  # CLI 主入口
│   ├── web_app.py                # 本地 Web 入口
│   ├── batch_analyze.py          # 批量作业、断点续跑与限并发
│   ├── replay_finalization.py    # 确定性最终处理回放
│   ├── run-quality-gates.sh      # 代码质量检查
│   └── flayr_core/               # 分析、证据、报告和运行状态模块
├── frontend/                     # Web 前端资源
├── assets/                       # HTML 报告模板
├── references/                   # 输出契约、观察指引和市场知识
├── QA-RULES.md                   # 分析结果校验规则
├── structure_library_full.md     # S1-S6 结构库
└── runs/                         # 默认运行输出目录
```

## 安装与依赖

运行环境：

- Python 3.11、3.12 或 3.13
- `ffmpeg` 和 `ffprobe`，用于媒体探测、抽帧和音频提取
- `curl`，用于调用在线 ASR 服务
- 可访问的视觉模型、判断模型和 ASR 服务

核心分析代码只使用 Python 标准库。开发、报告增强和质量检查依赖已固定在 `requirements-dev.lock`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --requirement requirements-dev.lock
```

依赖边界见 [DEPENDENCIES.md](DEPENDENCIES.md)，版本和发布流程见 [VERSION](VERSION) 与 [RELEASE.md](RELEASE.md)。

## 配置

密钥通过环境变量或 macOS Keychain 读取，不要写入命令、仓库或运行产物。下面的值均为占位符，需要替换为已批准服务的实际配置：

```bash
export FLAYR_JUDGMENT_MODEL="<judgment-model-id>"
export FLAYR_VISION_MODEL="<vision-model-id>"
export FLAYR_LLM_API_URL="<approved-chat-completions-url>"
export FLAYR_LLM_API_KEY="<secret>"
export FLAYR_LLM_API_KEY_ENV="FLAYR_LLM_API_KEY"

export FLAYR_ASR_API_URL="<approved-asr-url>"
export FLAYR_ASR_MODEL="<asr-model-id>"
export FLAYR_ASR_API_KEY="<secret>"
export FLAYR_ASR_API_KEY_ENV="FLAYR_ASR_API_KEY"
```

视觉模型和判断模型必须同时配置。系统不会在调用失败后静默切换模型。ASR 失败时，`compare` 和 `improve` 默认返回非零；只有显式使用 `--allow-degraded` 才会继续生成带降级状态的报告。

## CLI 用法

### 基本分析

```bash
python3 scripts/flayr.py improve \
  --benchmark-video /path/to/benchmark.mp4 \
  --creator-video /path/to/creator.mp4 \
  --product-name "产品名称" \
  --target-market auto \
  --core-selling-points "已确认的核心卖点" \
  --judgment-model "$FLAYR_JUDGMENT_MODEL" \
  --vision-model "$FLAYR_VISION_MODEL" \
  --llm-api-url "$FLAYR_LLM_API_URL" \
  --llm-api-key-env FLAYR_LLM_API_KEY \
  --asr-api-url "$FLAYR_ASR_API_URL" \
  --asr-model "$FLAYR_ASR_MODEL" \
  --asr-api-key-env FLAYR_ASR_API_KEY \
  --verification-stage production \
  --output-dir runs/example
```

CLI 提供四种模式：

| 模式 | 用途 |
|------|------|
| `breakdown` | 分析单条参考视频的结构 |
| `compare` | 对比参考视频与达人视频 |
| `improve` | 对比并生成改进建议和完整报告 |
| `scope` | 只执行可比较性预检，不生成报告 |

常用参数：

| 参数 | 说明 |
|------|------|
| `--benchmark-video` | 参考视频路径 |
| `--creator-video` | 达人视频路径，`compare`、`improve` 和 `scope` 必需 |
| `--product-name` | 产品名称 |
| `--product-category` | 结构库中的产品类别 |
| `--target-market` | `auto`、`sea` 或两位市场代码 |
| `--core-selling-points` | 已核实的产品卖点和差异点 |
| `--judgment-model` | 资格、比较、汇总和文本判断所用模型 ID |
| `--vision-model` | OCR、事实观察和定向视频复核所用模型 ID；必须与判断模型同时提供 |
| `--llm-api-url` | 已批准的 Chat Completions 兼容端点 |
| `--llm-api-key-env` | 保存分析服务密钥的环境变量名 |
| `--asr-api-url` | 在线 ASR 服务端点 |
| `--asr-model` | 在线 ASR 模型 ID |
| `--asr-language` | ASR 语言提示，默认 `auto` |
| `--asr-api-key-env` | 保存 ASR 服务密钥的环境变量名 |
| `--ocr-mode auto/on/off` | 字幕 OCR 模式，默认 `auto` |
| `--max-total-wall-time` | 单次运行总墙钟预算，默认 1800 秒 |
| `--max-llm-calls` | 单次运行允许的真实模型网络尝试上限，默认 32 |
| `--reuse-preprocessing` | 复用同一输出目录中身份匹配的抽帧、转写和字幕轨 |
| `--allow-degraded` | 明确允许生成降级结果；降级运行不会写成功清单 |

完整参数以命令帮助为准：

```bash
python3 scripts/flayr.py --help
```

## Web 用法

Web worker 读取上一节的 `FLAYR_*` 配置。判断模型和视觉模型只设置一个时，任务会在启动分析前失败。

```bash
python3 scripts/web_app.py --host 127.0.0.1 --port 8787
```

启动后访问 `http://127.0.0.1:8787/`。默认只监听本机。对外监听必须显式使用 `--unsafe-expose`，并配置不少于 32 字节的 `FLAYR_WEB_AUTH_TOKEN` 和允许访问的 `FLAYR_WEB_ALLOWED_HOSTS`。使用前先查看：

```bash
python3 scripts/web_app.py --help
```

## 工作流程

```text
视频输入
  -> 在线转写、抽帧和音频提取
  -> 可选的转写翻译
  -> Stage1 单视频事实抽取和资格投影
  -> Stage2 分阶段比较
  -> 必要时 Phase C 定向复核
  -> 后处理、契约校验和报告渲染
  -> 输出到 --output-dir 或 runs/<时间戳>/
```

## 输入与输出

所有模式都需要参考视频；`compare`、`improve`、`scope` 还需要达人视频。建议明确提供产品名称、产品类别、目标市场和已核实卖点；目标用户与购买动机可进一步收窄判断上下文。未提供的信息不会由 README 示例替用户推断。

成功的 `compare` 或 `improve` 运行会写入 `_SUCCESS.json`。降级运行会写入 `degraded_manifest.json`，不会伪装成成功。

主要产物：

| 文件 | 说明 |
|------|------|
| `bd_report.html` | 业务分析报告 |
| `creator_report.html` | 达人执行报告 |
| `report.html` | 通用报告 |
| `analysis.json` | 完整运行数据和状态 |
| `analysis_result.json` | 归一化后的模型分析结果 |
| `raw_model_response.json` | 原始模型响应 |
| `validated_normalized_result.json` | 通过契约校验的规范化结果 |
| `final_derived_result.json` | 最终派生结果和字段来源 |
| `analysis_replay_context.json` | 最终处理回放所需的上下文与输入哈希 |
| `postprocess_change_log.json` | 后处理字段变更、规则和证据记录 |
| `video_facts_benchmark.json` / `video_facts_creator.json` | 两侧 Stage1 事实清单 |
| `stage1_provider_*.json` / `stage2_provider_*.json` | Provider 响应、请求身份、哈希和执行来源 |
| `provider_asr.json` | ASR 原始响应、请求身份、哈希和执行来源 |
| `benchmark/` / `creator/` | 各侧转写、帧、联系表、时间线和证据审计产物 |

## 回放

确定性最终处理只读取已存在的规范化结果、分析上下文和输入哈希，不读取视频，也不调用外部服务：

```bash
python3 scripts/replay_finalization.py <source-run> <new-output-dir>
```

Stage1、Stage2 或完整 Provider 结果可分别通过 `--stage1-replay-from`、`--stage2-replay-from` 和 `--provider-replay-from` 严格回放。缺失产物或请求身份不匹配会直接失败，不会静默回退到在线调用。

## 验证

运行单元测试和质量检查：

```bash
python3 -m unittest discover -s tests -v
bash scripts/run-quality-gates.sh
python3 scripts/check_release.py
```

## 设计原则

1. **模态分工明确**：视觉模型负责可见事实和定向视频复核，ASR 服务负责口播转写，判断模型只读取确认后的事实。
2. **事实与判断分离**：Stage1 生成事实，Stage2 在事实账本上比较，避免两侧证据串用。
3. **证据可追溯**：事实、判断和建议绑定时间范围、证据 ID 与执行来源。
4. **失败显式化**：外部服务、模型输出或契约失败返回非零；降级结果不会写成功清单。
5. **有限复核**：原生视频只用于触发条件明确的定向复核，不创建第二套事实系统。
6. **商业导向**：建议围绕停留、信任和下单转化组织。
7. **本地化**：输出适配目标市场，话术建议保留达人使用的语言语境。
