# Transcript Window Migration Record

更新时间：2026-08-05

## 当前合同

- `transcript.srt`、`transcript.txt` 和 `transcript.words.json` 保留为本地原始/审计产物。
- 阶段 2/比较模型只接收 `transcript_windowed.md/json` 或已锁定的 `video_understanding` 事实。
- 有词级时间戳时，`transcript_windowed` 是窗口归因输入。
- 没有词级时间戳时，系统不得从粗粒度 SRT 自动推断精确 S1、CTA 或阶段边界；结果必须标记为时间粒度不足或转人工复核。

## 历史盘点

当前仓库盘点结果：

| 范围 | 预处理产物 | 有词级时间戳 | 仅粗粒度 SRT | 已生成窗口产物 | 处理结论 |
|---|---:|---:|---:|---:|---|
| 全部正式 `_preprocess.json` | 142 | 33 | 109 | 0 | 不把历史结果统一视为 clean_current |
| `mechanism-regression-dry-run` | 32 | 0 | 32 | 0 | 16 个样本 × benchmark/creator 两侧；仅机制回归/降级占位，不作准确率证据 |

`mechanism-regression-dry-run` 中的 `32` 明确是 16 个样本的两侧角色运行数，不是 32 个独立样本。该批次的 32 份输入都没有词级时间戳，涉及 S1、CTA 或口播阶段归因的历史观察不能标记为 clean。

## 历史分级

### `rebuildable_word_timing`

仓库共发现 34 个 `transcript.words.json` 文件，其中 1 个位于临时 generation 目录；与正式 `_preprocess.json` 对应的 33 份产物可以使用当前窗口合同重建 `transcript_windowed.md/json`，不需要重新调用模型。重建后仍需重新生成依赖窗口输入的 timeline/analysis artifact，并记录新的合同版本。

### `legacy_coarse`

没有词级时间戳的 109 份正式预处理产物只保留原始审计价值。除非某个样本是正在使用的关键回归案例，否则不主动批量补救；后续新运行自然替换这些结果。需要复用时必须显式标记 `legacy_coarse`，不能和当前结果混合统计。

## 关键 fixture 与结论

- `tests/fixtures/are_xie_s4_temporal_mismatch.json` 是合成的阶段归属 fixture，不依赖 SRT，结构上不受本次转写窗口修复影响。
- `are_xie/S5` 的 GT 标签是静态参考，不删除；仓库中相关实际分析产物来自旧输入，使用它验证 S5/derive 前必须重新核验。
- `youkoubo-c0/S3` 的 GT 标签是静态参考，不删除；当前仓库只有机制回归降级输入，没有对应的已完成新链路分析结果，因此不能把该标签与旧运行结果当作已验证配对。
- `s1-landing-shadow-validation.json` 当前不在正式仓库中，历史讨论中的 `93.1%` 无法确认输入版本，降级为待重跑的历史数字；不得继续作为 clean 基准引用。

## 人工 GT

`new12` 的结构化 Excel 目前仍为空模板；已有的文字草稿不构成冻结 GT。后续人工填写应使用当前版本的原视频和窗口安全辅助材料，不能把旧的完整 SRT 展示作为阶段边界依据。
