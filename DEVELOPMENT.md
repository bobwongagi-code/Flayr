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

## 状态权威与交付有效性

本节说明现有状态由谁写、谁核验，不新增状态框架、审批队列或全局“通过”。旧分析报告与 Zero/A/B/C 人工辅助产物分开执行；后者不要求先跑旧分析链来获得 `_SUCCESS.json`。

| 回答的问题 | 权威记录及写入者 | 消费与边界 |
| --- | --- | --- |
| 运行走到哪一步 | `run_state.json`；`run_state.py` 约束转换，由 CLI 编排推进 | 普通转换不能离开终态；恢复必须记录 recovered，且只能修正为终态。`recover_run_state` 不替调用方核验产物；新一轮通过显式目录复用/reset 开始，不冒充旧轮续接 |
| 分析产出了什么 | `pipeline.py` 的共享 finalizer 收口，`apply_finalized_analysis_result` 写 `analysis_run_state` | Stage2 candidate、分组、综合和未闭合阶段共同决定最终分析状态；不得用候选完成覆盖最终降级。Stage1 资格总体 completed 可以保留失败阶段，D 只处理目标范围 |
| 自动报告是否已发布 | CLI `_generate_reports_and_publish` 写报告、成功/降级清单并推进终态；`run_manifest.py` 核必需产物哈希及调用方指定的输入/配置 | Web `_publication_status` 共用于收口和重启恢复，batch 使用自己的严格完成门禁；二者用途不同。退出码 0、单个文件存在或索引 completed 均不能单独证明发布成功 |
| 哪些模型判断已人工处理 | `human_review.json`；`human_review.py` 按六阶段决定派生 status，并绑定来源 | approved 表示该轮六阶段均已有处理决定，可包含证据不足；不等于模型预测正确，也不批准所有建议。建议动作另审；复核后的展示是投影，不改原分析 |
| 当前作者成品检查到了什么 | 实际成品及相应检查结果；HTML 由 `validate_review_delivery.py` 记录成品/检查器哈希与作者区域 | 机械检查、执行者复核、人工确认、发送事实分别记录。机械通过不授予人工批准或转发权限；ABC 没有自动批准与发送系统 |

**失效必须限定对象。** 阶段修改经 `update_stage` 重开已批准阶段、清建议批准并删除两份 reviewed 报告；原模型草稿的合法发布状态不因此变失败。原分析重新合法发布后，旧 sidecar 仍因来源不一致而失效。外部修改文件时，拒绝读取过期审核不等于自动删除已复制或已发送的 HTML。明确要求包的来源变化只使该包过期，不撤销无关的阶段审核；人工备注也不等于要求已满足。

**失败与降级分别保留。** FAILED/UNPUBLISHED 优先于残留报告；有效 DEGRADED 草稿可以按现有来源核验进入人工接管。人工确认后仍保留模型降级与 `model_completed=false`，不生成完整成功标记。分析-only 完成于 UNPUBLISHED，不能借残留报告变成已交付。单视频 breakdown 可完成本模式报告，但不代表完成双视频比较，也不生成比较成功标记。

**恢复不是可重复消费的资源。** Stage1 已记录 pipeline 来源的 focused recovery 终态，即使仍有 unknown，也不重新请求。保留未闭合阶段和原失败，不能把“恢复已执行”写成“证据已解决”。缓存与传输协议不在本批重构。

跨模块回归在 [交付状态转换测试](tests/test_delivery_state_transitions.py) 与 [降级人工接管测试](tests/test_degraded_human_review.py)：

| 不变量 | 本批验证 |
| --- | --- |
| 普通转换只走允许边，终态不能静默重开 | 八状态 64 个 source/target 组合；拒绝和同状态调用不改原文件；恢复纠正单列事件 |
| 报告/清单存在不代表已发布 | 发布提交前、FAILED 残留与分析-only UNPUBLISHED，联查 manifest、Web 发布和人工审核入口；正常完成作保护例 |
| 修改使相关批准失效，不全量作废 | 审核版报告撤销与再生成、原模型草稿仍有效、新模型版本不能继承旧人工确认 |
| 来源变更不能继续写旧核对包 | 真实来源门禁下核对包变更后拒绝追加；无关阶段审核保持原字节 |
| 合法降级不是失败，也不能升级成模型完成 | 完整降级草稿经过审核版双报告生成，Web 仍为 degraded，原分析不变且无成功标记 |
| 已用恢复机会不因 unknown 再执行 | 连续两次进入恢复函数均不准备请求、不调用 provider；局部 D 保留非目标失败 |

