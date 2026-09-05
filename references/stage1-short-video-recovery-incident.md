# Stage1 短视频恢复事件记录

- 样本：`colorkey-creamy-mud`，`creator_C_S5_S6`。
- 原视频 duration：18.1s。
- S5 fallback 默认窗口 `23-27s`，压到片尾空窗；加 padding 后实际请求为 `17.6-18.1s`，请求窗口长 0.5s；按相同参数本地重建的 MP4 封装时长约 0.667s。
- S6 请求窗口为 `7.6-18.1s`，与 S5 共用同一请求，因而连带失败。
- 已查存档中的 9 个命中文件、19 次匹配，均为同一个上游响应的副本；未发现第二个独立 case。
- 本记录不将旧 C2 清单不同步或流式截断归为同一根因。
- 最后一组启动前发现双方短视频也有落入同一类 S5 fallback 短窗口的风险（creator 14.58s、benchmark 18.782993s）；未发模型请求，不算第二例 provider 失败，也未据此宣称 S5 实际是否存在。
- 参考：阿里云视觉模型视频限制要求最短 2s：<https://help.aliyun.com/en/model-studio/vision>。

## 2026-09-05 零调用审计

- Stage1-C `payload.py:_recovery_stage_windows` 无条件默认 S1-S5 绝对桶，S6 默认 `tail5` / `tail-review10`。
- `media.py:select_stage_recovery_visual_inputs` 从 `artifacts.build_stage_frame_manifest` 的默认桶分配带阶段名的帧，并可取窗外最近帧。主 Stage1 是首尾 timeline 加全片 canonical sampled 帧，不是纯固定六桶；`video_evidence.direct_stage_coverage` 只做时间桶覆盖，不做阶段语义召回。
- Phase C 读取已有 `role_time_range + 2`，不直接默认桶；但上游范围正确性不能自动担保。
- 历史证据有限：Carslan c0 的 `20260823-v4` benchmark C 已查为 completed、窗口 `22.5-27.5s`，未证实 too-short；Retinol 50/52.5s 的 C replay completed、S5 GT NA，未证实 too-short；早期 PAPA C2 DEGRADED 原产物本次未定位，保持未闭合。当前 colorkey 是目前唯一检出的原始 400 短片错误，不代表唯一漏采风险。

## 2026-09-05 历史状态（Root 裁决）

- preflight 已提交 `fd866e6`，freeze 已提交 `52c946c`；新采样策略未实施。
- 不从旧窗口成功样本拟合 S5 距结尾 `N`，无证据支持位置固定；不新增关键词机制。
- 不改 GT、不自动重跑旧样本；不扩窗、不改模型、不改资格、不删除 stage。
- 本次不声称恢复完成；运行保持 `DEGRADED`，人工 GT 保持独立。
- 候选整段短视频测试已保存到 diagnostics patch 并撤回到 HEAD；6 项原聚焦测试通过。
- 当前新增模型调用为 0；无新采样修复、commit 或 push，最后样本未启动。

## 2026-09-05 两旧案事实补录

- `direct_stage_coverage` 仅是 diagnostic，不是资格消费者。历史 `41a926e` 中 `_extend_stage1_acquisition_for_recovery` 将局部 native 片段记为 `native_video`，`build_stage1_acquisition_manifest` 随后写成 `visual.coverage=full`、全 S1-S6 observed；native gate 跳过 positive 时间覆盖检查，并在 `full` 前提下接受 negative。限定原 6 run 中仅 4 个为 `COMPLETED`（`carslan-b1`、`carslan-powder-c1`、`colorblu-c1`、`colorkey-b1`）；`are_xie` 为 `FAILED`、`tashadiyana` 为 `DEGRADED`，不计完成分母。8 侧均为局部 window 却写成 `full`；这是暴露数，不是业务误判率，也不证明所有结论错误。
- `colorkey-b1` 与 `carslan-b1` 的 `_SUCCESS.json` 和 `run_state.json` 均对应各自标杆/达人输入且为 `COMPLETED`。两案 Stage1-A provider artifact 的 prompt metadata 均有 `image_tokens=7704`、无 `video_tokens`；旧审计所说的 `colorkey` B6/B7、`carslan` B5-B8 已在 A 中出现。A 原始请求体未存档，不能从当前文件补造更细的图片时间标签。
- 两案均确实有 Stage1-C，但 C 不以 S4 为 target：`colorkey-b1` benchmark C=`S1,S5,S6`（`0-3.5s`、`22.5-27.5s`、`42.509-53.009s`），creator C=`S5`（`22.5-25.433s`）；`carslan-b1` benchmark C=`S2,S5`（`2.5-6.5s`、`22.5-27.5s`），creator C=`S5`（`22.5-27.5s`）。上述 `media_windows` 来自对应 `stage1_recovery` 记录，S4 未列入任何 C window；两案 S4 内容事实来自 Stage1-A，Stage1-B 的 S3/S4 仅是资格/分组来源，不能据此自动归为同根因。

## 2026-09-06 实施及本地验证（未提交/冻结）

- Stage1-C 多阶段 target 共用一次全片请求；canonical 输入是真实时间帧，不携带阶段语义标签。
- video、ASR 与 coverage 使用同一组 recovery windows；窗口超限直接失败，不截断后降级或伪装成功。
- `stage1_acquisition` 版本 5 只有在实际发送的源时间范围并集覆盖全片时才可写 `full`；`full` 只证明素材上下文已送达，不证明模型识别完整或阶段真实存在。
- 完成数或未完成数发生变化不自动代表修好，必须区分真实暴露与回归。
- 历史 `8/8 sides` 是限定暴露数，不是错误率或业务误判率。
- 两旧 D 案的 S4 不在 C targets；不能借本修复重跑，或重开其诊断结论。
- 本地验证：960 tests 中 959 通过，唯一失败为 freeze contract 旧哈希；Ruff 按项目规则（E4/E7/E9/F821，ignore E402，`--isolated` 排除父目录配置）通过；`analysis_contracts` 仅同一 freeze 失败，不据此声称全门禁通过。
- old HEAD 行为红测复现固定窗、缺源 fallback、局部范围误写 `full`；最终对抗 review 已修复图片时间 label 丢失、同字节不同时间帧按 URL 去重、invalid 旧范围变 `full`。
- 日志（相对仓库）：`runs/diagnostics/stage1-recovery-window-fix-20260906/diagnostics/final-unittest.log`；真实视频调用 0，未上线。
- 本轮无模型/视频重跑、无 GT 改动、无 commit/refreeze/push，不生成新报告。
