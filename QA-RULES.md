# Flayr 分析结果校验规则

> v1.0 | 2026-05-25
>
> 校验对象：`llm.py` 输出的 `analysis_result.json`
>
> 校验时机：LLM 返回结果后、写入 `analysis.json` 和渲染 `report.html` 之前
>
> 实现位置：`llm.py` 内部的 `qa_check(result, meta) -> list[QAIssue]` 函数
>
> **字段唯一真相源**：所有字段名以 `references/analysis-output-schema.json` 为准。本文规则中的字段名与 schema 和 `flayr_core/llm.py:normalize_analysis_result` 完全一致。三者出现分歧时以 schema.json 为准。

---

## 执行流程

```
result = llm.analyze(input)
issues = qa_check(result, meta)

if not issues:
    # 全部通过，正常输出
    write_analysis(result)

elif retry_count < 1:
    # 第一次不过，定向修正
    result = llm.fix(result, issues)
    issues = qa_check(result, meta)
    if not issues:
        write_analysis(result)
    else:
        write_analysis(result, qa_warnings=issues)
        # 标记人工复核，但仍输出报告

else:
    # 不应到达，兜底
    write_analysis(result, qa_warnings=issues)
```

`meta` 包含校验所需的外部信息：

```python
meta = {
    "creator_duration": 113.8,      # 达人视频时长（秒）
    "benchmark_duration": 31.6,     # 标杆视频时长（秒）
    "valid_module_ids": [...],      # 从 structure_library_full.md 提取的官方模块编号列表
    "creator_transcript": "...",    # 达人转写文本
    "benchmark_transcript": "...", # 标杆转写文本
}
```

---

## 校验规则

### 第一类：Schema 完整性（必须全过，否则报告无法渲染）

#### R01 · 顶层字段完整

```
必须存在：
  - one_line_verdict       (string, 非空)
  - one_line_summary       (string, 非空)
  - executive_summary      (string, 通常与 one_line_summary 一致)
  - holistic_assessment    (object, 含五大维度速评 + 转化预判；每维独立评估，不得复制)
  - key_conclusions        (array of string, 1~5 条消费者视角关键结论)
  - product_visibility     (object)
  - video_understanding    (object, 含 benchmark/creator 的 evidence_units)
  - stage_analysis         (array, 长度 = 6，对应 S1~S6)
  - loop_closure           (object)
  - improvements           (array, 长度 1~5)

缺失任一项 → 触发修正
```

#### R02 · stage_analysis 数组完整

```
stage_analysis 必须为长度 6 的数组，顺序对应 S1~S6
（S5 不可缺失，结构库判断"可跳过"时仍要输出条目并把 task_completion 标为 "missing"）

每个 stage 必须包含以下字段（与 schema.json 对齐）：
  - stage                  (string, 例 "S1 Hook")
  - core_question          (string, 非空)
  - benchmark_time_range   (string, 例 "0~3s")
  - creator_time_range     (string, 例 "0~5.2s")
  - creator_module_id      (string, 必须是结构库官方编号或 "unknown")
  - benchmark_module_id    (string, 必须是结构库官方编号或 "unknown")
  - module_fit             (string, 枚举: "fit" / "degraded" / "unfit" / "unknown")
  - module_fit_reason      (string)
  - task_completion        (string, 枚举: "complete" / "partial" / "missing")
  - gap_type               (string, 枚举: "structural" / "execution" / "resource")
  - gap_summary            (array of string, 至少 1 条)
  - voice_performance      (object: pace / energy / key_pause / note)
  - benchmark_summary      (string, 非空)
  - benchmark_key_message  (string, 非空)
  - benchmark_evidence_ids (array of string, 至少 1 条，必须能在 video_understanding.benchmark.evidence_units 中找到)
  - benchmark_visual_evidence (array of string)
  - benchmark_support_status (string, 枚举: "supported" / "voice_only" / "visual_only" / "conflict")
  - benchmark_quote        (string, 本地语言；无口播时可空)
  - benchmark_quote_zh     (string, 中文翻译；无口播时可空)
  - creator_summary        (string, 非空)
  - creator_key_message    (string, 非空)
  - creator_evidence_ids   (array of string, 至少 1 条)
  - creator_visual_evidence (array of string)
  - creator_support_status (string, 同上枚举)
  - creator_quote          (string, 同上)
  - creator_quote_zh       (string, 同上)
  - gap                    (string, 非空)
  - evidence               (array of string, 至少 1 条)
  - severity               (string, 枚举: "large" / "medium" / "small")

缺失任一字段 → 触发修正，修正提示中列出缺失字段和所属 stage
```

