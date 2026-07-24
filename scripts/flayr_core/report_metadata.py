"""Stable metadata attached to every rendered report."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORT_SCHEMA_VERSION = 2
REPORT_VARIANT_FILES = {
    "bd_report.html": "bd-internal-v2",
    "creator_report.html": "creator-v2",
}


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


def extract_report_metadata(path: Path) -> dict[str, Any]:
    """Read the version record embedded in a generated audience report."""
    text = path.read_text(encoding="utf-8")
    marker = "var report = "
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"报告缺少运行数据：{path}")
    start += len(marker)
    end = text.find(";\n", start)
    if end < 0:
        raise ValueError(f"报告数据未闭合：{path}")
    payload = json.loads(text[start:end])
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        raise ValueError(f"报告缺少 metadata：{path}")
    if not isinstance(metadata.get("report_schema_version"), int):
        raise ValueError(f"报告 schema 版本无效：{path}")
    for field in ("template_version", "generated_by", "generator"):
        if not str(metadata.get(field) or "").strip():
            raise ValueError(f"报告 metadata 缺少 {field}：{path}")
    return dict(metadata)


def extract_variant_report_metadata(run_dir: Path, names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Return metadata for each audience report that must be published."""
    result: dict[str, dict[str, Any]] = {}
    for name in names:
        if name not in REPORT_VARIANT_FILES:
            continue
        metadata = extract_report_metadata(run_dir / name)
        if metadata.get("report_schema_version") != REPORT_SCHEMA_VERSION:
            raise ValueError(f"报告 schema 版本与当前协议不匹配：{name}")
        if metadata.get("template_version") != REPORT_VARIANT_FILES[name]:
            raise ValueError(f"报告模板版本与文件不匹配：{name}")
        result[name] = metadata
    return result
