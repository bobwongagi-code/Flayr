"""Shared helpers for Flayr core modules."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_COMMAND_TIMEOUT_SECONDS = 900


def run_command(
    command: list[str],
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """运行外部工具；超时转为普通失败，交由调用方记录或降级。"""
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_text(exc.stdout)
        stderr = _timeout_text(exc.stderr)
        detail = f"command timed out after {timeout_seconds}s"
        return subprocess.CompletedProcess(command, 124, stdout, f"{stderr}\n{detail}".strip())


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, content: str) -> None:
    write_bytes(path, content.encode("utf-8"))


def write_bytes(path: Path, content: bytes) -> None:
    """在同目录临时文件写完并替换，避免中断后留下半份可见产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return "（缺失）"
    text = path.read_text(encoding="utf-8").strip()
    return text or "（空）"
