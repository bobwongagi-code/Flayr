"""Durable provider call artifacts and strict technical replay.

The live pipeline has several provider calls that are not Stage1/Stage2 group
calls. They still need the same identity and replay guarantees: a semantic
result must never be silently reused after its request changed, and a failed
call must remain observable after the run finishes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable

from ..utils import write_json


PROVIDER_ARTIFACT_SCHEMA_VERSION = 2
REQUIRED_PROVIDER_META_FIELDS = (
    "logical_request_id",
    "completion_attempts",
    "retry_reasons",
    "usage",
)
PROVIDER_VIDEO_ROLES = frozenset({"benchmark", "creator"})


class ProviderArtifactError(ValueError):
    """Raised when a provider artifact cannot be audited or replayed safely."""


class ProviderCallError(RuntimeError):
    """Raised after a failed live provider call has been persisted."""


def provider_role_replay_root(replay_from: Path, role: str) -> Path:
    """Resolve a published role directory without leaking random staging names into replay identity."""
    normalized = str(role or "").strip()
    if normalized not in PROVIDER_VIDEO_ROLES:
        raise ValueError(f"invalid provider replay role: {role}")
    return (Path(replay_from).expanduser().resolve() / normalized).resolve()


class ProviderReplayError(ProviderArtifactError):
    """Raised after strict technical replay rejects its source artifact."""


def initialize_provider_response_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Create the mandatory audit envelope before provider work starts."""
    if not isinstance(meta, dict):
        raise ProviderArtifactError("provider response metadata must be a mutable object")
    meta.setdefault("logical_request_id", uuid.uuid4().hex)
    meta.setdefault("completion_attempts", 0)
    meta.setdefault("retry_reasons", [])
    meta.setdefault("usage", {})
    return meta


