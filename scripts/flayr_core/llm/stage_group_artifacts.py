"""Durable Stage2 provider responses for deterministic replay and resume.

The provider response is preserved before any semantic projection.  Reuse is
allowed only when the complete request identity matches, so a prompt, model,
endpoint, Stage1 ledger, or comparison-contract change becomes a semantic
rerun instead of silently mixing results from different runs.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


STAGE_GROUP_ARTIFACT_SCHEMA_VERSION = 1


class StageGroupArtifactError(ValueError):
    """Raised when a saved provider response is not safe to replay."""


def _stable_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stage_group_label(group: Sequence[str]) -> str:
    normalized = tuple(str(item).strip().upper() for item in group if str(item).strip())
    if not normalized:
        raise StageGroupArtifactError("stage group must not be empty")
    return "_".join(normalized)


def stage_group_artifact_path(root: Path, group: Sequence[str]) -> Path:
    return root / f"stage2_provider_{stage_group_label(group)}.json"


def request_identity(
    *,
    group: Sequence[str],
    payload: Mapping[str, Any],
    model: str,
    api_url: str,
) -> dict[str, Any]:
    """Build the complete identity required for a technical replay."""
    identity = {
        "group": [str(item).strip().upper() for item in group],
        "model": str(model or "").strip(),
        "api_url": str(api_url or "").strip(),
        "payload_sha256": _stable_sha256(payload),
    }
    identity["sha256"] = _stable_sha256(identity)
    return identity


def completed_stage_group_artifact(
    *,
    group: Sequence[str],
    payload: Mapping[str, Any],
    response: Mapping[str, Any],
    model: str,
    api_url: str,
) -> dict[str, Any]:
    identity = request_identity(
        group=group,
        payload=payload,
        model=model,
        api_url=api_url,
    )
    response_copy = copy.deepcopy(dict(response))
    return {
        "schema_version": STAGE_GROUP_ARTIFACT_SCHEMA_VERSION,
        "status": "completed",
        "request_identity": identity,
        "response_sha256": _stable_sha256(response_copy),
        "provider_response": response_copy,
    }


def failed_stage_group_artifact(
    *,
    group: Sequence[str],
    payload: Mapping[str, Any],
    model: str,
    api_url: str,
    error: str,
) -> dict[str, Any]:
    return {
        "schema_version": STAGE_GROUP_ARTIFACT_SCHEMA_VERSION,
        "status": "failed",
        "request_identity": request_identity(
            group=group,
            payload=payload,
            model=model,
            api_url=api_url,
        ),
        "error": str(error)[:1000],
    }


def reusable_stage_group_response(
    artifact: Mapping[str, Any],
    *,
    group: Sequence[str],
    payload: Mapping[str, Any],
    model: str,
    api_url: str,
) -> dict[str, Any]:
    """Return a replayable response or reject the artifact without fallback."""
    if artifact.get("schema_version") != STAGE_GROUP_ARTIFACT_SCHEMA_VERSION:
        raise StageGroupArtifactError("stage group artifact schema version mismatch")
    if artifact.get("status") != "completed":
        raise StageGroupArtifactError("stage group artifact is not completed")
    expected_identity = request_identity(
        group=group,
        payload=payload,
        model=model,
        api_url=api_url,
    )
    if artifact.get("request_identity") != expected_identity:
        raise StageGroupArtifactError("stage group request identity mismatch")
    response = artifact.get("provider_response")
    if not isinstance(response, Mapping):
        raise StageGroupArtifactError("stage group provider response is missing")
    response_copy = copy.deepcopy(dict(response))
    if artifact.get("response_sha256") != _stable_sha256(response_copy):
        raise StageGroupArtifactError("stage group provider response hash mismatch")
    return response_copy


def read_stage_group_artifact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageGroupArtifactError(f"stage group artifact is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise StageGroupArtifactError("stage group artifact must be a JSON object")
    return value
