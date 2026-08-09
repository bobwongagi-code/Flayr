"""Hard gate for the frozen evaluation execution order."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any


VERIFICATION_ORDER = (
    "fixture",
    "offline_replay",
    "fake_provider",
    "ordinary_sample",
    "boundary_sample",
)
PRODUCTION_STAGE = "production"
MARKER_SCHEMA_VERSION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_PROOF_FIELDS = ("source_commit", "scope_sha256", "evidence_sha256", "command_sha256")


class VerificationOrderError(ValueError):
    """Raised when an evaluation stage is attempted out of order."""


def assert_verification_order(root: Path, stage: str) -> None:
    normalized = str(stage or "").strip().lower()
    if normalized not in VERIFICATION_ORDER:
        raise VerificationOrderError(f"unknown verification stage: {stage}")
    root = root.expanduser().resolve()
    index = VERIFICATION_ORDER.index(normalized)
    for prerequisite in VERIFICATION_ORDER[:index]:
        marker = root / f"{prerequisite}.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationOrderError(
                f"verification stage {normalized} requires passed marker {marker}"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != MARKER_SCHEMA_VERSION
            or value.get("stage") != prerequisite
            or value.get("status") != "passed"
            or not _valid_marker_proof(value)
        ):
            raise VerificationOrderError(
                f"verification prerequisite {prerequisite} is not passed"
            )


def write_verification_marker(root: Path, stage: str, *, metadata: dict[str, Any] | None = None) -> Path:
    normalized = str(stage or "").strip().lower()
    if normalized not in VERIFICATION_ORDER:
        raise VerificationOrderError(f"unknown verification stage: {stage}")
    if not isinstance(metadata, dict) or not _valid_proof(metadata.get("proof")):
        raise VerificationOrderError(
            "verification marker requires proof: source_commit, scope_sha256, "
            "evidence_sha256 and command_sha256"
        )
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / f"{normalized}.json"
    proof = dict(metadata["proof"])
    proof["proof_sha256"] = _proof_sha256(proof)
    marker.write_text(
        json.dumps(
            {
                **(metadata or {}),
                "proof": proof,
                # Descriptive metadata cannot overwrite the fields that make
                # a verification marker authoritative.
                "schema_version": MARKER_SCHEMA_VERSION,
                "stage": normalized,
                "status": "passed",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return marker


def _valid_proof(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    source_commit = str(value.get("source_commit") or "").strip().lower()
    if not _COMMIT_RE.fullmatch(source_commit):
        return False
    for field in ("scope_sha256", "evidence_sha256", "command_sha256"):
        digest = str(value.get(field) or "").strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            return False
    return True


def _proof_sha256(proof: dict[str, Any]) -> str:
    body = {key: proof[key] for key in _REQUIRED_PROOF_FIELDS}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _valid_marker_proof(marker: dict[str, Any]) -> bool:
    proof = marker.get("proof")
    return _valid_proof(proof) and str(proof.get("proof_sha256") or "").lower() == _proof_sha256(proof)