运行相关离线回归：

```bash
.venv/bin/python -B -m unittest tests.test_delivery_state_transitions tests.test_degraded_human_review tests.test_run_state tests.test_review_regressions tests.test_operations_contracts tests.test_human_review tests.test_requirement_review tests.test_review_workflow_checks -v
```

交接沿用 [既有任务记录格式](references/review-improvement.md)，不把这张权威表变成另一处业务进度台账。测试只证明这些转换与边界，不证明内容判断准确率、独立人审、实际发送或业务采用。

## 重试与缓存协议

重试、续跑、缓存命中和证据恢复是不同事件。以下说明和离线回归不合并线路、不增加调用额度、不改默认值。

| 层级 | 谁决定继续 | 停止与保留 |
| --- | --- | --- |
| 兼容接口传输与输出扩容 | [api.py](scripts/flayr_core/llm/api.py)；默认最多2次额外传输重试、2次输出扩容，调用方可收紧 | 两个计数独立，但每次发送都受同一资源预算约束；永久HTTP错误停止。扩容至少抬到16384，再翻倍到字段上限，不换模型或端点。扩容耗尽后若返回非空正文，仍保留 `finish_reason=length`，不等于JSON或业务合同合格 |
| 公司原生Gemini | [gemini_transport.py](scripts/flayr_core/llm/gemini_transport.py)；按调用方传输重试额度，最多3次发送 | 首次瞬时传输错误可重试；仅前两次均HTTP504才可第三次。HTTP200后的模型身份、STOP、正文或JSON不合格不在原生传输内重试/扩容，也不回退兼容接口 |
| 外层完整响应 | [pipeline.py](scripts/flayr_core/llm/pipeline.py) 的 `fetch_json_completion`，默认1次 | 只有调用方显式提高 `max_attempts` 才可重新进入；永久HTTP错误、共享预算不足仍停止。解析失败不等于自动修复通过，下游必须处理返回正文；各调用点的额外修复/资格流程不能当作传输重试 |
| 事实恢复 | 同文件的 focused recovery | pipeline已记录的恢复终态不会因仍有unknown而重新消费；可复用的局部覆盖必须保留目标、未解决阶段和来源 |

**记账看事件，不数请求ID。** 发送前先经过 `ResourceBudget.reserve_api_call`；传输失败和扩容发送均占额度。一个逻辑ID可能包含多次发送，外层重入也可能沿用ID并重置局部attempt；`request_retry_kinds` 还可能先记录计划重试，再被预算阻断。因此累计额度以已预留的发送尝试事件为准，不能拿唯一ID数、最后一次 `transport_attempts` 或重试理由数量代替。预留事件不证明服务端收到、成功或实际收费；费用仍需服务端账单核对。

| 复用对象 | 身份与完整性依据 | 不代表什么 |
| --- | --- | --- |
| 预处理 | [flayr.py](scripts/flayr.py) 的 fingerprint：源视频内容、实际配置、预处理实现/工具版本和产物manifest | 不以每次Git提交作为失效条件；路径搬迁不等于内容变化，也不允许缺产物照用 |
| 商品地基 | [cache.py](scripts/flayr_core/llm/cache.py)：商品上下文、判断模型/端点、实际请求摘要、缓存和输出schema | 不是任意商品资料都能套用旧答案 |
| 视频事实 | 同文件：视频、预处理内容、角色、商品/地基、判断与视觉模型/端点、源码提交及参考文件摘要等；读时再核结果与嵌入provider哈希、冻结事实及资格/恢复合同 | 比预处理更保守；`references/` 直接子文件变化或源码提交变化也可能失效，不能笼统声称“文档改动不影响缓存” |
| 已完成运行 | [cli.py](scripts/flayr_core/cli.py) 的有效配置/结果复用版本、输入与 [run_manifest.py](scripts/flayr_core/run_manifest.py) 的必需产物核验 | 与事实缓存不是同一门禁，不能仅凭退出码或目录存在复用 |

事实缓存的 `completion_status=completed` 只表示缓存写入完成。合法的局部恢复仍可含未解决事实，命中后保留unknown、恢复记录和原资格状态；不能把命中改写为全局证据闭合。身份、字节或语义合同不合格则miss，读取不覆盖原记录；miss本身不授予新调用权限。

