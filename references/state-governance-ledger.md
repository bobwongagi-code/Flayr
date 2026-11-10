# 状态机制逐项对账

基线为 `72fa453c64f1dae9e26c2e91114a86254da88af8`，核对日期为 2026-11-10。沿用最初盘点的原编号，50 类代码机制加 4 类人工流程；这些编号是机制分类，不是 54 个缺陷。

原始依据已从本聊天读回：英文盘点为 2026-11-10 00:08:50，中文复述为 06:18:01，后续行动建议为 06:22:34（均为 Asia/Singapore）。原文和 SHA-256 留在本机任务证据中，不把聊天、本机路径或业务材料公开。以下链接指现有文件，原聊天的行号不再作为当前定位。

## 如何读状态

| 标记 | 含义 | 能否称为已修复 |
| --- | --- | --- |
| R | 该机制内有已复现问题，已修改生产代码并发布；不代表整个模块重写 | 仅指本行限定的问题 |
| T | 原机制保留，本轮补跨层回归/说明并发布 | 不能称生产逻辑已修复 |
| K | 保留原实现，提供相关源码/既有测试索引，未做逐项专项复验 | 不能并入已修复或全面验收 |
| M | 保留人工业务流程，规则/材料存在，不代表试点完成 | 不属于自动工程闭环 |
| O | 尚未闭合的已知问题或外部边界 | 不能称已完成 |

本表共 54 项：R 13 项、T 14 项、K 21 项、M 4 项、O 2 项。R/T 是机制数量，不是缺陷或测试数量。历史完整套件通过不证明每项所有路径都已覆盖，本次文档核对不重跑模型或全量测试。

## 原编号对账

