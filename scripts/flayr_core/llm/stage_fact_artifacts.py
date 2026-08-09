"""Durable Stage1 provider artifacts and exact replay validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_identity import identity_value as _identity_value
from .artifact_identity import stable_sha256 as _stable_sha256
from .provider_artifacts import (
    ProviderArtifactError,
    failed_provider_response_meta,
    validated_provider_response_meta,
)


STAGE_FACT_ARTIFACT_SCHEMA_VERSION = 2


class StageFactArtifactError(ValueError):
    """Raised when a Stage1 provider artifact cannot be safely replayed."""


def stage_fact_artifact_path(
    root: Path,
    role: str,
    phase: str,
    group: Sequence[str] | None = None,
) -> Path:
    role_token = str(role or "").strip().lower()
    phase_token = str(phase or "").strip().upper()
    if role_token not in {"benchmark", "creator"}:
        raise StageFactArtifactError(f"invalid Stage1 role: {role!r}")
    if phase_token not in {"A", "B", "C"}:
        raise StageFactArtifactError(f"invalid Stage1 phase: {phase!r}")
    suffix = ""
    if group:
        labels = [str(item).strip().upper() for item in group if str(item).strip()]
        if not labels:
            raise StageFactArtifactError("Stage1 artifact group must not be empty")
        suffix = "_" + "_".join(labels)
    return root / f"stage1_provider_{role_token}_{phase_token}{suffix}.json"


def request_identity(
    *,
    role: str,
    phase: str,
    payload: Mapping[str, Any],
    model: str,
    api_url: str,
    group: Sequence[str] | None = None,
) -> dict[str, Any]:
    identity = {
        "role": str(role or "").strip().lower(),
        "phase": str(phase or "").strip().upper(),
        "group": [str(item).strip().upper() for item in (group or ())],
        "model": str(model or "").strip(),
        "api_url": str(api_url or "").strip(),
        "payload_sha256": _stable_sha256(_identity_value(payload)),
    }
    identity["sha256"] = _stable_sha256(identity)
    return identity


def completed_stage_fact_artifact(
    *,
    role: str,
    phase: str,
    payload: Mapping[str, Any],
    response: Mapping[str, Any],
    model: str,
    api_url: str,
    group: Sequence[str] | None = None,
    response_meta: Mapping[str, Any],
    artifact_name: str = "",
) -> dict[str, Any]:
    identity = request_identity(
        role=role,
        phase=phase,
        payload=payload,
        model=model,
        api_url=api_url,
        group=group,
    )
    response_copy = copy.deepcopy(dict(response))
    return {
        "schema_version": STAGE_FACT_ARTIFACT_SCHEMA_VERSION,
        "status": "completed",
        "artifact_name": str(artifact_name or ""),
        "request_identity": identity,
        "response_sha256": _stable_sha256(response_copy),
        "provider_response": response_copy,
        "response_meta": validated_provider_response_meta(
            dict(response_meta), completed=True
        ),
    }


def failed_stage_fact_artifact(
    *,
    role: str,
    phase: str,
    payload: Mapping[str, Any],
    model: str,
    api_url: str,
    error: str,
    group: Sequence[str] | None = None,
    artifact_name: str = "",
    response_meta: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": STAGE_FACT_ARTIFACT_SCHEMA_VERSION,
        "status": "failed",
        "artifact_name": str(artifact_name or ""),
        "request_identity": request_identity(
            role=role,
            phase=phase,
            payload=payload,
            model=model,
            api_url=api_url,
            group=group,
        ),
        "error": str(error or "Stage1 provider request failed")[:1000],
        "response_meta": failed_provider_response_meta(
            dict(response_meta), execution_source=str(response_meta.get("execution_source") or "live")
        ),
    }


def reusable_stage_fact_response(
    artifact: Mapping[str, Any],
    *,
    role: str,
    phase: str,
    payload: Mapping[str, Any],
    model: str,
    api_url: str,
    group: Sequence[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(artifact, Mapping):
        raise StageFactArtifactError("Stage1 artifact must be an object")
    if artifact.get("schema_version") != STAGE_FACT_ARTIFACT_SCHEMA_VERSION:
        raise StageFactArtifactError("Stage1 artifact schema version mismatch")
    if artifact.get("status") != "completed":
        raise StageFactArtifactError("Stage1 artifact is not completed")
    expected = request_identity(
        role=role,
        phase=phase,
        payload=payload,
        model=model,
        api_url=api_url,
        group=group,
    )
    if artifact.get("request_identity") != expected:
        raise StageFactArtifactError("Stage1 request identity mismatch")
    response = artifact.get("provider_response")
    if not isinstance(response, dict):
        raise StageFactArtifactError("Stage1 provider response is missing")
    if artifact.get("response_sha256") != _stable_sha256(response):
        raise StageFactArtifactError("Stage1 provider response hash mismatch")
    meta = artifact.get("response_meta")
    try:
        validated_meta = validated_provider_response_meta(meta, completed=True)
    except ProviderArtifactError as exc:
        raise StageFactArtifactError(f"Stage1 provider metadata invalid: {exc}") from exc
    return copy.deepcopy(response), validated_meta


def read_stage_fact_artifact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageFactArtifactError(f"cannot read Stage1 artifact: {path}") from exc
    if not isinstance(value, dict):
        raise StageFactArtifactError(f"Stage1 artifact must be an object: {path}")
    if value.get("schema_version") != STAGE_FACT_ARTIFACT_SCHEMA_VERSION:
        raise StageFactArtifactError(f"Stage1 artifact schema version mismatch: {path}")
    status = value.get("status")
    if status not in {"completed", "failed"}:
        raise StageFactArtifactError(f"Stage1 artifact status invalid: {path}")
    try:
        validated_provider_response_meta(
            value.get("response_meta"), completed=status == "completed"
        )
    except ProviderArtifactError as exc:
        raise StageFactArtifactError(f"Stage1 provider metadata invalid: {exc}") from exc
    return value