**严格回放与续跑不能互换。** [provider_artifacts.py](scripts/flayr_core/llm/provider_artifacts.py) 同时核任务类型、请求摘要、模型、端点及响应哈希。严格replay缺失、失败或不匹配就失败落盘，不调用在线callback；resume只有合格记录才复用，否则可进入调用方提供且已获授权的在线分支。严格回放和dry-run绕过事实缓存，不能用缓存悄悄代替待验证的provider路径。本文的严格回放矩阵针对共享provider包装层；ASR/OCR各自编排还需保留其已有协议测试。

跨层新增回归见 [重试与缓存测试](tests/test_retry_cache_protocols.py)，使用合成响应与临时缓存，拦截实际传输，不读密钥或处理业务视频：

```bash
.venv/bin/python -B -m unittest tests.test_retry_cache_protocols tests.test_llm_api tests.test_gemini_transport tests.test_gemini_gateway tests.test_resource_budget tests.test_stage_fact_artifacts -v
```

上述检查验证现行停止、记账、身份与复用边界，不证明网关实际稳定、模型内容正确或缓存能提高准确率。

## 批处理异常回收

[batch_analyze.py](scripts/batch_analyze.py) 在 `Popen` 返回后立即登记子进程与日志句柄，再写运行状态；资源所有权不能依赖磁盘写入成功。发生异常或用户中断时，逐个尝试停止尚在运行的进程组并关闭日志，即使中断记录写入失败、另一个进程停止失败或日志关闭失败，也继续处理其余资源。清理错误尽可能写入标准错误，保留最初异常；不自动重启任务。

状态文件标成 `interrupted` 不能证明进程已停止；如果出现 `cannot stop` 警告，仍需操作人员核查。磁盘持续不可写时也不能承诺状态已落盘。既有进程组终止与宽限时间不变，正常完成不增加等待、磁盘写入或调用；额外处理只发生在异常路径，清理耗时取决于存活子进程数及既有宽限窗口。

[异常回收测试](tests/test_batch_cleanup.py) 覆盖初始写入、启动后写入、后续启动与完成写入失败、多任务中断、持续磁盘故障及停止/关闭失败；正常完成作保护例，另用无联网的本地等待子进程验证实际终止回收。Web单任务已有独立finally清理，本批不改Web队列或运行发布门禁。

```bash
.venv/bin/python -B -m unittest tests.test_batch_cleanup tests.test_batch_analyze tests.test_review_regressions tests.test_run_state -v
```

## Web 停机边界

[web_app.py](scripts/web_app.py) 的 `JobStore.shutdown` 先关闭接单入口；任务提交、子进程启动与登记共用停机锁边界。已开始但尚未接纳的上传若遇到停止，清掉本次暂存目录并拒绝提交；已排队任务取消后记为failed，理由明确为“服务停止，未执行”，不伪装成模型判断失败。启动准备中的配置异常也进入任务失败记录，不留下永久queued。

每个已登记进程均独立尝试停止。一个停止失败或返回后仍存活，不跳过其他进程及线程池取消；保留句柄，并明确报“未完全停止”。此时线程池使用 `wait=False`，避免明知子进程仍存活却等待其监控线程；这不证明OS进程已经退出，也不保证Python解释器能立即退出，仍需核查并可再次调用shutdown。正常停止保留 `wait=True` 与原5秒宽限，不改业务调用、重试或预算。

排队取消只改受影响任务，完成/降级产物不因服务关闭失效。取消记录写盘失败会报错并保留dirty标记，下次shutdown补写；成功后重复停止不重复写索引。锁不覆盖停止进程或等待线程池，仅保护接单/提交及Popen登记的短临界区，避免等待线程时阻塞其收尾。

[停机回归](tests/test_web_shutdown.py) 使用合成输入、事件协调的真实线程池和模拟进程；测试隔离环境变量，失败断言不展开Popen环境。覆盖停止失败、仍存活、取消队列、停止中上传、启动前拦截、配置失败、线程池异常和取消记录补写，配合原Web与批处理资源回收回归。没有将这些保护包装为新服务、自动审批或发送系统。

```bash
.venv/bin/python -B -m unittest tests.test_web_shutdown tests.test_web_app tests.test_review_regressions tests.test_batch_cleanup tests.test_batch_analyze -v
```

这些工程保护不授予ABC材料确认、实际发送或启动模型任务的权限；业务交付仍沿用原人工流程。本地验证和GitHub CI分别记录。

## Web 接单失败与认领

