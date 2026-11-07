# 开发与维护

本文件面向维护 Flayr 代码的人。只想了解 Flayr 能做什么，请看 [README](README.md)。

## 旧分析链

原分析链保留以下模式，不作为内容策划与反馈的默认启动命令：

| 模式 | 原有用途 |
| --- | --- |
| `breakdown` | 单条参考视频拆解 |
| `compare` | 两条视频的分阶段比较 |
| `improve` | 比较、建议和报告输出 |
| `scope` | 可比较性预检 |

```bash
# 查看帮助不会发起分析。
python3 scripts/flayr.py --help
python3 scripts/web_app.py --help
```

实际运行可能涉及收费模型、ASR、OCR 和视频上传，需要本次任务授权及可用的服务配置。模型与端点以实际 CLI 配置和 [解析代码](scripts/flayr_core/cli.py) 为准；显式配置优先，失败不自动换线路。ASR 保持独立配置，密钥不写进仓库、命令示例或产物。本地 Web 对应分析任务，不是内容策划工作台。

旧链的 `evidence_units`、时间范围、截图、转写和来源记录可供后续工作复用。`_SUCCESS.json` 表示工程流程通过，`degraded_manifest.json` 表示降级；两者都不能替代内容终审或外发批准。请求或来源身份不匹配时，严格回放失败，不静默转成在线调用。

```bash
# 只对已有产物执行确定性最终处理，不调用外部服务。
python3 scripts/replay_finalization.py /path/to/source-run /path/to/new-output
```

## 离线检查

以下检查不调用模型、不重新处理视频。路径须替换为已有材料。

```bash
# 检查学习辅审材料与当前作者稿。
python3 scripts/validate_learning_review.py /path/to/cases.json --author-copy /path/to/author-draft.md

# 只检查作者稿的有限用语问题。
python3 scripts/validate_learning_review.py --author-copy /path/to/author-draft.md

# 查看明确要求核对的 init / show / note 用法。
python3 scripts/review_requirements.py --help
```

检查通过只说明相应字段、来源绑定或有限用语条件通过。截图是否支持句子、是否抓住主要问题、当地语言是否自然，仍需要内容审核。

## 环境与验证

支持 Python 3.11、3.12、3.13。媒体处理需要 `ffmpeg`、`ffprobe`；在线 ASR 使用 `curl`。纯文档阅读和离线材料检查不要求配置模型密钥。依赖见 [requirements-dev.lock](requirements-dev.lock) 与 [DEPENDENCIES.md](DEPENDENCIES.md)。

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --requirement requirements-dev.lock

python3 -m unittest discover -s tests -v
python3 scripts/verify_semantic_baseline_freeze.py
bash scripts/run-quality-gates.sh
```

2026-11-07 的 CI 存在已记录的依赖安全审计阻断，涉及锁定的 Pillow / Black 版本；不能将当前仓库描述为全套质量门禁通过。依赖升级须另行处理，不通过跳过审计放行。

版本与发布约定见 [RELEASE.md](RELEASE.md)；模块说明见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 目录

```text
Flayr/
├── AGENTS.md、SKILL.md         # Agent 的工作约定与任务入口
├── references/                # 业务方案、策划方法、审核规则与生产契约
│   └── migrations/            # 可公开的内容迁移方法与检查工具
├── scripts/                   # 取证、离线核对、旧分析链与维护命令
│   └── flayr_core/            # 商品、事实、阶段、来源、报告和运行模块
├── tests/                     # 离线测试与回归
├── assets/、frontend/          # 旧报告模板和本地 Web 资源
├── structure_library_full.md  # 带货表达的结构与拍法资料
└── runs/、outputs/            # 本地任务与审核产物，不随仓库发布
```

原始视频、内部任务与本机凭据不因进入仓库就获准公开；公开仓库只保留已确认可发布的内容。

内容方法的吸收与归档边界见 [Senty 内容迁移说明](references/migrations/senty-20261107/README.md)。公开方法与本地原始材料分开维护，开发验证不代表业务内容已经通过人工审核。
