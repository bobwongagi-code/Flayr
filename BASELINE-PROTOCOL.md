# Baseline 测量协议（常法，2026-06-21 起草）

> 起因：2026-06-21 发现——乱的根**不是「旧数据脏」，是「一直用单跑测量」**。
> temp=0 仍不可复现，执行分 B/C 在重跑间 ±1 档横跳，derive 阈值把它放大成 severity 翻档。
> 每个单跑命中率都是从会抖的分布里抽的一个点，没有误差棒。
> ground-truth-labels.md 早记过「单跑虚高~2分、稳定取众数」，本协议把它升为强制常法。
>
> **本文件是常法：以后每个 baseline / 实验都按它来，不按它得的数字一律不算数。**

## 三条铁律

### 铁律 1：单跑作废，一切数字 = N 次众数（稳定口径）+ 报一致度
- 任何命中率 / severity / B / C，**必须是 N 次重跑取众数**，N 见下（`[待 5× 定]`）。
- **同时报一致度**（N 次里几次落同档），把 wobble 摆明面、不藏。
- **一致度门槛（已钉死，不等 5×）**：`< 4/5`（或 `< 3/3`）同档 = **标红「不可信」**。即 N=5 需 ≥4 次同档、N=3 需 3 次全同，才算稳定结论；否则该格判「不可信」，不得用于汇报或拍板。
- 5× 实验本身也用这把尺读结果。

### 铁律 2：基准不可变 + 锁代码版本
- 基准 = 某个 git tag 下、固定样本集各 N 次众数。
- **存进只读目录、永不就地覆盖**。基准永远可追溯到「哪版代码 tag + 哪次 N 跑众数」。
- 改了代码要新基准 → 新 tag + 新目录，旧基准留着对照，不覆盖。

### 铁律 3：输入 / 基准 / 实验 三层物理隔离
```
runs/sample-X/                              ← 输入层，永不动
    analysis.json        （清单 videos + 输入 product；嵌的旧 stage_analysis 是死字节，每跑覆盖）
    _preprocess.json / benchmark/ / creator/ （帧/音/转写——重建贵，保住）
runs_baseline_v3/X/run_1..N/ + mode.json    ← 基准，锁 tag，只读，永不覆盖
runs_exp/<实验名>/X/run_1..N/ + diff_vs_baseline.json  ← 实验，每个独立版本，绝不碰基准
```
**实验 = (锁定代码 tag) × (N 次众数) × (对基准 diff)**。三个都钉住，不可能再「拿错误数据做实验」。

## 稳定口径计算器（把纪律写进代码，不靠记）
小脚本 `scripts/stable_verdict.py`（待写）：
- 输入：某样本 N 次 result。
- 输出：每阶段 **众数 severity + 一致度 (k/N) + 众数 B/C**；一致度 `< 4/5`（或 `<3/3`）**自动标红「不可信」**。
- 任何报告/实验结论必须经它产出，禁止手抄单跑值。

## 待 5× 定的两个参数
- **N**：`[待 5× 定]`——wobble 凶 → N=5；温和 → N=3。
- **facts 重跑策略**：`[待 5×]`——5× 测 video_facts 跨次稳不稳：
  - facts 稳 → 抽一次 facts、只 N 跑阶段2（省一大笔 API）；
  - facts 也抖 → 必须全流程（Step-0+facts+对比）N 跑。

## 删除范围（清旧账，等 5× 完 + 确认后执行）
- **删**（污染输出 + 历史垃圾）：所有 `analysis_result*.json`、`_blindcmp_*`、`dev_stage_*`、`gate_*`、`facts_backup_*`、`llm_*response/request`、`*.sse`、`*.stream_req.json`、`preHED/preHUD/v2bak` 备份、`runs/_probe`、`runs/_*_status.json`。
- **留**：`analysis.json`（清单+输入，已归类）、`_preprocess.json`、`benchmark/` `creator/`（帧/音/转写）、`references/` 全部（含**不可再生的 ground-truth-labels.md**）。
- runs/ 为 gitignore（~950M）→ 删除是**本地操作，不动 git 历史**。

## 子集先跑（验协议，再扩全量）
协议先在子集跑通验证，**子集必须含一条已知会抖样本做压力测试**（否则只验了正常路径）：
- **已知抖（压力测试）**：`carslan-b0`（B 在 preHUD→current 间掉过、severity 翻过档）。
- **演示不对称（放大器触发路径）**：`skincare`（达人没演、放大器该点火）。
- **干净（正常路径）**：`[待定，从 ground-truth 覆盖里挑一条稳的]`（候选 colorkey-b0 / paint）。

## 执行顺序
1. 5× 完 → 用铁律1门槛读：定 **N** + **facts 策略**。
2. 起草填数、封版本文件。
3. 清删旧账（按上）。
4. 打 tag `baseline-v3-code`（锁当前已并 main 的代码）。
5. 子集 3 条 × N 跑 → 稳定口径计算器出众数 → **验协议**（含 carslan 压力测试）。
6. 协议通过 → 扩全 17 × N → 冻结 `baseline-v3`（只读目录 + tag）。

---
**一句话**：以后没有「单跑数字」，只有「某 tag 下 N 次众数 + 一致度」。低一致度自动标不可信。基准只读、实验隔离。这是不再产生错误数据的根治。
