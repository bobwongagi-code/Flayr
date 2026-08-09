#!/usr/bin/env python3
"""Replay Canonical -> Finalized without reading videos or calling providers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from flayr_core.offline_replay import replay_canonical_finalization
from flayr_core.utils import write_json


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid replay JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"replay JSON must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise SystemExit(f"replay output directory must be new or empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_run", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    source = args.source_run.expanduser().resolve()
    canonical_path = source / "validated_normalized_result.json"
    provenance_path = source / "final_derived_result.json"
    analysis_path = source / "analysis_replay_context.json"
    input_path = source / "analysis_input.md"
    for path in (canonical_path, provenance_path, analysis_path, input_path):
        if not path.is_file():
            raise SystemExit(f"required replay artifact is missing: {path}")
    provenance_result = _read_object(provenance_path)
    provenance = provenance_result.get("postprocess_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema_version") != 2:
        raise SystemExit("replay provenance is missing or outdated; the source run must be regenerated")
    canonical_sha = _sha256(canonical_path)
    if provenance.get("validated_normalized_sha256") != canonical_sha:
        raise SystemExit("replay provenance does not match validated_normalized_result.json")
    if provenance.get("replay_context") != analysis_path.name:
        raise SystemExit("replay provenance points to a different analysis context")
    if provenance.get("replay_context_sha256") != _sha256(analysis_path):
        raise SystemExit("replay analysis context hash mismatch")
    if provenance.get("analysis_input_sha256") != _sha256(input_path):
        raise SystemExit("replay analysis input hash mismatch")
    output = _prepare_output(args.output_dir)

    finalized = replay_canonical_finalization(
        _read_object(canonical_path),
        _read_object(analysis_path),
        input_path.read_text(encoding="utf-8"),
        output_dir=output,
    )
    result_path = output / "analysis_result.json"
    write_json(result_path, finalized)
    write_json(
        output / "_REPLAY.json",
        {
            "schema_version": 1,
            "mode": "canonical_finalization_only",
            "provider_calls": 0,
            "source_run": str(source),
            "source_canonical_sha256": canonical_sha,
            "source_provenance_sha256": _sha256(provenance_path),
            "source_replay_context_sha256": _sha256(analysis_path),
            "source_analysis_input_sha256": _sha256(input_path),
            "result_sha256": _sha256(result_path),
            "stage2_pipeline_status": finalized.get("stage2_pipeline_status"),
        },
    )
    return 0 if finalized.get("stage2_pipeline_status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
