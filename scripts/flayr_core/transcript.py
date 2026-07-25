"""Explicit current-generation transcript artifact resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def current_transcript_segments_path(info: dict[str, Any]) -> Path | None:
    """Return only the transcript segment artifact explicitly recorded for this video."""
    raw = str(info.get("transcript_segments_path") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    work_dir = str(info.get("work_dir") or "").strip()
    if work_dir:
        root = Path(work_dir).expanduser().resolve()
        try:
            candidate.resolve().relative_to(root)
        except ValueError:
            return None
    return candidate if candidate.is_file() else None
