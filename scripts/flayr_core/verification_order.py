"""Hard gate for the frozen evaluation execution order."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VERIFICATION_ORDER = (
    "fixture",
    "offline_replay",
    "fake_provider",
    "ordinary_sample",
    "boundary_sample",
)


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
            or value.get("schema_version") != 1
            or value.get("stage") != prerequisite
            or value.get("status") != "passed"
        ):
            raise VerificationOrderError(
                f"verification prerequisite {prerequisite} is not passed"
            )


def write_verification_marker(root: Path, stage: str, *, metadata: dict[str, Any] | None = None) -> Path:
    normalized = str(stage or "").strip().lower()
    if normalized not in VERIFICATION_ORDER:
        raise VerificationOrderError(f"unknown verification stage: {stage}")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / f"{normalized}.json"
    marker.write_text(
        json.dumps(
            {
                **(metadata or {}),
                # Descriptive metadata cannot overwrite the fields that make
                # a verification marker authoritative.
                "schema_version": 1,
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