#### R03 · improvements 数组完整

```
improvements 数量：1~5 项（上限 5；下限 1 避免 LLM 主动判断"只有 1 条值得改"时被强迫凑数）

每个 improvement 必须包含（与 schema.json 对齐）：
  - priority               (integer, 1~5，按 GMV 杠杆排序，1 为最优先)
  - title                  (string, 非空)
  - target_stage           (string, "S1"~"S6")
  - gmv_impact             (string, 枚举: "极高" / "高" / "中")
  - gap_type               (string, 枚举: "structural" / "execution" / "resource")
  - time_range             (string, 例 "27~31s")
  - creator_time_range     (string, 达人视频中要修改的具体时间段)
  - benchmark_time_range   (string, 标杆可参考的具体时间段)
  - problem                (string, 非空)
  - benchmark_reference    (string, 非空)
  - benchmark_evidence_ids (array of string, 至少 1 条，必须能在 benchmark.evidence_units 中找到)
  - suggestion             (string, 非空)
  - actions                (array of string, 至少 1 条)
  - gmv_reason             (string, 非空)
  - evidence               (array of string, 至少 1 条)
  - creator_script         (string, 本地语言；无有效口播时按规则 #10 处理)
  - creator_script_zh      (string, 中文翻译)
  - base_frame_suitability (string, 枚举: "usable" / "no_suitable_frame")
  - best_base_frame_time   (string, 例 "18.5s"；no_suitable_frame 时留空)
  - base_frame_evidence_id (string, 对应达人 evidence_unit ID；留空时同上)
  - base_frame_reason      (string, 非空)
  - aigc_prompt            (string, 非空)
  - aigc_image_path        (string, 分析阶段留空)
  - expected_effect        (string, 非空)

缺失任一字段 → 触发修正
```

#### R04 · product_visibility 完整

```
必须包含：
  - first_appearance_sec   (number, ≥ 0)
  - total_screen_time_sec  (number, ≥ 0)
  - video_duration_sec     (number, > 0)
  - ratio                  (number, 0~1)
  - estimation_note        (string, 估算依据；精确统计或粗估都要说明)

校验：ratio ≈ total_screen_time_sec / video_duration_sec（允许 ±0.05 误差）
```

---

### 第二类：时间逻辑（确保帧选取和报告展示正确）

> 说明：schema 中 `time_range` 是字符串描述（如 `"0~3s"` 或 `"0.0s - 3.0s"`），需要先解析为 `(start, end)` 秒数再校验。解析参考 `flayr_core/artifacts.py:parse_time_range_seconds`。

#### R05 · time_range 合法

```
对 stage_analysis 中所有 benchmark_time_range / creator_time_range，
以及 improvements 中所有 time_range / benchmark_time_range / creator_time_range：
  解析后必须满足：
    - start < end
    - start ≥ 0
    - end > 0

违反 → 触发修正，列出具体哪个 stage/improvement 的哪个 time_range 字段不合法
```

#### R06 · stages 时间段不重叠

```
分别对 benchmark_time_range 和 creator_time_range 校验：

按解析后的 start 排序，相邻 stage 不得重叠：
  stages[i].end ≤ stages[i+1].start

允许 ≤ 0.5s 的微小重叠（转场过渡）
超过 0.5s 重叠 → 触发修正
```

#### R07 · stages 时间段不超出视频时长

```
所有达人侧 stage：
  creator_time_range.end ≤ meta.creator_duration + 1.0（允许 1 秒容差）

所有标杆侧 stage：
  benchmark_time_range.end ≤ meta.benchmark_duration + 1.0

违反 → 触发修正
```

