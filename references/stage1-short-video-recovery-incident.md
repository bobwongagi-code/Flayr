# Stage1 短视频恢复事件记录

- 样本：`colorkey-creamy-mud`，`creator_C_S5_S6`。
- 原视频 duration：18.1s。
- S5 fallback 默认窗口 `23-27s`，压到片尾空窗；加 padding 后实际请求为 `17.6-18.1s`，请求窗口长 0.5s；按相同参数本地重建的 MP4 封装时长约 0.667s。
- S6 请求窗口为 `7.6-18.1s`，与 S5 共用同一请求，因而连带失败。
- 已查存档中的 9 个命中文件、19 次匹配，均为同一个上游响应的副本；未发现第二个独立 case。
- 本记录不将旧 C2 清单不同步或流式截断归为同一根因。
- 最后一组启动前发现双方短视频也有落入同一类 S5 fallback 短窗口的风险（creator 14.58s、benchmark 18.782993s）；未发模型请求，不算第二例 provider 失败，也未据此宣称 S5 实际是否存在。
- 参考：阿里云视觉模型视频限制要求最短 2s：<https://help.aliyun.com/en/model-studio/vision>。

## Root 裁决

- local preflight 拒绝 `<2s` 的 native recovery 请求。
- 不扩窗、不改模型、不改资格、不删除 stage。
- 本次不声称恢复完成；运行保持 `DEGRADED`，人工 GT 保持独立。
- 真实模型新增调用：0。
- 已实现 preflight（24 新增 / 1 删除 payload）；6 项聚焦测试通过，233 项相关回归通过，项目规定 ruff（E4、E7、E9、F821，ignore E402）通过；未提交、未跑真实视频。
- 扩展 ruff 36 条与 HEAD 一致，非项目门禁失败。