def validated_provider_response_meta(
    meta: Any,
    *,
    completed: bool,
    execution_source: str | None = None,
) -> dict[str, Any]:
    """Return a defensive copy of mandatory request, retry, and usage metadata."""
    if not isinstance(meta, dict):
        raise ProviderArtifactError("provider response metadata must be an object")
    normalized = copy.deepcopy(meta)
    if execution_source:
        normalized["execution_source"] = str(execution_source)
    if normalized.get("completion_attempts", 0) == 0 and isinstance(
        normalized.get("transport_attempts"), int
    ):
        normalized["completion_attempts"] = max(0, int(normalized["transport_attempts"]))
    if not normalized.get("retry_reasons") and isinstance(
        normalized.get("transport_retry_reasons"), list
    ):
        normalized["retry_reasons"] = [
            str(item) for item in normalized["transport_retry_reasons"]
        ]
    missing = [field for field in REQUIRED_PROVIDER_META_FIELDS if field not in normalized]
    if missing:
        raise ProviderArtifactError(
            "provider response metadata missing required fields: " + ", ".join(missing)
        )
    request_id = str(normalized.get("logical_request_id") or "").strip()
    if not request_id:
        raise ProviderArtifactError("provider logical_request_id must not be empty")
    attempts = normalized.get("completion_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ProviderArtifactError("provider completion_attempts must be a non-negative integer")
    if completed and attempts < 1:
        raise ProviderArtifactError("completed provider metadata requires at least one attempt")
    retry_reasons = normalized.get("retry_reasons")
    if not isinstance(retry_reasons, list) or any(not isinstance(item, str) for item in retry_reasons):
        raise ProviderArtifactError("provider retry_reasons must be a list of strings")
    if not isinstance(normalized.get("usage"), dict):
        raise ProviderArtifactError("provider usage must be an object")
    normalized["logical_request_id"] = request_id
    return normalized


def failed_provider_response_meta(
    meta: dict[str, Any],
    *,
    execution_source: str,
) -> dict[str, Any]:
    """Finalize metadata when work fails before or during a provider request."""
    initialize_provider_response_meta(meta)
    if meta.get("completion_attempts", 0) == 0 and isinstance(meta.get("transport_attempts"), int):
        meta["completion_attempts"] = max(0, int(meta["transport_attempts"]))
    if not meta.get("retry_reasons") and isinstance(meta.get("transport_retry_reasons"), list):
        meta["retry_reasons"] = [str(item) for item in meta["transport_retry_reasons"]]
    return validated_provider_response_meta(meta, completed=False, execution_source=execution_source)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    if isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def provider_request_identity(
    *,
    call_kind: str,
    payload: Any,
    model: str,
    api_url: str,
) -> dict[str, str]:
    """Return the exact identity that must match for a technical replay."""
    return {
        "call_kind": str(call_kind or "").strip(),
        "payload_sha256": _sha256(payload),
        "model": str(model or "").strip(),
        "api_url": str(api_url or "").strip(),
    }


def completed_provider_artifact(
    *,
    call_kind: str,
    payload: Any,
    response: Any,
    model: str,
    api_url: str,
    response_meta: dict[str, Any],
    execution_source: str = "live",
) -> dict[str, Any]:
    response_copy = copy.deepcopy(response)
    return {
        "schema_version": PROVIDER_ARTIFACT_SCHEMA_VERSION,
        "status": "completed",
        "request_identity": provider_request_identity(
            call_kind=call_kind, payload=payload, model=model, api_url=api_url
        ),
        "response_meta": validated_provider_response_meta(
            response_meta,
            completed=True,
            execution_source=execution_source,
        ),
        "provider_response": response_copy,
        "response_sha256": _sha256(response_copy),
    }


def failed_provider_artifact(
    *,
    call_kind: str,
    payload: Any,
    model: str,
    api_url: str,
    error: str,
    response_meta: dict[str, Any],
    execution_source: str = "live",
) -> dict[str, Any]:
    return {
        "schema_version": PROVIDER_ARTIFACT_SCHEMA_VERSION,
        "status": "failed",
        "request_identity": provider_request_identity(
            call_kind=call_kind, payload=payload, model=model, api_url=api_url
        ),
        "response_meta": failed_provider_response_meta(
            response_meta,
            execution_source=execution_source,
        ),
        "error": str(error)[:1000],
    }


def write_provider_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, artifact)


def read_provider_artifact(path: Path) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderArtifactError(f"provider artifact unreadable: {path}") from exc
    if not isinstance(artifact, dict) or artifact.get("schema_version") != PROVIDER_ARTIFACT_SCHEMA_VERSION:
        raise ProviderArtifactError(f"provider artifact schema mismatch: {path}")
    if not isinstance(artifact.get("request_identity"), dict):
        raise ProviderArtifactError(f"provider artifact request identity missing: {path}")
    if not isinstance(artifact.get("response_meta"), dict):
        raise ProviderArtifactError(f"provider artifact response metadata missing: {path}")
    status = artifact.get("status")
    if status not in {"completed", "failed"}:
        raise ProviderArtifactError(f"provider artifact status invalid: {path}")
    validated_provider_response_meta(
        artifact["response_meta"],
        completed=status == "completed",
        execution_source=str(artifact["response_meta"].get("execution_source") or ""),
    )
    if status == "completed" and not {"provider_response", "response_sha256"}.issubset(artifact):
        raise ProviderArtifactError(f"provider artifact completed payload missing: {path}")
    if status == "failed" and not str(artifact.get("error") or "").strip():
        raise ProviderArtifactError(f"provider artifact failure reason missing: {path}")
    return artifact


