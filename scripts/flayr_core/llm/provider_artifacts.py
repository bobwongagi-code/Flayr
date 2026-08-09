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
from pathlib import Path
from typing import Any, Callable

from ..utils import write_json


PROVIDER_ARTIFACT_SCHEMA_VERSION = 1


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
    response_meta: dict[str, Any] | None = None,
    execution_source: str = "live",
) -> dict[str, Any]:
    response_copy = copy.deepcopy(response)
    return {
        "schema_version": PROVIDER_ARTIFACT_SCHEMA_VERSION,
        "status": "completed",
        "request_identity": provider_request_identity(
            call_kind=call_kind, payload=payload, model=model, api_url=api_url
        ),
        "response_meta": {
            **(copy.deepcopy(response_meta) if isinstance(response_meta, dict) else {}),
            "execution_source": execution_source,
        },
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
    response_meta: dict[str, Any] | None = None,
    execution_source: str = "live",
) -> dict[str, Any]:
    return {
        "schema_version": PROVIDER_ARTIFACT_SCHEMA_VERSION,
        "status": "failed",
        "request_identity": provider_request_identity(
            call_kind=call_kind, payload=payload, model=model, api_url=api_url
        ),
        "response_meta": {
            **(copy.deepcopy(response_meta) if isinstance(response_meta, dict) else {}),
            "execution_source": execution_source,
        },
        "error": str(error)[:1000],
    }


def write_provider_artifact(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, artifact)


def read_provider_artifact(path: Path) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"provider artifact unreadable: {path}") from exc
    if not isinstance(artifact, dict) or artifact.get("schema_version") != PROVIDER_ARTIFACT_SCHEMA_VERSION:
        raise ValueError(f"provider artifact schema mismatch: {path}")
    if not isinstance(artifact.get("request_identity"), dict):
        raise ValueError(f"provider artifact request identity missing: {path}")
    if not isinstance(artifact.get("response_meta"), dict):
        raise ValueError(f"provider artifact response metadata missing: {path}")
    status = artifact.get("status")
    if status not in {"completed", "failed"}:
        raise ValueError(f"provider artifact status invalid: {path}")
    if status == "completed" and not {"provider_response", "response_sha256"}.issubset(artifact):
        raise ValueError(f"provider artifact completed payload missing: {path}")
    if status == "failed" and not str(artifact.get("error") or "").strip():
        raise ValueError(f"provider artifact failure reason missing: {path}")
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
        raise ValueError("provider replay requires a completed artifact")
    if artifact.get("request_identity") != expected:
        raise ValueError("provider replay request identity mismatch")
    response = artifact.get("provider_response")
    if "response_sha256" not in artifact or artifact["response_sha256"] != _sha256(response):
        raise ValueError("provider replay response hash mismatch")
    metadata = artifact.get("response_meta")
    return copy.deepcopy(response), copy.deepcopy(metadata) if isinstance(metadata, dict) else {}


def provider_call_with_artifact(
    *,
    artifact_path: Path,
    replay_root: Path | None,
    call_kind: str,
    payload: Any,
    model: str,
    api_url: str,
    call: Callable[[], tuple[Any, dict[str, Any]]],
    response_meta: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any], str]:
    """Run or strictly replay a provider call and persist its outcome.

    ``replay_root`` is intentionally strict. Missing or mismatched artifacts
    fail instead of silently falling back to a new provider request.
    """
    if replay_root is not None:
        replay_source_path = (replay_root / artifact_path.name).expanduser().resolve()
        output_path = artifact_path.expanduser().resolve()
        if replay_source_path == output_path:
            raise ValueError(
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
            if isinstance(response_meta, dict):
                response_meta["execution_source"] = "technical_replay"
            write_provider_artifact(
                artifact_path,
                failed_provider_artifact(
                    call_kind=call_kind,
                    payload=payload,
                    model=model,
                    api_url=api_url,
                    error=f"technical replay rejected: {exc}",
                    response_meta={
                        **(copy.deepcopy(response_meta) if isinstance(response_meta, dict) else {}),
                        "execution_source": "technical_replay",
                    },
                    execution_source="technical_replay",
                ),
            )
            raise
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
        metadata = copy.deepcopy(metadata)
        metadata["execution_source"] = "technical_replay"
        return response, metadata, "technical_replay"

    try:
        response, metadata = call()
    except (Exception, SystemExit) as exc:
        error = str(exc).strip() or exc.__class__.__name__
        if isinstance(response_meta, dict):
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
        raise
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
    metadata = copy.deepcopy(metadata)
    metadata["execution_source"] = "live"
    return response, metadata, "live"