`create` 的任务记录、索引和线程池不是一个跨系统事务。任一步保存/提交抛错时，先在同一锁内把本次任务置为failed、写明“未启动”，再分别尽力保存任务记录和索引；保留已接管输入供核查，不删除其他任务。持续磁盘故障时保留原异常及补写失败说明，不能承诺已经落盘；重启仍按已有恢复门禁收口，不自动重新提交。

线程池可能先入队再抛错，因此工作线程必须在同一锁内从当前queued认领为running，之后才准备命令。已失败、已完成、正在运行或状态缺失的任务不能再次启动；提交失败的迟到回调也不能启动子进程。这不是对外部模型请求的收费幂等或分布式“恰好一次”保证。

成功接单不代表分析或报告完成。提交已被执行器接受后，返回响应失败不撤销其所有权，也不自动重复提交。本次仅给失败路径补写状态，成功路径不增加索引/元数据写入、请求或重试，锁内不等待future完成。

[接单故障回归](tests/test_web_submission.py) 覆盖保存后抛错、持续写盘故障、缺索引恢复、真实线程池“先入队再报错”、非queued拒绝启动及正常单次认领；合成输入与环境隔离，不运行视频或模型。

```bash
.venv/bin/python -B -m unittest tests.test_web_submission tests.test_web_shutdown tests.test_web_app tests.test_review_regressions -v
```

## 上传暂存与预留释放

multipart解析、输入接管和HTTP响应各有独立的资源所有权。解析中断也清理本次已创建的分片，
一个文件删不掉仍尝试其余文件；清理失败作为附注保留，不替换原解析异常或中断。
输入接管先独占新建任务目录，失败只回滚本次目录；ID冲突不能复用或删除旧任务目录。

最终清理与容量/接单预留释放分别执行。分片或上传体清理失败会警告，但不跳过预留释放，
也不把已接受的任务改说成未接收。残留文件仍计入已有磁盘用量检查，按既有暂存TTL回收；
这不是文件必已删除或进程强杀后的恢复保证。正常路径不增加模型请求、重试或全盘扫描。

[上传回收回归](tests/test_upload_cleanup.py)含中断、文件锁注入、原异常保留、独占目录保护及
真实loopback HTTP接单测试；执行器被替换，使用合成字节，不启动分析或模型。

```bash
.venv/bin/python -B -m unittest tests.test_upload_cleanup tests.test_web_submission tests.test_web_app -v
```

## 前端异步状态

旧Web入口的HTTP成功不等于拿到合法任务。`app.js` 拒绝坏JSON、非对象、缺ID、未知状态、错误列表结构及详情ID错配；无效响应不创建假任务、不清空已有列表。提交响应不明时提示查看任务列表，不自动重试或宣称服务器未接收。

提交中使用独立pending状态：改选/移除视频不能解锁第二次提交，成功才清选择，失败保留输入，所有结束路径统一恢复按钮。这只防同页的重叠提交，不代替服务端幂等或保证手动重试不重复计费。

详情请求按最后已应用的回应版本防倒退，不能仅因另一个请求仍在进行就丢掉有效回应，避免慢响应下持续不更新。已知终态不被迟到的queued/running详情重开，终态之间的合法纠正仍可更新。列表请求按请求版本及逐任务快照合并：保留请求期间的本地更新，同时接收列表里的其他任务，不能因一条轮询丢弃整份列表。轮询归属绑定任务及代次，旧任务完成不能停止新任务的定时器。仍保持30秒间隔，不增加API请求；忽略旧回应不代表取消了已发送的请求。

真实任务无可用报告时显示文件不可用，不回退原型示例、虚构分阶段结论或新报告。报告用途仍由用户选择，已有iframe切换和来源门禁不变。

[前端契约测试](tests/test_frontend_contracts.py) 经标准Node VM执行真实app与导入模块，[运行夹具](tests/frontend_runtime.mjs) 只在闭包末尾暴露测试入口，使用最小DOM、合成响应、可控Promise与计时器，不改写被测函数。十七个运行场景覆盖响应、乱序、慢轮询、跨入口终态与删除、报告加载超时/取消/回退及跨视图离开、缺报告和重复提交。Node不可用时明确skip，不安装依赖，也不能宣称这些运行检查已通过；DOM夹具不替代实际浏览器核验。

```bash
.venv/bin/python -B -m unittest tests.test_frontend_contracts -v
```

## 删除与跨请求收口