| 原编号与机制 | 处理与理由 | 提交 | 代码/回归依据 |
| --- | --- | --- | --- |
| 01 运行目录复用 | **R** 只补复用入口的恢复备份保护，保留未知文件拒绝规则。| `86457a0` | [源码](../scripts/flayr.py)；[test_incomplete_generation_backup_survives_directory_preparation](../tests/test_preprocess_publication_transactions.py)，`test_incomplete_generation_backup_survives_directory_preparation` |
| 02 报告发布 | **T** 区分分析完成、发布提交和终态；未重写发布器。| `eb38cfc` | [源码](../scripts/flayr.py)；[test_manifest_before_terminal_commit_is_not_publication_or_review_readiness](../tests/test_delivery_state_transitions.py)，`test_manifest_before_terminal_commit_is_not_publication_or_review_readiness` |
| 03 Web 任务执行 | **R** 补单次认领、停机拦截、迟到写回和持久删除意图。| `eb38cfc`、`a061011`、`9974a11` | [源码](../scripts/web_app.py)；[test_successful_acceptance_claims_job_once_before_preparing_command](../tests/test_web_submission.py)，`test_successful_acceptance_claims_job_once_before_preparing_command` |
| 04 任务索引恢复 | **R** 隔离单任务坏状态/写入失败；全局索引写入失败仍显式报错。| `b444cfa` | [源码](../scripts/web_app.py)；[test_global_index_write_errors_are_not_suppressed](../tests/test_web_recovery_isolation.py)，`test_global_index_write_errors_are_not_suppressed` |
| 05 上传事务 | **R** 接管失败保留或回滚本次资源；HTTP 清理失败不漏释放预留。| `eb38cfc`、`539fe01` | [源码](../scripts/web_app.py)；[test_real_http_acceptance_releases_capacity_despite_cleanup_error](../tests/test_upload_cleanup.py)，`test_real_http_acceptance_releases_capacity_despite_cleanup_error` |
| 06 提交额度 | **R** 只补异常路径的预留释放；频率、并发、每日与存储阈值未改。| `539fe01` | [源码](../scripts/web_app.py)；[test_admission_reservation_is_released_when_final_cleanup_fails](../tests/test_upload_cleanup.py)，`test_admission_reservation_is_released_when_final_cleanup_fails` |
| 07 上传解析 | **R** 只补解析中断及逐文件回收；HTTP/multipart 顺序规则未重写。| `539fe01` | [源码](../scripts/web_app.py)；[test_parser_interrupt_closes_and_removes_all_created_parts](../tests/test_upload_cleanup.py)，`test_parser_interrupt_closes_and_removes_all_created_parts` |
| 08 保留与回收 | **R** 先持久保存归属再清理；失败保留任务，拒绝目录别名误删。| `1418aa2` | [源码](../scripts/web_app.py)；[test_failed_cleanup_is_retained_and_other_expired_jobs_continue](../tests/test_retention_cleanup.py)，`test_failed_cleanup_is_retained_and_other_expired_jobs_continue` |
| 09 批量任务调度 | **R** 修复启动后登记、写盘与异常回收顺序，不改调度策略。| `eb38cfc` | [源码](../scripts/batch_analyze.py)；[test_launch_status_write_failure_still_owns_and_stops_child](../tests/test_batch_cleanup.py)，`test_launch_status_write_failure_still_owns_and_stops_child` |
| 10 批量运行锁 | **K** 保留既有原子锁及失效所有者判定；本轮未专项重构。| 未改 | [源码](../scripts/batch_analyze.py)；[相关索引](../tests/test_batch_analyze.py) |
| 11 子进程监管 | **K** 保留既有进程组、超时与输出上限监管；不能据此保证任意强杀恢复。| 未改 | [源码](../scripts/flayr_core/utils.py)；[相关索引](../tests/test_resource_budget.py) |
| 12 预处理版本发布 | **R** 清单提交前失败撤回新代，回滚失败保留旧备份。| `0e1ab17`、`86457a0` | [源码](../scripts/flayr.py)；[test_every_precommit_failure_preserves_old_generation_and_exception](../tests/test_preprocess_publication_transactions.py)，`test_every_precommit_failure_preserves_old_generation_and_exception` |
| 13 预处理缓存复用 | **K** 保留原指纹/产物核验和历史读取边界；本轮未逐分支重审。| 未改 | [源码](../scripts/flayr.py)；[相关索引](../tests/test_preprocess_generation_paths.py) |
| 14 媒体提取 | **K** 保留原提取、登记与预算处理；不新增真实媒体试跑。| 未改 | [源码](../scripts/flayr_core/video.py)；[相关索引](../tests/test_video_extraction.py) |
| 15 ASR 与重试 | **K** 保留 ASR 的失败/占位/无口播区别；未重验线上 ASR 服务。| 未改 | [源码](../scripts/flayr_core/asr.py)；[相关索引](../tests/test_audio_layers.py) |
| 16 翻译 | **K** 保留原文哈希和翻译状态；未重验真实译文质量或在线服务。| 未改 | [源码](../scripts/flayr_core/translation.py)；[相关索引](../tests/test_audio_layers.py) |
| 17 单帧 OCR | **K** 保留逐帧 OCR 尝试与严格回放；未新增在线 OCR 验证。| 未改 | [源码](../scripts/flayr_core/subtitle_track.py)；[相关索引](../tests/test_ocr_provider_policy.py) |
| 18 字幕轨完整度 | **K** 保留空轨/部分/失败聚合；本轮未重写字幕轨状态。| 未改 | [源码](../scripts/flayr_core/subtitle_track.py)；[相关索引](../tests/test_audio_layers.py) |
| 19 证据阅读材料组装 | **K** 保留阅读材料局部失败与来源审计；不把组装完成当内容通过。| 未改 | [源码](../scripts/flayr_core/video_evidence.py)；[相关索引](../tests/test_video_evidence_selection.py) |
| 20 远端视频上传与验证 | **O** 签名 URL 到期不删除对象；云端保留/清理策略未核，见 F02。| 未改 | [源码](../scripts/flayr_core/llm/r2_video.py)；[相关索引](../tests/test_r2_video.py) |
| 21 单次运行资源预算 | **T** 发送预留按尝试累计，传输失败与扩容共用预算；未扩额。| `eb38cfc` | [源码](../scripts/flayr_core/resources.py)；[test_mixed_transport_and_expansion_attempts_share_one_budget](../tests/test_retry_cache_protocols.py)，`test_mixed_transport_and_expansion_attempts_share_one_budget` |
| 22 流式响应解析 | **K** 保留流解析与原错误保留规则；本轮未重写 SSE 解析器。| 未改 | [源码](../scripts/flayr_core/llm/api.py)；[相关索引](../tests/test_llm_api.py) |
| 23 兼容接口传输恢复 | **T** 分别验证传输重试和扩容；耗尽仍保留 length，不当语义成功。| `eb38cfc` | [源码](../scripts/flayr_core/llm/api.py)；[test_exhausted_expansion_preserves_length_instead_of_claiming_semantic_success](../tests/test_retry_cache_protocols.py)，`test_exhausted_expansion_preserves_length_instead_of_claiming_semantic_success` |
| 24 原生 Gemini 恢复 | **T** 保留原生重试条件及答案拒绝；不引入扩容或线路回退。| `eb38cfc` | [源码](../scripts/flayr_core/llm/gemini_transport.py)；[test_answer_rejection_is_not_output_expansion_or_route_fallback](../tests/test_retry_cache_protocols.py)，`test_answer_rejection_is_not_output_expansion_or_route_fallback` |
| 25 外层 JSON 请求循环 | **T** 默认外层一次，显式重入受永久错误和预算约束。| `eb38cfc` | [源码](../scripts/flayr_core/llm/pipeline.py)；[test_outer_completion_is_explicit_and_permanent_errors_still_stop](../tests/test_retry_cache_protocols.py)，`test_outer_completion_is_explicit_and_permanent_errors_still_stop` |
| 26 Provider 回放与续跑 | **T** 严格 replay 禁止在线兜底，resume 权限另核，不混用。| `eb38cfc` | [源码](../scripts/flayr_core/llm/provider_artifacts.py)；[test_strict_replay_and_resume_have_distinct_fallback_permissions](../tests/test_retry_cache_protocols.py)，`test_strict_replay_and_resume_have_distinct_fallback_permissions` |
| 27 商品地基建立 | **K** 保留商品地基缓存与修复出口；未重验商品理解准确率。| 未改 | [源码](../scripts/flayr_core/llm/pipeline.py)；[相关索引](../tests/test_product_identity_payload.py) |
| 28 Stage1 证据生命周期 | **K** 保留 A/B/C/D 及冻结事实门禁；未重写取证或资格语义。| 未改 | [源码](../scripts/flayr_core/llm/pipeline.py)；[相关索引](../tests/test_stage_evidence_contracts.py) |
| 29 资格判断执行 | **T** 局部 D 不能抹掉非目标阶段失败；总体完成不冒充逐阶段闭合。| `eb38cfc` | [源码](../scripts/flayr_core/llm/pipeline.py)；[test_focused_qualification_preserves_failure_outside_its_targets](../tests/test_delivery_state_transitions.py)，`test_focused_qualification_preserves_failure_outside_its_targets` |
| 30 Stage1 一次恢复额度 | **T** 恢复留有终态后，即使 unknown 也不再次消耗机会。| `eb38cfc` | [源码](../scripts/flayr_core/llm/pipeline.py)；[test_consumed_recovery_does_not_restart_when_stages_remain_unknown](../tests/test_delivery_state_transitions.py)，`test_consumed_recovery_does_not_restart_when_stages_remain_unknown` |
| 31 事实缓存恢复 | **T** 验证真实缓存身份/字节/语义及恢复路径，未知不会偷偷再调用。| `eb38cfc` | [源码](../scripts/flayr_core/llm/cache.py)；[test_real_unresolved_cache_reuse_does_not_requalify_recover_or_charge](../tests/test_retry_cache_protocols.py)，`test_real_unresolved_cache_reuse_does_not_requalify_recover_or_charge` |
| 32 Stage2 分组与综合 | **T** 候选/分组完成不得覆盖最终失败或降级；未改评分。| `eb38cfc` | [源码](../scripts/flayr_core/llm/pipeline.py)；[test_candidate_completion_cannot_override_final_outcomes](../tests/test_delivery_state_transitions.py)，`test_candidate_completion_cannot_override_final_outcomes` |
| 33 结果收口 | **T** 补候选到最终收口联动回归，继续复用原 finalizer。| `eb38cfc` | [源码](../scripts/flayr_core/llm/pipeline.py)；[test_candidate_completion_cannot_override_final_outcomes](../tests/test_delivery_state_transitions.py)，`test_candidate_completion_cannot_override_final_outcomes` |
| 34 Phase C 定向复核 | **K** 保留定向复核及补丁合同；未开新复核或改候选机制。| 未改 | [源码](../scripts/flayr_core/llm/pipeline.py)；[相关索引](../tests/test_phase_c_patches.py) |
| 35 建议补齐 | **K** 保留一次建议补齐及失败记录；未开新建议生成调用。| 未改 | [源码](../scripts/flayr_core/llm/pipeline.py)；[相关索引](../tests/test_suppressed_action_review.py) |
| 36 事实校验标记有效期 | **K** 保留事实指纹标记与 resolver 消费条件；未重构标记生命周期。| 未改 | [源码](../scripts/flayr_core/postprocess/repair_stages.py)；[相关索引](../tests/test_stage_evidence_contracts.py) |
| 37 可选影子审计 | **K** 保留可选影子审计边界；本轮未专项执行或重审审计链。| 未改 | [源码](../scripts/flayr_core/llm/pipeline.py)；[相关索引](../tests/test_stage_evidence_contracts.py) |
| 38 人工阶段审核 | **T** 验证批准后修改重开、来源变化拒继承，原模型稿不全量作废。| `eb38cfc` | [源码](../scripts/flayr_core/human_review.py)；[test_new_valid_model_publication_does_not_inherit_old_human_confirmation](../tests/test_delivery_state_transitions.py)，`test_new_valid_model_publication_does_not_inherit_old_human_confirmation` |
| 39 建议动作审核 | **T** 阶段修改撤销相关建议批准，与阶段审核完成分开。| `eb38cfc` | [源码](../scripts/flayr_core/human_review.py)；[test_stage_edit_revokes_reviewed_outputs_not_the_original_model_draft](../tests/test_delivery_state_transitions.py)，`test_stage_edit_revokes_reviewed_outputs_not_the_original_model_draft` |
| 40 人工审核版报告 | **R** 渲染中断也清理派生报告对并重抛，保留原分析及审核决定。| `8074042` | [源码](../scripts/flayr_core/human_review.py)；[test_interrupted_render_removes_pair_without_changing_review_or_sources](../tests/test_review_publication_interruptions.py)，`test_interrupted_render_removes_pair_without_changing_review_or_sources` |
| 41 明确要求核对包 | **T** 核对包来源变化阻止追加；备注不改阶段批准或等同要求满足。| `eb38cfc` | [源码](../scripts/flayr_core/requirement_review.py)；[test_requirement_notes_and_changed_requirements_do_not_rewrite_stage_approval](../tests/test_delivery_state_transitions.py)，`test_requirement_notes_and_changed_requirements_do_not_rewrite_stage_approval` |
| 42 最终 HTML 检查 | **K** 保留 HTML 哈希、资源/区域检查；机械通过仍不批准内容。| 未改 | [源码](../scripts/validate_review_delivery.py)；[相关索引](../tests/test_review_workflow_checks.py) |
| 43 隔离模型实验 | **K** 保留隔离实验和一次请求策略；未启动实验或自动晋级。| 未改 | [源码](../scripts/flayr_core/llm/compact_eval.py)；[相关索引](../tests/test_compact_eval.py) |
| 44 离线回放产物 | **K** 保留独立输出与来源身份；未运行真实业务回放。| 未改 | [源码](../scripts/flayr_core/offline_replay.py)；[相关索引](../tests/test_offline_replay.py) |
| 45 验证样本冻结锁 | **K** 保留冻结/已使用及漂移门禁；不重开样本或改 GT。| 未改 | [源码](../scripts/flayr_core/validation_cohort.py)；[相关索引](../tests/test_validation_cohort.py) |
| 46 验证顺序门禁 | **K** 保留验证顺序和前置哈希；本轮未重新走完整在线验证链。| 未改 | [源码](../scripts/flayr_core/verification_order.py)；[相关索引](../tests/test_project_contracts.py) |
| 47 浏览器上传表单 | **R** 坏响应不造成功、pending 防重叠提交，保留原输入。| `eb38cfc` | [源码](../frontend/app.js)；[test_actual_app_with_deferred_responses](../tests/test_frontend_contracts.py)，`test_actual_app_with_deferred_responses` |
| 48 浏览器导航和轮询 | **R** 列表/详情按任务版本合并，已知终态不被旧响应重开。| `a061011` | [源码](../frontend/app.js)；[test_actual_app_with_deferred_responses](../tests/test_frontend_contracts.py)，`test_actual_app_with_deferred_responses` |
| 49 报告受众切换 | **R** HEAD/iframe 同一期限，失败恢复旧报告，离开视图取消迟到回调。| `a061011` | [源码](../frontend/app.js)；[test_actual_app_with_deferred_responses](../tests/test_frontend_contracts.py)，`test_actual_app_with_deferred_responses` |
| 50 报告内交互 | **O** 选择仍仅在内存；保存按钮仅 toast 却声称已保存，见 F01。| 未改 | [源码](../assets/creator_report.html)；[相关索引](../tests/test_creator_report.py) |
| 51 人工流程：C 简报维护 | **M** C 版本与受影响条目仍靠维护人记录；未做自动依赖传播或真实变更走查。| 未改 | [源码](../references/flayr-upgrade-frozen-plan-20261018.md)；[相关索引](../SKILL.md) |
| 52 人工流程：B 发送与采用 | **M** B 发送、回应与采用沿用原任务记录；无自动发送或新试点。| 未改 | [源码](../references/flayr-upgrade-frozen-plan-20261018.md)；[相关索引](../SKILL.md) |
| 53 人工流程：A 与作者稿定稿 | **M** 补分离检查/自审/人审/发送的交接记录；业务批准继续由人作出。| `eb38cfc` | [源码](../references/review-improvement.md)；[相关索引](review-improvement.md) |
| 54 人工流程：Zero 试点 | **M** 保留商务资格、分组、制作、发送与观察的人工边界；未启动试点。| 未改 | [现行入口](../SKILL.md)；原具体计划仅留本机证据，不链接未公开文件。|

