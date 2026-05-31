"""Shared helpers for Flayr core modules."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return "（缺失）"
    text = path.read_text(encoding="utf-8").strip()
    return text or "（空）"
