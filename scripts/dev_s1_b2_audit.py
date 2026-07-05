#!/usr/bin/env python3
"""S1 Hook flag 切片 B 验收审计器：读某品 N 次重跑结果，量 hook flag 稳定性 + 窗口泄漏。

用途（B2）：判 hook flag 在 0.69 抖动区到底稳不稳、landing 是否靠"后段材料补足"凑出来。
不进 severity、不改 pipeline，纯离线审计。配合 dev_test_stage2 --repeat N 产出的
dev_stage2_result_0*.json，或 baseline 的 run_*_result.json。

量三件（见讨论）：
  1. landing_met 稳不稳（众数 + 一致率）
  2. type / dims / anchors 一致率（type 仅记录，不进 severity）
  3. landing_window_leak 频率：landing=true 但 landing_reason 引用了 hook_boundary_seconds
     之后的时间戳（旧结果无边界时回退默认 6s）→ 疑似"用后段痛点/产品补足三件套"

用法：
  python3 scripts/dev_s1_b2_audit.py runs/sample-carslan-b0            # 读该目录下 dev_stage2_result_0*.json
  python3 scripts/dev_s1_b2_audit.py runs/sample-carslan-b0 --from-stability  # 只读本次成功记录，避开旧文件污染
  python3 scripts/dev_s1_b2_audit.py runs_baseline_v3/carslan-b0 --glob 'run_*_result.json'
  python3 scripts/dev_s1_b2_audit.py runs/sample-* --cutoff 6.0        # 多品
"""
from __future__ import annotations

import argparse
import glob
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

_TS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s")  # 抓 landing_reason 里的时间戳，如 10.4s / 0-10.4s / 6.8s


def _s1_hook(result_path: Path, side: str) -> dict[str, Any] | None:
    """从单次结果取 S1 某侧的 hook 对象。"""
    try:
        d = json.loads(result_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    s1 = next((s for s in d.get("stage_analysis", []) if str(s.get("stage", "")).startswith("S1")), {})
    h = s1.get(f"{side}_hook")
    return h if isinstance(h, dict) else None


def _max_ts(reason: str) -> float:
    """landing_reason 里引用的最大时间戳（秒）；无时间戳返回 0。"""
    vals = [float(m) for m in _TS_RE.findall(reason or "")]
    return max(vals) if vals else 0.0


def _hook_boundary(hook: dict[str, Any], fallback: float) -> float:
    value = hook.get("hook_boundary_seconds")
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)) and float(value) >= 0:
        return float(value)
    return fallback


def _agreement(values: list[Any]) -> tuple[Any, float]:
    """众数 + 一致率（众数占比）。空 → (None, 0)。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, 0.0
    counts = Counter(map(str, vals))
    top, n = counts.most_common(1)[0]
    return top, n / len(vals)


def result_files(run_dir: Path, pattern: str, from_stability: bool) -> list[Path]:
    if not from_stability:
        return sorted(run_dir.glob(pattern))
    path = run_dir / "dev_stage2_stability.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    files = []
    for record in data.get("records") or []:
        if not isinstance(record, dict) or not record.get("ok"):
            continue
        result_path = Path(str(record.get("result_path") or ""))
        if result_path.is_file():
            files.append(result_path)
    return files


def audit_sample(run_dir: Path, pattern: str, cutoff: float, from_stability: bool) -> dict[str, Any] | None:
    """对一个品的 N 次结果做 S1 hook 审计。"""
    files = result_files(run_dir, pattern, from_stability)
    if not files:
        return None
    rows: dict[str, dict[str, Any]] = {}
    for side in ("creator", "benchmark"):
        hooks = [h for h in (_s1_hook(f, side) for f in files) if h is not None]
        if not hooks:
            continue
        type_mode, type_agree = _agreement([h.get("type") for h in hooks])
        land_mode, land_agree = _agreement([h.get("landing_met") for h in hooks])
        dim_agree = {
            d: _agreement([(h.get("dims") or {}).get(d) for h in hooks])[1]
            for d in ("camera", "copy", "sound", "rhythm")
        }
        anchor_mode, anchor_agree = _agreement([h.get("anchors_proposition") for h in hooks])
        # 窗口泄漏：优先用模型输出的 S1/S2 边界；旧结果无边界时回退 cutoff。
        leaks = [
            (
                h.get("landing_window_leak") is True
                or (
                    h.get("landing_met") is True
                    and _max_ts(h.get("landing_reason") or "") > _hook_boundary(h, cutoff) + 0.3
                )
            )
            for h in hooks
        ]
        boundaries = [_hook_boundary(h, cutoff) for h in hooks]
        boundary_mode, boundary_agree = _agreement(boundaries)
        rows[side] = {
            "n": len(hooks),
            "type": (type_mode, round(type_agree, 2)),
            "landing": (land_mode, round(land_agree, 2)),
            "boundary": (boundary_mode, round(boundary_agree, 2)),
            "dims_agree": {k: round(v, 2) for k, v in dim_agree.items()},
            "anchors": (anchor_mode, round(anchor_agree, 2)),
            "leak_rate": round(sum(leaks) / len(leaks), 2),
            "leak_reasons": [h.get("landing_reason") for h, lk in zip(hooks, leaks) if lk][:3],
        }
    return rows or None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", help="品的 run 目录（可多个 / 通配）")
    ap.add_argument("--glob", default="dev_stage2_result_0*.json", help="单品 N 次结果文件名 glob")
    ap.add_argument("--cutoff", type=float, default=6.0, help="钩子窗口秒数上限，reason 超此时间戳算泄漏")
    ap.add_argument("--from-stability", action="store_true", help="只读取 dev_stage2_stability.json 中本次成功记录的 result_path，避免旧结果污染")
    args = ap.parse_args()

    dirs: list[Path] = []
    for d in args.run_dirs:
        dirs += [Path(p) for p in glob.glob(d)] or [Path(d)]

    print(f"窗口泄漏判据：优先用 hook_boundary_seconds；旧结果无边界时回退 {args.cutoff}s\n")
    for run_dir in dirs:
        rows = audit_sample(run_dir, args.glob, args.cutoff, args.from_stability)
        if not rows:
            print(f"[{run_dir.name}] 无结果（glob={args.glob}）")
            continue
        print(f"=== {run_dir.name} ===")
        for side, r in rows.items():
            print(f"  {side:9} n={r['n']} | type={r['type'][0]}({r['type'][1]}) "
                  f"landing={r['landing'][0]}({r['landing'][1]}) "
                  f"boundary={r['boundary'][0]}({r['boundary'][1]}) anchors={r['anchors'][0]}({r['anchors'][1]})")
            print(f"            dims一致率={r['dims_agree']} | ⚠️leak_rate={r['leak_rate']}")
            for lr in r["leak_reasons"]:
                print(f"            leak证据: {lr}")
        print()


if __name__ == "__main__":
    main()