def reusable_provider_response(
    artifact: dict[str, Any],
    *,
    call_kind: str,
    payload: Any,
    model: str,
    api_url: str,
) -> tuple[Any, dict[str, Any]]:
    expected = provider_request_identity(
        call_kind=call_kind, payload=payload, model=model, api_url=api_url
    )
    if artifact.get("status") != "completed":
        raise ProviderReplayError("provider replay requires a completed artifact")
    if artifact.get("request_identity") != expected:
        raise ProviderReplayError("provider replay request identity mismatch")
    response = artifact.get("provider_response")
    if "response_sha256" not in artifact or artifact["response_sha256"] != _sha256(response):
        raise ProviderReplayError("provider replay response hash mismatch")
    metadata = artifact.get("response_meta")
    return copy.deepcopy(response), validated_provider_response_meta(
        metadata,
        completed=True,
        execution_source=str(metadata.get("execution_source") or "") if isinstance(metadata, dict) else "",
    )


def provider_call_with_artifact(
    *,
    artifact_path: Path,
    replay_root: Path | None,
    call_kind: str,
    payload: Any,
    model: str,
    api_url: str,
    call: Callable[[], tuple[Any, dict[str, Any]]],
    response_meta: dict[str, Any],
) -> tuple[Any, dict[str, Any], str]:
    """Run or strictly replay a provider call and persist its outcome.

    ``replay_root`` is intentionally strict. Missing or mismatched artifacts
    fail instead of silently falling back to a new provider request.
    """
    initialize_provider_response_meta(response_meta)
    if replay_root is not None:
        replay_source_path = (replay_root / artifact_path.name).expanduser().resolve()
        output_path = artifact_path.expanduser().resolve()
        if replay_source_path == output_path:
            raise ProviderReplayError(
                "provider replay output must differ from the replay source; "
                "in-place replay could overwrite the source artifact"
            )
        try:
            source = read_provider_artifact(replay_source_path)
            response, metadata = reusable_provider_response(
                source,
                call_kind=call_kind,
                payload=payload,
                model=model,
                api_url=api_url,
            )
        except (Exception, SystemExit) as exc:
            # Strict replay must never fall back to a live request, but the
            # mismatch itself is still a durable, inspectable outcome.
            response_meta["execution_source"] = "technical_replay"
            write_provider_artifact(
                artifact_path,
                failed_provider_artifact(
                    call_kind=call_kind,
                    payload=payload,
                    model=model,
                    api_url=api_url,
                    error=f"technical replay rejected: {exc}",
                    response_meta=response_meta,
                    execution_source="technical_replay",
                ),
            )
            raise ProviderReplayError(f"technical replay rejected: {exc}") from exc
        replayed = completed_provider_artifact(
            call_kind=call_kind,
            payload=payload,
            response=response,
            model=model,
            api_url=api_url,
            response_meta=metadata,
            execution_source="technical_replay",
        )
        write_provider_artifact(artifact_path, replayed)
        metadata = validated_provider_response_meta(
            metadata,
            completed=True,
            execution_source="technical_replay",
        )
        return response, metadata, "technical_replay"

    try:
        response, metadata = call()
    except (Exception, SystemExit) as exc:
        error = str(exc).strip() or exc.__class__.__name__
        response_meta["execution_source"] = "live"
        write_provider_artifact(
            artifact_path,
            failed_provider_artifact(
                call_kind=call_kind,
                payload=payload,
                model=model,
                api_url=api_url,
                error=error,
                response_meta=response_meta,
                execution_source="live",
            ),
        )
        raise ProviderCallError(error) from exc
    if not isinstance(metadata, dict):
        metadata = {}
    merged_metadata = copy.deepcopy(response_meta)
    merged_metadata.update(metadata)
    if merged_metadata.get("completion_attempts", 0) == 0:
        # The wrapper observed one completed logical call even when a small
        # adapter has no separate completion-level retry loop.
        merged_metadata["completion_attempts"] = 1
    metadata = validated_provider_response_meta(
        merged_metadata,
        completed=True,
        execution_source="live",
    )
    write_provider_artifact(
        artifact_path,
        completed_provider_artifact(
            call_kind=call_kind,
            payload=payload,
            response=response,
            model=model,
            api_url=api_url,
            response_meta=metadata,
        ),
    )
    return response, metadata, "live"
