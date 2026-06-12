#!/usr/bin/env python3
"""生死测量打分器：按预注册阈值判定 stabilize 去过拟合的 go/no-go 门禁。

口径：读各样本 dev_stage2_result_raw_NN.json 的**模型原始 severity**（未经 stabilize），
即"删掉确定性特例之后"的世界；与 references/ground-truth-labels.md 的人工标签比对。

预注册阈值（2026-06-10 用户签定，跑前写死，不许测完再解释）：
  T1 每样本成功 repeats = 5/5（传输已流式化，缺的用 --skip-existing 补齐后再评）
  T2 每阶段 raw severity 众数频次 ≥ 4/5
  T3 全程零 large↔small 对跳（任一阶段取值同时出现 large 和 small 即 FAIL）
  T4 标签一致率：众数 vs 人工标签 ≥ 15/18，且不一致处无 large↔small 级错位
  T5 task_completion 众数频次 ≥ 4/5
  T6 are_xie S2 品类权重执行一致性：≥4/5 的 repeats 中 S2 文本含省钱类措辞，且众数 = medium
  T7 are_xie S5 自愈预言：众数 = large（认证→S5 修正后模型应自判出背书差距）
门禁不过 → 不删特例，保留薄兜底（TODO #1）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 人类真相源：references/ground-truth-labels.md（2026-06-10 用户定标）。此处为机器副本。
LABELS: dict[str, dict[str, str]] = {
    # are_xie S6：2026-06-11 用户复议 medium→small（门禁中模型 5/5 稳定判 small，采纳）
    "sample-are_xie": {"S1": "large", "S2": "medium", "S3": "small", "S4": "large", "S5": "large", "S6": "small"},
    "sample-kakwanreview": {"S1": "large", "S2": "medium", "S3": "medium", "S4": "small", "S5": "small", "S6": "large"},
    "sample-tashadiyana": {"S1": "medium", "S2": "small", "S3": "small", "S4": "large", "S5": "small", "S6": "small"},
    # Round5 样本外标签（2026-06-12 用户盲标；"不涉及"按 small 计；
    # bluetoothwanju/skincare 的 S6 为存疑标签——whisper 泰语，用户自注不确定）
    "sample-bluetoothwanju": {"S1": "small", "S2": "small", "S3": "medium", "S4": "medium", "S5": "small", "S6": "medium"},
    "sample-skincare": {"S1": "small", "S2": "large", "S3": "large", "S4": "large", "S5": "small", "S6": "large"},
    "sample-wukoubo-c0": {"S1": "medium", "S2": "large", "S3": "large", "S4": "large", "S5": "small", "S6": "small"},
    "sample-wukoubo-c1": {"S1": "medium", "S2": "large", "S3": "large", "S4": "large", "S5": "small", "S6": "small"},
    "sample-youkoubo-c0": {"S1": "large", "S2": "medium", "S3": "medium", "S4": "medium", "S5": "small", "S6": "small"},
    "sample-youkoubo-c1": {"S1": "large", "S2": "large", "S3": "large", "S4": "large", "S5": "small", "S6": "large"},
    "sample-youkoubo-c2": {"S1": "large", "S2": "large", "S3": "large", "S4": "large", "S5": "small", "S6": "large"},
}
STAGES = ["S1", "S2", "S3", "S4", "S5", "S6"]
_THRIFT_RE = re.compile(r"省钱|划算|性价比|便宜|jimat|murah|affordable", re.IGNORECASE)


def load_raw(run: Path, index: int) -> dict | None:
    path = run / f"dev_stage2_result_raw_{index:02d}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def stage_map(result: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for stage in result.get("stage_analysis", []):
        match = re.match(r"(S[1-6])", str(stage.get("stage") or ""))
        if match:
            out[match.group(1)] = stage
    return out


def mode_and_freq(values: list[str]) -> tuple[str, int]:
    counts = Counter(values)
    if not counts:
        return ("missing", 0)
    value, freq = counts.most_common(1)[0]
    return (value, freq)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"))
    args = parser.parse_args()
    runs_dir = Path(args.runs_dir)

    failures: list[str] = []
    agree = 0
    total_stages = 0
    severe_mismatch: list[str] = []

    for name, labels in LABELS.items():
        run = runs_dir / name
        raws = [load_raw(run, index) for index in range(1, args.repeat + 1)]
        ok_raws = [raw for raw in raws if raw is not None]
        print(f"\n##### {name}  成功 repeats: {len(ok_raws)}/{args.repeat}")
        if len(ok_raws) < args.repeat:
            failures.append(f"T1 {name} 成功 repeats {len(ok_raws)}/{args.repeat}")

        sev_by_stage: dict[str, list[str]] = {code: [] for code in STAGES}
        task_by_stage: dict[str, list[str]] = {code: [] for code in STAGES}
        s2_thrift_hits = 0
        for raw in ok_raws:
            stages = stage_map(raw)
            for code in STAGES:
                stage = stages.get(code, {})
                sev_by_stage[code].append(str(stage.get("severity") or "missing"))
                task_by_stage[code].append(str(stage.get("task_completion") or "missing"))
            if name == "sample-are_xie":
                s2_text = json.dumps(stages.get("S2", {}), ensure_ascii=False)
                if _THRIFT_RE.search(s2_text):
                    s2_thrift_hits += 1

        for code in STAGES:
            values = sev_by_stage[code]
            mode, freq = mode_and_freq(values)
            label = labels[code]
            mark = "✓" if mode == label else "✗"
            print(f"  {code} raw={values} 众数={mode}({freq}/{len(values)}) 标签={label} {mark}")
            # T2 稳定性
            if freq < 4:
                failures.append(f"T2 {name} {code} 众数频次 {freq}/5")
            # T3 对跳
            if "large" in values and "small" in values:
                failures.append(f"T3 {name} {code} 出现 large↔small 对跳")
            # T4 一致率
            total_stages += 1
            if mode == label:
                agree += 1
            elif {mode, label} == {"large", "small"}:
                severe_mismatch.append(f"{name} {code} 众数={mode} 标签={label}")
            # T5 task_completion 稳定性
            t_mode, t_freq = mode_and_freq(task_by_stage[code])
            if t_freq < 4:
                failures.append(f"T5 {name} {code} task_completion 众数频次 {t_freq}/5（{task_by_stage[code]}）")

        if name == "sample-are_xie" and ok_raws:
            s2_mode, _ = mode_and_freq(sev_by_stage["S2"])
            print(f"  [T6] S2 省钱措辞命中 {s2_thrift_hits}/{len(ok_raws)}，S2 众数={s2_mode}")
            if s2_thrift_hits < 4:
                failures.append(f"T6 are_xie S2 省钱措辞仅 {s2_thrift_hits}/5")
            if s2_mode != "medium":
                failures.append(f"T6 are_xie S2 众数={s2_mode}（应 medium）")
            s5_mode, _ = mode_and_freq(sev_by_stage["S5"])
            print(f"  [T7] S5 众数={s5_mode}（自愈预言应 large）")
            if s5_mode != "large":
                failures.append(f"T7 are_xie S5 众数={s5_mode}（应 large）")

    print(f"\n[T4] 标签一致率: {agree}/{total_stages}（阈值 ≥15/18）")
    if agree < 15:
        failures.append(f"T4 标签一致率 {agree}/{total_stages} < 15/18")
    if severe_mismatch:
        failures.append(f"T4 出现 large↔small 级错位: {severe_mismatch}")

    print()
    if failures:
        print("GATE RESULT: NO-GO")
        for item in failures:
            print(f"  ✗ {item}")
        print("→ 不删特例，保留薄兜底（见 TODO #1）。")
        return 2
    print("GATE RESULT: GO ——可按 TODO #1 处置清单执行去过拟合重构。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
