#!/usr/bin/env python3
"""task_completion 枚举修复验证：映射器单测 + 已有 15 个 raw 的映射后稳定性回扫。

回扫是关键实验（零 LLM 成本）：若映射后的 task_completion 跨 run 稳定（众数 ≥4/5），
说明模型对"完成度"的判断本身是稳的、只是没说枚举话——gating 前提可恢复；
若映射后仍不稳，则是判断本身不稳，gating 方案需重新评估。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.parse import normalize_task_completion  # noqa: E402

failures: list[str] = []


def check(name: str, got: str, expect: str) -> None:
    ok = got == expect
    if not ok:
        failures.append(f"{name}: got={got} expect={expect}")
    print(f"{'✓' if ok else '✗'} {got:8} (期望 {expect:8}) <- {name}")


# ---- 单测：2026-06-11 门禁 T5 实测观察值全集 ----
CASES = [
    ("complete", "complete"),
    ("partial", "partial"),
    ("missing", "missing"),
    ("both_complete", "complete"),
    ("both_completed", "complete"),
    ("both_completed_well", "complete"),
    ("both_completed_with_different_focus", "complete"),
    ("completed", "complete"),
    ("complete", "complete"),
    ("full", "complete"),
    ("good", "complete"),
    ("incomplete", "partial"),
    ("benchmark_complete_creator_partial", "partial"),
    ("benchmark_complete_creator_missing", "missing"),
    ("creator_completed_benchmark_missing", "complete"),
    ("creator_stronger", "complete"),
    ("creator_superior", "complete"),
    ("creator_better", "complete"),
    ("creator_incomplete", "partial"),
    ("creator_only", "complete"),
    ("benchmark_stronger_creator_weaker", "partial"),
    ("benchmark_superior", "partial"),
    ("benchmark_better", "partial"),
    ("both_missing", "missing"),
    ("双方均完成了任务。", "complete"),
    ("双方均完成了介绍产品成分和功能的任务。", "complete"),
    ("双方均完成任务，达人额外增加了促销和防伪信息。", "complete"),
    ("双方均清晰完成任务。", "complete"),
    ("达人完成了产品定位，但未完成激发用户迫切需求的任务。", "partial"),
    ("达人未完成效果验证的任务，标杆出色完成。", "missing"),
    ("标杆完成任务，达人未涉及。", "missing"),
    ("标杆出色完成，达人基本完成但力度不足。", "partial"),
    ("标杆通过用户案例完成任务，达人未完成。", "missing"),
    ("标杆完成任务，达人部分完成。", "partial"),
    ("达人未完成任务，标杆通过视觉化认证信息出色完成。", "missing"),
    ("标杆通过官方图文强力完成任务，达人仅口头提及部分认证。", "partial"),
    ("双方均完成了引导购买的任务，达人表现更积极。", "complete"),
    ("", "partial"),
]

print("==== 映射器单测 ====")
for raw, expect in CASES:
    check(repr(raw)[:60], normalize_task_completion(raw), expect)

# 语义争议项说明：'达人未完成效果验证的任务' 判 missing（功能整体未达成）；
# '达人完成了X，但未完成Y' 判 partial（做了一部分）。

# ---- 回扫：15 个已有 raw 的映射后稳定性 ----
print("\n==== 映射后 task_completion 稳定性回扫（15 个已有 raw，零成本）====")
STAGES = ["S1", "S2", "S3", "S4", "S5", "S6"]
unstable = 0
total = 0
for sample in ("sample-are_xie", "sample-kakwanreview", "sample-tashadiyana"):
    run = ROOT / "runs" / sample
    by_stage: dict[str, list[str]] = {code: [] for code in STAGES}
    for index in range(1, 6):
        path = run / f"dev_stage2_result_raw_{index:02d}.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for stage in data.get("stage_analysis", []):
            match = re.match(r"(S[1-6])", str(stage.get("stage") or ""))
            if match:
                by_stage[match.group(1)].append(normalize_task_completion(stage.get("task_completion")))
    print(f"  [{sample}]")
    for code in STAGES:
        values = by_stage[code]
        if not values:
            continue
        mode, freq = Counter(values).most_common(1)[0]
        total += 1
        flag = "✓" if freq >= 4 else "✗"
        if freq < 4:
            unstable += 1
        print(f"    {code} {values} 众数={mode}({freq}/{len(values)}) {flag}")

print(f"\n回扫结论：{total - unstable}/{total} 阶段映射后稳定（众数≥4/5）")
print("RESULT:", "PASS" if not failures else f"FAIL ({len(failures)}): {failures[:3]}")
sys.exit(1 if failures else 0)
