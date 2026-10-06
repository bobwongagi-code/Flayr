# 采集失败与无信号边界修复

2026-10-06。起点 be6b7a9，工程修复单独提交；与 S4 定义修正分开记录，随后机械 refreeze。
本轮零模型调用、零视频重跑，不改历史产物、人工 GT、判断标尺或 provider 路线。

## 修复范围

- subtitle_track.py：总状态不再只由是否存在字幕段决定。
  计划内帧全部检查成功才可 ready/empty；不完整但有成功字幕为 partial，无可用字幕为 failed。
  预算提前停止、缺失图片或时间、单帧失败都不能证明计划内完整检查。
- 显式 NONE 按原 OCR 指令识别为合法无字幕，返回 ocr_empty，不再生成一条 NONE 字幕或重试。
  无响应、纯坐标、不可读内容仍走原失败/重试处理，不能根据空列表推定无字幕。
- stage_evidence_contracts.py：partial 字幕保留可用正向内容，覆盖为 partial，不能用于证明完整无信号。
- video.py：锚点帧真实 PTS 缺失、越界或与请求不符时记错误，timestamp_seconds 保留 None。
  requested_timestamp_seconds 只记录请求位置，不能代替实际取到的帧时间；图片保留供审计。
- flayr.py：递增预处理 pipeline_version，使旧缓存不自动复用为修复后的新产物。

不引入新的 ASR 引擎/VAD、抽帧算法或外部依赖。原 S4 候选不因此启用。
未知时间的帧不能用于精确时间引用；由既有采集门禁处理，不把它解释成“没有结果”。

## 历史影响核查

范围为本地 runs 中 subtitle_track.json 与 frames/manifest.json；排除 checkout/worktree/.git。
按完整文件字节 SHA256 去重，不代表去重后每份都对应不同视频；重复实验仍不是新增样本。

- 311 份字幕轨文件，258 份唯一内容。
- 4 份唯一轨道存在逐帧失败却总状态 empty，未发现 ready 且含逐帧失败的轨道。
  对应 extension-20260903-b49cd25/self-leveling-grout-c2 和
  extension-20260901-bc44f48/female-bikini-trimmer 的 creator/benchmark。
  前者 analysis.json 双侧确实记录 empty；这说明错误状态被消费，不证明最终哪个阶段因此错判。
- 175 份唯一轨道包含文本为 NONE 的字幕段：属于无字幕标记污染候选，
  未逐帧回看、未判定是否影响最终结论，不能将这个数字称作 175 次业务错误。
- 121 份唯一帧清单含结构锚点，共 121 条锚点记录，实际时间均等于请求时间。
  旧清单没有记录 PTS 验证过程，仅凭两者相等无法判断是真实相等还是回填。
  因此这些只记为“时间真实性未能从旧清单独立核验”，不统计为 121 次错位。

本轮不按这些计数推算总体准确率，也不自动重开历史根因分析或改成功标记。

## 离线验证

回归用例覆盖 OCR 全失败、部分失败保留成功文本、合法全空、正常字幕、
预算零调用、显式 NONE 不重试、部分字幕不能证明 absent，
以及锚点 PTS 缺失/偏离/真实匹配三种情况。

相关验证：video_extraction 4 项、video_evidence_selection 25 项、
stage_evidence_contracts 200 项、architecture_contracts 253 项通过，共 482 项。
项目 ruff 选择项 E4/E7/E9/F821（忽略 E402）与 git diff --check 通过。
没有全量模型实验，没有宣称业务准确率提升。提交身份和最终版本门禁见 semantic-baseline-freeze.json/md；此前 482 项为提交前的局部验证，不冒称干净提交上的全量验收。