删除请求必须完成“撤销执行资格、确认进程停止、移除文件”才能返回成功。
索引提交失败先恢复内存记录并尽力补写；停止失败、进程仍活或文件清理失败时，
保留可重试记录和未结束进程句柄，不用204掩盖失败。部分文件可能已经清理。
删除未完成标记同时写入索引和目录元数据，索引重建不以旧成功产物覆盖它。
恢复记录不代表恢复了所有文件；持续磁盘故障仍需人工核查。删除与停机都不授权新调用。
工作线程在目录准备和Popen前核验当前所有权；被删除后不再通过失败记录重建目录。
监控与最终发布回写也必须在锁内确认仍为running，不能用迟到的完成回调覆盖删除失败。
进程停止、文件删除不在接单锁内等待，正常路径没有额外模型请求或自动重试。
相关保护见 [删除回归](tests/test_web_deletion.py)。

列表接收成功后使此前发起的详情失效，包括列表已移除的任务；之后的新详情仍可更新。
两种入口均不把已知终态退回queued/running，但允许新的终态纠正。
报告加载包含loading、active、failed和cancelled四种局部状态：30秒内未打开则恢复旧报告
或空态并解除忙碌；切换任务取消未完成HEAD并撤销旧回调。取消不等于重试，
不改变后台任务、报告内容或人工批准。实际iframe事件、HEAD失败、超时和迟到回调
由真实JS运行夹具覆盖；最小DOM不是浏览器布局或跨设备验证。

## 真实浏览器状态验收

Node夹具不能证明浏览器实际的iframe事件与取消行为。需要核对这层时，运行：

```bash
.venv/bin/python tests/frontend_browser_fixture.py
```

打开终端打印的本机URL；它提供真实前端及六个合成任务，不启动JobStore、视频处理或模型。
只绑定127.0.0.1，静态文件走白名单，POST/PUT/DELETE拒绝。结束后Ctrl-C停止；不用于部署。

| 合成任务与操作 | 应观察到的结果 |
| --- | --- |
| Ready：依次选自己用、给达人 | 内容与用途一致，动画结束后只有一个iframe，按钮恢复可用 |
| Rejected：先打开自己用，再切给达人 | 拒绝后保留原报告，按钮恢复，不留下空白帧 |
| Slow HEAD：先打开自己用，再切给达人，等待至少35秒 | HEAD超时后恢复原报告，不因迟到响应切换 |
| Slow iframe：首次选给达人，等待至少35秒 | HEAD成功但正文未到仍受总期限约束，回到空态且按钮可用 |
| Slow iframe：开始加载后立刻回列表，再打开Running | 未完成帧移除，35秒后仍是运行页，无旧报告或超时反馈污染 |
| Failed：从列表进入 | 失败页没有旧报告；重新进入Ready仍能正常选择报告 |

`/fixture/events`只记录本轮夹具收到的方法与路径；是否重试须结合手动操作记录判断。
浏览器验收记录存当前任务诊断目录，不把人工操作结果冒充CI自动浏览器测试。
[夹具单测](tests/test_frontend_browser_fixture.py)检查只读白名单和故障路由，不能替代上表操作。

## 状态边界覆盖图

不把所有状态收进一个全局FSM。以下边界分别由现有所有者维护：

| 边界 | 当前落点 | 不自动推导 |
| --- | --- | --- |
| 运行与发布 | run_state、manifest及交付转换回归 | 退出码或候选完成不等于交付完成 |
| 预处理代发布 | 清单写完才提交，失败撤回并保留旧代；未恢复备份阻止目录重用；发布事务回归 | 不保证断电或多进程并发发布恢复 |
| 资格、判断、恢复 | 原合同及重试/缓存跨层回归 | 不新增恢复额度，不借工程修复改业务规则 |
| 人工结论与动作 | 原sidecar来源绑定，审核报告中断清理回归 | 确认阶段不等于批准全部建议或已发送 |
| Web执行与删除 | 单次认领、停机、删除故障回归 | 取消或failed不保证OS进程已退出 |
| 浏览器视图 | 跨请求版本、终态保护、报告加载取消/失败回退 | 页面可读不代表获准外发 |
| Zero/A/B/C业务交接 | 原任务文件/版本、确认人与发送记录 | 不新建自动审批、自动失效传播或发送系统 |

机械检查与人工业务判断的职责不互换；没有复现的语义或架构假设不作为本轮修复。

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

历史记录：2026-11-07 的 CI 曾被当时锁定的 Pillow / Black 安全审计阻断；该记录不代表后续版本的当前状态。现行锁定版本及升级记录见 [DEPENDENCIES.md](DEPENDENCIES.md)，本批是否通过以实际验证为准，不能继承旧失败或旧 PASS，也不跳过审计。

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
