"""Stable metadata attached to every rendered report."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_SCHEMA_VERSION = 2


def current_code_commit() -> str:
    """Return the short source revision used to render the report."""
    configured = os.environ.get("FLAYR_CODE_COMMIT", "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def build_report_metadata(template_version: str, generator: str) -> dict[str, Any]:
    """Build metadata without mixing renderer details into semantic fields."""
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "template_version": template_version,
        "generated_by": current_code_commit(),
        "generator": generator,
    }