#### R08 · 阶段引用的 evidence_unit 时间必须与该阶段时间相交

```
对每个 stage：
  对 creator_evidence_ids 中每个 id：
    - 必须能在 video_understanding.creator.evidence_units 中找到对应条目
    - 该 evidence_unit 的 time_range 必须与 stage.creator_time_range 相交
      （允许 ±0.5s 边界容差）

  对 benchmark_evidence_ids 中每个 id：
    - 必须能在 video_understanding.benchmark.evidence_units 中找到对应条目
    - 该 evidence_unit 的 time_range 必须与 stage.benchmark_time_range 相交

违反 → 触发修正，列出具体 stage、引用 id、evidence 时间、合法范围
```

#### R09 · best_base_frame_time 在达人视频范围内

```
对每个 improvement，当 base_frame_suitability == "usable" 时：
  解析 best_base_frame_time 为秒数后：
    - ≤ meta.creator_duration + 1.0
    - ≥ 0
  base_frame_evidence_id 必须能在 video_understanding.creator.evidence_units 中找到

当 base_frame_suitability == "no_suitable_frame" 时：
  best_base_frame_time 和 base_frame_evidence_id 必须为空字符串

违反 → 触发修正
```

---

### 第三类：模块编号校验（确保与结构库一致）

#### R10 · module_id 必须是官方编号

```
对每个 stage 的 creator_module_id 和 benchmark_module_id：
  必须满足以下之一：
    - 在 meta.valid_module_ids 列表中（来自 structure_library_full.md）
    - 等于 "unknown"（模型显式声明无法识别）

不在列表中且不为 "unknown" → 触发修正，列出无效编号和所属 stage
```

#### R11 · module_id 前缀与 stage 匹配

```
对每个 stage 的 creator_module_id 和 benchmark_module_id（非 "unknown"）：

  stage[0]（S1 Hook）的 module_id 必须以 "S1-" 开头
  stage[1]（S2 产品引出）的 module_id 必须以 "S2-" 开头
  ...以此类推到 stage[5]（S6 CTA）

例：stage = "S3 使用过程", creator_module_id = "S1-A" → 错误
违反 → 触发修正
```

---

### 第四类：证据交叉污染校验（确保达人和标杆信息不混淆）

#### R12 · 达人 evidence_units 不包含标杆口播

```
对 video_understanding.creator.evidence_units 中每条的 voiceover / voiceover_zh：
  不得包含 meta.benchmark_transcript 中的连续 10 字以上片段

同时对每个 stage 的 creator_quote：
  不得包含 meta.benchmark_transcript 中的连续 10 字以上片段

违反 → 触发修正，列出具体位置（unit id 或 stage）和被污染的文本片段
```

#### R13 · 标杆 evidence_units 不包含达人口播

```
对 video_understanding.benchmark.evidence_units 中每条的 voiceover / voiceover_zh：
  不得包含 meta.creator_transcript 中的连续 10 字以上片段

同时对每个 stage 的 benchmark_quote：
  不得包含 meta.creator_transcript 中的连续 10 字以上片段

违反 → 触发修正
```

---

### 第五类：排序逻辑校验（确保提升点按 GMV 杠杆排序）

#### R14 · improvements 按 priority 递增

```
improvements[0].priority = 1
improvements[1].priority = 2
...依次递增，不跳号

违反 → 触发修正
```

#### R15 · GMV 影响权重不逆序

```
GMV 权重映射：
  "极高" = 3
  "高"   = 2
  "中"   = 1

improvements 按 priority 排序后：
  priority 靠前的 gmv_impact 权重 ≥ priority 靠后的 gmv_impact 权重

例：priority=1 的 gmv_impact="中"，priority=2 的 gmv_impact="极高" → 违反

允许同权重内按 target_stage 对应阶段的 severity 排序（large > medium > small）
违反 → 触发修正
```

---

### 第六类：内容合理性校验（兜底检查）

#### R16 · severity 与 gap_summary 一致性

