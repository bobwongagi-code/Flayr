"""Completion manifest for a trustworthy Flayr run.

The presence of ``analysis.json`` is not a completion signal: that file is
written before report generation and can survive a failed or interrupted run.
This module defines the final, atomic marker used by the batch runner.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .utils import write_json


SUCCESS_MANIFEST_NAME = "_SUCCESS.json"
SUCCESS_MANIFEST_SCHEMA_VERSION = 1
REQUIRED_ARTIFACTS = (
    "analysis.json",
    "report.html",
    "raw_model_response.json",
    "validated_normalized_result.json",
    "final_derived_result.json",
    "postprocess_change_log.json",
)


def command_digest(argv: list[str]) -> str:
    """Stable digest for CLI inputs that affect a run but are not media files."""
    return hashlib.sha256(
        json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_metadata(path: Path) -> dict[str, Any]:
    return {"size_bytes": path.stat().st_size, "sha256": _sha256_file(path)}


def _has_complete_field_sources(path: Path) -> bool:
    """Require every final leaf to be attributable before publishing success."""
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(result, dict):
        return False
    provenance = result.get("postprocess_provenance")
    if not isinstance(provenance, dict):
        return False
    sources = provenance.get("field_sources")
    return (
        isinstance(sources, dict)
        and sources.get("coverage") == "complete"
        and not list(sources.get("unresolved_paths") or [])
        and not bool(sources.get("truncated"))
    )


def _input_metadata(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
    }


def build_success_manifest(
    run_dir: Path,
    inputs: Mapping[str, Path],
    analysis: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a manifest only for a fully completed analysis and report."""
    if str(analysis.get("analysis_run_state") or "") != "completed":
        raise ValueError("only analysis_run_state=completed can publish a success manifest")
    artifacts: dict[str, dict[str, Any]] = {}
    for relative in REQUIRED_ARTIFACTS:
        candidate = run_dir / relative
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        artifacts[relative] = _artifact_metadata(candidate)
    if not _has_complete_field_sources(run_dir / "final_derived_result.json"):
        raise ValueError("final_derived_result.json must contain complete field source coverage")
    return {
        "schema_version": SUCCESS_MANIFEST_SCHEMA_VERSION,
        "status": "completed",
        "analysis_run_state": "completed",
        "required_artifacts": list(REQUIRED_ARTIFACTS),
        "inputs": {name: _input_metadata(path) for name, path in sorted(inputs.items())},
        "artifacts": artifacts,
        "provenance": dict(provenance or {}),
    }


def write_success_manifest(
    run_dir: Path,
    inputs: Mapping[str, Path],
    analysis: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically publish the final completion marker as the last artifact."""
    path = run_dir / SUCCESS_MANIFEST_NAME
    write_json(path, build_success_manifest(run_dir, inputs, analysis, provenance))
    return path


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _valid_file_metadata(path: Path, metadata: Any) -> bool:
    if not isinstance(metadata, dict) or not path.is_file():
        return False
    try:
        expected_size = int(metadata.get("size_bytes"))
    except (TypeError, ValueError):
        return False
    expected_hash = str(metadata.get("sha256") or "")
    if expected_size < 0 or len(expected_hash) != 64:
        return False
    if path.stat().st_size != expected_size:
        return False
    try:
        return _sha256_file(path) == expected_hash
    except OSError:
        return False


def validate_success_manifest(
    run_dir: Path,
    expected_inputs: Mapping[str, Path] | None = None,
    expected_provenance: Mapping[str, Any] | None = None,
) -> bool:
    """Validate completion, input identity and required artifact hashes."""
    root = run_dir.expanduser().resolve()
    manifest_path = root / SUCCESS_MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("schema_version") != SUCCESS_MANIFEST_SCHEMA_VERSION:
        return False
    if manifest.get("status") != "completed" or manifest.get("analysis_run_state") != "completed":
        return False
    required = manifest.get("required_artifacts")
    if required != list(REQUIRED_ARTIFACTS):
        return False
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    for relative in REQUIRED_ARTIFACTS:
        candidate = (root / relative).resolve()
        if not _path_is_under(candidate, root) or not _valid_file_metadata(candidate, artifacts.get(relative)):
            return False
    if not _has_complete_field_sources(root / "final_derived_result.json"):
        return False
    try:
        analysis = json.loads((root / "analysis.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(analysis, dict) or analysis.get("analysis_run_state") != "completed":
        return False

    recorded_provenance = manifest.get("provenance")
    if not isinstance(recorded_provenance, dict):
        return False
    for key, expected in (expected_provenance or {}).items():
        if recorded_provenance.get(key) != expected:
            return False

    recorded_inputs = manifest.get("inputs")
    if not isinstance(recorded_inputs, dict):
        return False
    for name, expected in (expected_inputs or {}).items():
        try:
            expected_meta = _input_metadata(expected)
        except (OSError, ValueError):
            return False
        if recorded_inputs.get(name) != expected_meta:
            return False
    return True
