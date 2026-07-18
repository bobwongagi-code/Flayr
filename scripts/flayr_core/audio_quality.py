"""Local, deterministic audio quality checks for extracted video audio."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .utils import run_command


_LOUDNESS_JSON_RE = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)
_SILENCE_DURATION_RE = re.compile(r"silence_duration:\s*([0-9.]+)")


def analyze_audio_quality(audio_path: Path | None, duration_seconds: Any = None) -> dict[str, Any]:
    """Return technical QC facts only; do not infer tone, BGM fit, or sales impact."""
    result: dict[str, Any] = {
        "status": "unavailable",
        "metrics": {
            "integrated_lufs": None,
            "true_peak_dbfs": None,
            "loudness_range_lu": None,
            "silence_ratio": None,
        },
        "hard_issues": [],
        "scope": "technical_qc_only",
        "commercial_audio_assessment": "observation_only",
    }
    if not audio_path or not Path(audio_path).is_file():
        result["hard_issues"].append(
            {"code": "audio_missing", "level": "blocking", "message": "未提取到可用音轨。"}
        )
        return result

    command = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio_path),
        "-af", "silencedetect=noise=-50dB:d=0.5,loudnorm=print_format=json",
        "-f", "null", "-",
    ]
    completed = run_command(command)
    diagnostic = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        result["hard_issues"].append(
            {"code": "audio_unreadable", "level": "blocking", "message": "音轨无法完成本地质量检测。"}
        )
        return result

    loudness = _parse_loudness(diagnostic)
    integrated = _finite_float(loudness.get("input_i"))
    true_peak = _finite_float(loudness.get("input_tp"))
    loudness_range = _finite_float(loudness.get("input_lra"))
    duration = _finite_float(duration_seconds)
    silence_seconds = sum(float(value) for value in _SILENCE_DURATION_RE.findall(diagnostic))
    silence_ratio = min(1.0, silence_seconds / duration) if duration and duration > 0 else None

    result["metrics"] = {
        "integrated_lufs": _rounded(integrated),
        "true_peak_dbfs": _rounded(true_peak),
        "loudness_range_lu": _rounded(loudness_range),
        "silence_ratio": _rounded(silence_ratio, 3),
    }
    issues: list[dict[str, str]] = []
    if integrated is None:
        issues.append({"code": "audio_level_unavailable", "level": "warning", "message": "无法测得整体响度。"})
    elif integrated <= -35.0:
        issues.append({"code": "audio_too_quiet", "level": "warning", "message": "整体音量明显偏低，可能影响口播接收。"})
    if silence_ratio is not None and silence_ratio >= 0.65:
        issues.append({"code": "excessive_silence", "level": "warning", "message": "长静音占比较高，需确认是否为内容设计。"})
    if true_peak is not None and true_peak >= 0.5:
        issues.append({"code": "peak_overload_risk", "level": "warning", "message": "真峰值过高，存在削波或失真风险。"})
    result["hard_issues"] = issues
    result["status"] = "warning" if issues else "ok"
    return result


def _parse_loudness(text: str) -> dict[str, Any]:
    matches = _LOUDNESS_JSON_RE.findall(text)
    if not matches:
        return {}
    try:
        value = json.loads(matches[-1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None