## 未闭合项与取舍

### F01 对应原 50，旧报告的保存承诺

[模板](../assets/creator_report.html)的`btn-save`只调用`showToast`，文案却说“已保存”；[作者报告生成器](../scripts/flayr_core/creator_report.py)仍引用该模板。现行代码未修复此项，不能因浏览器上传/轮询已修就把它一并关闭。

下一决定由该旧页面的维护者和使用方作出：确认是否仍外发；若在用，另行选择移除/改成不声称持久化的文案，或明确保存位置与恢复行为后实现。关闭时至少验证“选择、操作、刷新后的实际结果”与文案一致。当前不得把按钮提示当作创作约定已留存；本次不修改模板或新增保存系统。

### F02 对应原 20，远端对象保留边界

[R2 代码](../scripts/flayr_core/llm/r2_video.py)生成的签名 GET 有效期是 3600 秒，没有客户端对象删除步骤；这只证明访问链接的期限。云端是否配置对象生命周期、实际保留多久，本次没有账户侧证据。

由存储维护者先只读核实桶生命周期与对象范围，再决定保留策略。没有策略证据前不写“上传视频一小时后已删除”，也不批量删除共享哈希对象。本次不访问云账号或执行清理；若要开发/配置删除策略，需要单独授权并核对引用与影响。

### 人工流程与未验证边界

原 51-54 保留人工处理。C 到 B 的版本关联/受影响范围已有文件级规则；实际影响链尚未走查，不宣称自动依赖追踪或业务闭环已完成。原行动建议里的“补 ABC 版本失效关系”只能按这个范围解释，不能改写成代码功能已实现。

真实 Windows 文件锁、断电与多进程目录替换仍不在本轮已验证保证中；需要这些保证时另定验证范围。保持原状不等于已证明无缺陷，也不自动产生新调用权限。

## 收口口径与维护

已纳入修复批次的工程变更已发布；对原 54 项的处理决定现在可逐项追溯。原 50 的保存承诺和原 20 的远端保留策略仍为 O 项，不能再写“原始盘点全部问题已解决”。旧的七类收口记录是当时已选工程范围的快照，不覆盖这两项。

后续只更新本表对应行和原任务证据，记录决定人/日期、影响范围、提交或验证依据。O 项没有核实前不能改成 R/T/K；不以新增一套全局状态框架代替这些决定。[异常处置说明](state-recovery-runbook.md)供本机操作时查阅。
