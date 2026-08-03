"""Explicit current-generation transcript artifact resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import resolve_artifact_path


def current_transcript_segments_path(info: dict[str, Any]) -> Path | None:
    """Return only the transcript segment artifact explicitly recorded for this video."""
    raw = str(info.get("transcript_segments_path") or "").strip()
    if not raw:
        return None
    work_dir = str(info.get("work_dir") or "").strip()
    return resolve_artifact_path(
        info,
        raw,
        require_file=True,
        require_root=bool(work_dir),
    )