```
severity = "small" 但 gap_summary 有 3 条以上 → 警告（不阻断，仅标记）
severity = "large" 但 gap_summary 只有 1 条 → 警告
severity = "medium" 但 gap_summary 含"无明显差距" → 阻断，severity 应为 small
6 个 stage 的 severity 全部相同 → 警告（几乎不可能，检查是否 LLM 偷懒）
```

#### R17 · product_visibility 合理性

```
first_appearance_sec > video_duration_sec → 错误，触发修正
total_screen_time_sec > video_duration_sec → 错误，触发修正
ratio > 1.0 或 ratio < 0 → 错误，触发修正
```

#### R18 · loop_closure 字段完整

```
必须包含：
  - pain_resolved_in_s4    (boolean)
  - benefit_delivered_in_s6 (boolean)
  - suspense_revealed       (boolean)
  - note                    (string, 非空)

suspense_revealed = true 时，suspense_reveal_time 必须是 number 且 > 0
suspense_revealed = false 时，suspense_reveal_time 可以是 null
```

---

## 修正 Prompt 模板

当 `qa_check` 返回 issues 时，构造以下修正请求：

```
你之前输出的分析结果存在以下问题。请仅修正这些问题，其他内容保持不变，输出修正后的完整 JSON。

问题列表：
{issues_list}

注意：
1. 只修正上述列出的问题，不要改动其他字段
2. 修正后的 JSON 必须保持完整，不要省略未修改的部分
3. 如果某个问题需要重新分析才能修正（如 evidence_frames 时间错误），
   请基于你之前的分析重新选择合理的时间点
```

`issues_list` 格式示例：

```
1. [R08] S3 的 creator_evidence_ids 包含 "C5"，
   该 evidence_unit 的 time_range 是 45.0~46.5s，
   但 S3 的 creator_time_range 是 6.0~15.0s，二者不相交。
   请改为引用落在 S3 时间范围内的 evidence_unit，或调整 S3 的 creator_time_range。

2. [R10] S1 的 creator_module_id = "S1-X"，
   不在结构库官方编号列表中。
   有效的 S1 编号包括：S1-A, S1-B, S1-C, S1-D, S1-E。
   请选择正确的编号，或在无法识别时填 "unknown"。

3. [R15] improvements 排序不正确：
   priority=1 的 gmv_impact="中"（权重 1），
   priority=2 的 gmv_impact="极高"（权重 3）。
   请按 GMV 杠杆重新排序。
```

---

## 规则优先级

校验失败时的处理策略：

| 级别 | 规则 | 处理 |
|------|------|------|
| **阻断** | R01~R04（schema 完整性） | 必须修正，否则报告无法渲染 |
| **阻断** | R05~R09（时间逻辑） | 必须修正，否则帧选取错误 |
| **阻断** | R10~R11（模块编号） | 必须修正，否则与结构库不一致 |
| **阻断** | R12~R13（交叉污染） | 必须修正，否则分析结论错误 |
| **阻断** | R14~R15（排序逻辑） | 必须修正，否则提升建议优先级错误 |
| **警告** | R16（severity 与 gap_summary 一致性） | 不阻断，标记到报告 qa_warnings |
| **阻断** | R17（product_visibility） | 必须修正，数值明显错误 |
| **阻断** | R18（loop_closure） | 必须修正，闭环校验数据不完整 |

---

## 实施路径

### Phase 1（立即实施）

先上最硬、最容易出错的规则：

- R01~R04：schema 完整性 → 防止报告渲染崩溃
- R05, R07, R08：时间逻辑基础 → 防止帧选取错位
- R10~R11：模块编号 → 防止自创编号

这 8 条规则覆盖了最常见的 LLM 输出问题。

### Phase 2（跑几轮真实数据后）

根据实际失败模式补充：

- R06, R09：时间段重叠和基底帧范围
- R12~R13：交叉污染（需要转写文本比对）
- R14~R15：排序逻辑

### Phase 3（稳定后）

- R16~R18：合理性校验和闭环完整性
- 根据实际数据积累新规则
