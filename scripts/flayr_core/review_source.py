"""Bind a terminal degraded draft without publishing a success marker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .run_manifest import BASE_REQUIRED_ARTIFACTS, REPORT_VARIANT_ARTIFACTS
from .run_state import read_run_state
from .speech_attestation import has_verified_no_product_speech


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"审核来源不是 JSON 对象：{path.name}")
    return data


def degraded_source_fingerprint(root: Path) -> dict[str, Any]:
    """Validate draft readiness, then bind inputs for the human-review lifetime."""
    if (root / "_SUCCESS.json").exists():
        raise ValueError("存在无效成功标记，不能降级绕过完整性检查")
    analysis = _json(root / "analysis.json")
    state = read_run_state(root) or {}
    degraded = _json(root / "degraded_manifest.json")
    if (state.get("state") != "DEGRADED"
            or analysis.get("analysis_run_state") != "degraded"
            or degraded.get("analysis_run_state") != "degraded"):
        raise ValueError("仅允许终态 DEGRADED 的完整草稿人工接管")
    sources = _json(root / "final_derived_result.json").get("postprocess_provenance", {}).get("field_sources", {})
    if (sources.get("coverage") != "complete" or sources.get("unresolved_paths") or sources.get("truncated")):
        raise ValueError("草稿字段来源未完整绑定，不能人工接管")
    paths = [root / name for name in (
        *BASE_REQUIRED_ARTIFACTS, *REPORT_VARIANT_ARTIFACTS,
        "run_state.json", "degraded_manifest.json",
    )]
    videos = analysis.get("videos")
    if not isinstance(videos, dict) or not videos:
        raise ValueError("缺少草稿原视频来源")
    expected_roles = {"benchmark", "creator"} if analysis.get("mode") == "compare" else {"creator"}
    if set(videos) != expected_roles or analysis.get("mode") not in {"compare", "improve"}:
        raise ValueError("草稿模式与视频角色不完整")
    inputs = {}
    for role, info in videos.items():
        if not isinstance(info, dict):
            raise ValueError("原视频来源结构不完整")
        video = Path(str(info.get("path") or "")).expanduser().resolve()
        expected = (info.get("preprocess_fingerprint") or {}).get("source_video") or {}
        if (not video.is_file() or expected.get("size_bytes") != video.stat().st_size
                or expected.get("sha256") != _hash(video)):
            raise ValueError("原视频与预处理来源指纹不一致")
        inputs[role] = {"path": str(video), **expected}
        work = Path(str(info.get("work_dir") or "")).expanduser().resolve()
        if not work.is_relative_to(root) or not work.is_dir():
            raise ValueError("预处理目录不在当前运行目录内")
        preprocess = _json(work / "_preprocess.json")
        if preprocess.get("preprocess_fingerprint") != info.get("preprocess_fingerprint"):
            raise ValueError("预处理与分析的来源记录不一致")
        files = (preprocess.get("preprocess_artifacts") or {}).get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("预处理缺少产物哈希清单")
        for name, record in files.items():
            artifact = (work / name).resolve()
            if (not artifact.is_relative_to(work) or not artifact.is_file()
                    or not isinstance(record, dict)
                    or record.get("size_bytes") != artifact.stat().st_size
                    or record.get("sha256") != _hash(artifact)):
                raise ValueError("预处理产物与既有哈希清单不一致")
        if info.get("transcription_status") != "completed" and not has_verified_no_product_speech(info):
            raise ValueError("转写来源尚未完成核验")
        paths.append(work / "_preprocess.json")
        paths.append(root / f"video_facts_{role}.json")
        paths.extend(path for path in work.rglob("*") if path.is_file())
    # Include provider and handoff records; reviewed outputs are deliberately excluded.
    paths.extend(root.glob("stage1_provider_*.json"))
    paths.extend(root.glob("stage2_provider_*.json"))
    paths.extend(root.glob("stage_group_*.json"))
    paths.extend(root.glob("stage1_to_stage2_handoff.json"))
    hashes = {}
    for path in sorted(set(paths)):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"草稿来源产物缺失或越界：{path.name}")
        hashes[str(path.relative_to(root))] = _hash(resolved)
    return {
        "source_run_state": "DEGRADED",
        "analysis_sha256": hashes["analysis.json"],
        "review_inputs": hashes,
        "original_inputs": inputs,
    }
