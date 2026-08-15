#!/usr/bin/env python3
"""Build a non-destructive inventory for the legacy 16-sample GT.

The inventory never upgrades legacy labels into canonical ``human_gap`` or
``stage_relations``.  It makes ambiguity explicit so offline evaluation cannot
silently treat the old ``small`` bucket as a real small gap under the current
none/small/not-applicable semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STAGES = tuple(f"S{index}" for index in range(1, 7))
CANONICAL_GAPS = frozenset({"none", "small", "medium", "large", "uncertain", "na", "not_applicable"})


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _stage_status(label: dict[str, Any], stage: str) -> tuple[str | None, str | None]:
    statuses = label.get("stage_label_statuses")
    entry = statuses.get(stage) if isinstance(statuses, dict) else None
    if not isinstance(entry, dict):
        return None, None
    return str(entry.get("status") or "").strip() or None, str(entry.get("reason") or "").strip() or None


def classify_legacy_cell(label: dict[str, Any], stage: str) -> dict[str, Any]:
    canonical = label.get("human_gap")
    canonical_gap = str(canonical.get(stage) or "").strip().lower() if isinstance(canonical, dict) else ""
    if canonical_gap in CANONICAL_GAPS:
        return {
            "legacy_gap": None,
            "migration_status": "canonical_existing",
            "requires_expert_confirmation": False,
        }
    if canonical_gap:
        return {
            "legacy_gap": None,
            "canonical_gap": canonical_gap,
            "migration_status": "canonical_invalid",
            "requires_expert_confirmation": True,
            "ambiguity": "canonical human_gap is not a recognized value",
        }

    stages = label.get("stages")
    raw_gap = str(stages.get(stage) or "").strip().lower() if isinstance(stages, dict) else ""
    status, reason = _stage_status(label, stage)
    result: dict[str, Any] = {
        "legacy_gap": raw_gap or None,
        "migration_status": "legacy_missing",
        "requires_expert_confirmation": False,
    }
    if raw_gap == "small":
        result["migration_status"] = "legacy_ambiguous"
        result["requires_expert_confirmation"] = True
        result["ambiguity"] = "old small may mean current none or a real small gap"
    elif raw_gap in {"medium", "large"}:
        result["migration_status"] = "legacy_magnitude_only"
        result["requires_expert_confirmation"] = False
        result["limitation"] = "usable only for gap magnitude until direction is independently reviewed"
    elif raw_gap in {"na", "not_applicable"}:
        documented = status == "not_applicable" and bool(reason)
        result["migration_status"] = (
            "legacy_not_applicable_documented" if documented else "legacy_applicability_ambiguous"
        )
        result["requires_expert_confirmation"] = not documented
    elif not raw_gap:
        result["limitation"] = "no stage-level legacy label; exclude rather than request a synthetic migration"
    if status:
        result["legacy_stage_status"] = status
    if reason:
        result["legacy_stage_reason"] = reason
    return result


def build_inventory(labels: dict[str, Any]) -> dict[str, Any]:
    samples = labels.get("samples")
    if not isinstance(samples, dict):
        raise ValueError("labels.samples must be an object")
    inventory_samples: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    confirmation_cells: list[str] = []
    for sample_id, label in samples.items():
        if not isinstance(label, dict):
            continue
        cells: dict[str, Any] = {}
        for stage in STAGES:
            cell = classify_legacy_cell(label, stage)
            cells[stage] = cell
            counts[cell["migration_status"]] += 1
            if cell["requires_expert_confirmation"]:
                confirmation_cells.append(f"{sample_id}/{stage}")
        inventory_samples[sample_id] = {
            "partition": label.get("partition"),
            "cells": cells,
        }
    return {
        "schema_version": 1,
        "decision_scope": "legacy_gt_migration_inventory_only",
        "promotion_eligible": False,
        "source": "references/ground-truth-labels.json",
        "source_sha256": hashlib.sha256(
            json.dumps(labels, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "policy": {
            "does_not_modify_source_gt": True,
            "legacy_small_default": "legacy_ambiguous",
            "automatic_small_to_none_migration": False,
            "legacy_medium_large_use": "magnitude_only_until_direction_review",
            "unresolved_cells_are_excluded": True,
            "minimum_other_migration_spot_checks": 5,
            "on_any_interpretation_mismatch": "halt_entire_legacy_migration_batch",
        },
        "summary": {
            "samples": len(inventory_samples),
            "cells": len(inventory_samples) * len(STAGES),
            "status_counts": dict(sorted(counts.items())),
            "requires_expert_confirmation": len(confirmation_cells),
        },
        "confirmation_cells": confirmation_cells,
        "samples": inventory_samples,
    }


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the legacy GT migration inventory")
    parser.add_argument("--labels", type=Path, default=ROOT / "references/ground-truth-labels.json")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "references/legacy-gt-migration-inventory.json",
    )
    parser.add_argument("--check", action="store_true", help="fail when the existing output is stale")
    args = parser.parse_args()
    inventory = build_inventory(_read_json(args.labels))
    if args.check:
        if not args.output.exists() or _read_json(args.output) != inventory:
            raise SystemExit(f"legacy GT migration inventory is stale: {args.output}")
        print(f"legacy GT migration inventory verified: {args.output}")
        return 0
    _write_json_atomic(args.output, inventory)
    print(f"legacy GT migration inventory written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
