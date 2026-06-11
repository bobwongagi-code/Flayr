#!/usr/bin/env python3
"""门禁 round3 逐样本分析器（4d 真验收：执行分事实 + 生产推导路径）。

与 dev_score_gate.py（按预注册 T1-T7 给全量 go/no-go）不同，本脚本按单样本滚动出报告：
  1. 执行分稳定性：creator/benchmark_execution 每阶段 5 次重复的取值分布（新地基的方差）。
  2. 生产路径推导：normalize_analysis_result + derive_severity_from_facts（真实管线代码，
     非离线正则代理）得出的 severity vs 人工标签。
  3. 模型直判 severity 对照（看推导层相对模型判断的纠偏量）。

用法：python3 scripts/dev_analyze_gate3.py sample-are_xie [--repeat 5]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from dev_score_gate import LABELS, STAGES, load_raw, stage_map
from flayr_core.llm.parse import normalize_analysis_result
from flayr_core.postprocess.derive import derive_severity_from_facts

ROOT = Path(__file__).resolve().parents[1]
_ABBR = {"large": "l", "medium": "m", "small": "s", None: "·"}


def fmt_exec(value: object) -> str:
    if value is None:
        return "·"
    number = float(value)  # type: ignore[arg-type]
    return str(int(number)) if number == int(number) else str(number)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", help="如 sample-are_xie")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    name = args.sample if args.sample.startswith("sample-") else f"sample-{args.sample}"
    run = ROOT / "runs" / name
    labels = LABELS[name]

    # 逐 repeat：归一 + 生产推导
    per_repeat: list[dict] = []
    for i in range(1, args.repeat + 1):
        raw = load_raw(run, i)
        if not raw:
            per_repeat.append({})
            continue
        normalized = normalize_analysis_result(raw)
        derive_severity_from_facts(normalized)
        per_repeat.append({"stages": stage_map(normalized), "profile": normalized.get("category_profile")})

    profiles = [r.get("profile") for r in per_repeat if r.get("profile")]
    print(f"\n══ {name} round3（{sum(1 for r in per_repeat if r)}/{args.repeat} 份结果）══")
    if profiles:
        p = profiles[0]
        archetypes = Counter(
            (r["profile"].get("decision_threshold"), r["profile"].get("drive_type")) for r in per_repeat if r.get("profile")
        )
        print(f"category_profile: {p.get('category_name')} | 档位组合分布 {dict(archetypes)}")
        print(f"painpoints(第1次): {p.get('painpoints')}")
    else:
        print("⚠ category_profile 全部缺失（推导退化为中性权重）")

    agree = total = 0
    severe = flips = 0
    for sid in STAGES:
        c_execs, b_execs, derived, model_sev, statuses = [], [], [], [], []
        for r in per_repeat:
            stage = (r.get("stages") or {}).get(sid)
            if not stage:
                continue
            c_execs.append(stage.get("creator_execution"))
            b_execs.append(stage.get("benchmark_execution"))
            derived.append(stage.get("severity"))
            trace = stage.get("severity_derivation") or {}
            statuses.append(trace.get("status"))
            model_sev.append(trace.get("model_severity") or stage.get("severity"))
        mode, freq = Counter(v for v in derived if v).most_common(1)[0] if any(derived) else ("missing", 0)
        label = labels[sid]
        hit = mode == label
        agree += hit
        total += 1
        if {mode, label} == {"large", "small"}:
            severe += 1
        flip = "large" in set(derived) and "small" in set(derived)
        flips += flip
        mark = "✓" if hit else ("✗✗" if {mode, label} == {"large", "small"} else "✗")
        skipped = statuses.count("skipped") + statuses.count("error")
        print(
            f"  {sid}: 达人执行分[{','.join(fmt_exec(v) for v in c_execs)}] "
            f"标杆[{','.join(fmt_exec(v) for v in b_execs)}] | "
            f"推导[{','.join(_ABBR.get(v, '?') for v in derived)}] 众数={mode}({freq}/{len(derived)}) "
            f"标签={label} {mark}{' ⚠对跳' if flip else ''}"
            f" | 模型直判[{','.join(_ABBR.get(v, '?') for v in model_sev)}]"
            + (f" | ⚠推导跳过×{skipped}" if skipped else "")
        )
        # 不一致或不稳时打第一份溯源
        if not hit or freq < 4:
            for r in per_repeat:
                stage = (r.get("stages") or {}).get(sid)
                if stage and (stage.get("severity_derivation") or {}).get("status") == "derived":
                    t = stage["severity_derivation"]
                    print(f"      溯源: E={t.get('E')} W={t.get('W')} C={t.get('C')} S={t.get('S')} | {t.get('reason')}")
                    break

    print(f"  小计: 一致 {agree}/{total} | severe 错位 {severe} | 对跳 {flips}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
