"""Shared identity primitives for durable provider artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def stable_sha256(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def identity_value(value: Any, key: str = "") -> Any:
    """Ignore run-local path labels while retaining provider-visible content."""
    if isinstance(value, dict):
        return {str(k): identity_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [identity_value(item, key) for item in value]
    if isinstance(value, str):
        if key in {"path", "work_dir", "run_dir", "source_path"} and value.startswith("/"):
            return "<local-path>"
        return re.sub(
            r"((?:本地路径|原视频|本地文件)[:：])[^\n，。]+",
            r"\1<local-path>",
            value,
        )
    return value
