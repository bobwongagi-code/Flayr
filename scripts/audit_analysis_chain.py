#!/usr/bin/env python3
"""Audit saved production artifacts without rerunning an LLM.

The command separates S4 hard-fact conflicts, S5 trust-state ambiguity, and
the Stage1 evidence-strength gate. It never writes an analysis result and does
not change severity, derive output, or promotion status.

Manifest format::

    {"samples": [{"sample_id": "are_xie", "run_dir": "/path/to/run"}]}
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.evaluation_chain import audit_analysis_chain  # noqa: E402
from flayr_core.report_metadata import current_code_commit  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402


def _read_manifest(path: Path) -> list[dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid manifest: {path}: {exc}") from exc
    rows = data.get("samples") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise SystemExit("manifest must contain a non-empty samples list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SystemExit(f"manifest sample {index} must be an object")
        sample_id = str(row.get("sample_id") or "").strip()
        raw_run_dir = str(row.get("run_dir") or "").strip()
        if not sample_id or not raw_run_dir:
            raise SystemExit(f"manifest sample {index} needs sample_id and run_dir")
        if sample_id in seen:
            raise SystemExit(f"manifest contains duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        run_dir = Path(raw_run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = path.parent / run_dir
        result.append({"sample_id": sample_id, "run_dir": str(run_dir.resolve())})
    return result


def _load_result(run_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = run_dir / "analysis_result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path}: {exc}"
    if not isinstance(result, dict):
        return None, f"{path}: root must be an object"
    return result, None


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    run_statuses = Counter(str(record["audit"].get("status") or "unknown") for record in records)
    strength_statuses = Counter(
        str(record["audit"].get("evidence_strength", {}).get("status") or "unknown")
        for record in records
    )
    s5_states = Counter(
        str(role.get("state") or "unknown")
        for record in records
        for role in record["audit"].get("s5", {}).get("roles", {}).values()
    )
    return {
        "runs": len(records),
        "run_statuses": dict(sorted(run_statuses.items())),
        "evidence_strength_statuses": dict(sorted(strength_statuses.items())),
        "s4_state_conflict_runs": sum(
            bool(record["audit"].get("summary", {}).get("s4_state_conflict")) for record in records
        ),
        "s4_state_conflict_roles": sum(
            int(record["audit"].get("s4", {}).get("state_conflict_count") or 0) for record in records
        ),
        "s4_evidence_error_count": sum(
            int(record["audit"].get("s4", {}).get("evidence_error_count") or 0) for record in records
        ),
        "s5_uncertain_roles": sum(
            int(record["audit"].get("summary", {}).get("s5_uncertain_roles") or 0) for record in records
        ),
        "s5_states": dict(sorted(s5_states.items())),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit S4/S5/evidence-strength paths in saved Flayr results.",
        allow_abbrev=False,
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    samples = _read_manifest(args.manifest.expanduser().resolve())
    records: list[dict[str, Any]] = []
    for sample in samples:
        run_dir = Path(sample["run_dir"])
        result, load_error = _load_result(run_dir)
        audit = (
            audit_analysis_chain(result)
            if result is not None
            else {
                "schema_version": 1,
                "status": "invalid_artifact",
                "errors": [load_error or "unknown artifact load error"],
            }
        )
        records.append(
            {
                "sample_id": sample["sample_id"],
                "run_dir": str(run_dir),
                "audit": audit,
            }
        )
    artifact = {
        "schema_version": 1,
        "source_commit": current_code_commit(),
        "input_role": "offline_production_artifact_audit",
        "promotion_eligible": False,
        "summary": _summarize(records),
        "records": records,
    }
    output = args.output.expanduser().resolve()
    write_json(output, artifact)
    print(json.dumps(artifact["summary"], ensure_ascii=False, indent=2))
    return 0 if set(artifact["summary"]["run_statuses"]) <= {"ok"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
