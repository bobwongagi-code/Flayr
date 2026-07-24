"""Creator-facing report rendering.

The creator report is a second rendering rule over the same normalized
analysis result. It intentionally does not expose internal severity, GMV
labels, or raw audit fields.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .artifacts import format_seconds
from .report import (
    REPORT_MAX_EMBEDDED_BYTES,
    ReportAssetContext,
    evidence_quotes,
    referenced_evidence_units,
    select_referenced_frames,
    stage_display_names,
)
from .resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits
from .report_metadata import build_report_metadata
from .semantic_model import SemanticAnalysis
from .utils import write_text


ROOT = Path(__file__).resolve().parents[2]
CREATOR_REPORT_TEMPLATE = ROOT / "assets" / "creator_report.html"
CREATOR_REPORT_NAME = "creator_report.html"

_STAGE_CODE_RE = re.compile(r"\bS([1-6])\b", re.IGNORECASE)
_PLACEHOLDER_PREFIXES = ("待基于", "待人工", "待补充", "（LLM 未填写")
_FORBIDDEN_CREATOR_PHRASES = (
    "整体表现不错",
    "还有较大提升空间",
    "增强吸引力",
    "优化节奏",
    "强化转化",
    "更有网感",
    "完播率可能大幅提升",
    "转化可能跳一档",
)


def write_creator_report(
    run_dir: Path,
    analysis: dict[str, Any],
    *,
    budget: ResourceBudget | None = None,
) -> Path:
    """Write the prototype-backed creator report for one completed analysis."""
    template = CREATOR_REPORT_TEMPLATE.read_text(encoding="utf-8")
    assets = ReportAssetContext(
        run_dir,
        max_embedded_bytes=budget.limits.max_report_bytes if budget is not None else REPORT_MAX_EMBEDDED_BYTES,
    )
    payload = build_creator_report_data(analysis, assets)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # Keep model text inside the script data block even when it contains HTML.
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    if "{{creator_report_data}}" not in template:
        raise ValueError("creator report template is missing creator_report_data token")
    report = template.replace("{{creator_report_data}}", serialized)

    report_bytes = report.encode("utf-8")
    if budget is not None:
        budget.reserve_report(len(report_bytes))
    elif len(report_bytes) > ResourceLimits().max_report_bytes:
        raise ResourceBudgetExceeded(
            f"creator report exceeds max_report_bytes={ResourceLimits().max_report_bytes}: "
            f"{len(report_bytes)} bytes"
        )
    report_path = run_dir / CREATOR_REPORT_NAME
    write_text(report_path, report)
    return report_path


def build_creator_report_data(
    analysis: dict[str, Any],
    assets: ReportAssetContext,
) -> dict[str, Any]:
    """Project semantic analysis into the creator report's public vocabulary."""
    semantic = SemanticAnalysis.from_mapping(analysis)
    product = semantic.product
    videos = semantic.videos
    creator_info = _as_dict(videos.get("creator"))
    creator_understanding = semantic.side("creator")

    raw_stages = [stage.data for stage in semantic.stages]
    low_confidence_codes = _low_confidence_codes(semantic.get("low_confidence_stages"))
    stage_by_code = {
        _stage_code(stage.get("stage"), index): stage
        for index, stage in enumerate(raw_stages, start=1)
    }
    experiments = _build_experiments(
        semantic,
        stage_by_code,
        creator_understanding,
    )
    experiments_by_id = {str(item.get("id")): item for item in experiments}
    experiments_by_stage = {
        _stage_code(item.get("targetStage"), index): item
        for index, item in enumerate(experiments, start=1)
        if item.get("targetStage")
    }
    top_experiment = experiments[0] if experiments else None

    stages = []
    for index, stage in enumerate(raw_stages, start=1):
        code, name = stage_display_names(stage.get("stage", ""), index)
        creator_range = _safe_text(stage.get("creator_time_range"))
        units = referenced_evidence_units(stage.get("creator_evidence_ids"), creator_understanding)
        observation = _stage_observation(stage, units)
        insufficient = bool(stage.get("insufficient_evidence")) or code in low_confidence_codes
        if not observation and not units:
            insufficient = True
        linked_id = _linked_experiment_id(stage, code, experiments, experiments_by_stage)
        linked = experiments_by_id.get(linked_id)
        if linked_id and linked_id not in {item.get("id") for item in experiments}:
            linked_id = ""

        frames = select_referenced_frames(creator_info, units, creator_range)
        frame = _frame_payload(frames[0] if frames else None, assets, name)
        local_quote, translated_quote = evidence_quotes(units)
        quote = _first_text(local_quote, stage.get("creator_quote"), translated_quote, stage.get("creator_quote_zh"))
        quote_zh = _first_text(stage.get("creator_quote_zh"), translated_quote)
        frame_ts = creator_range or (format_seconds(frames[0].get("timestamp_seconds")) if frames else "")

        reference = _reference_payload(stage, linked)
        status, status_label = _stage_status(
            stage,
            linked_id,
            experiments,
            insufficient,
        )
        confidence = _stage_confidence(stage, insufficient)
        stages.append(
            {
                "code": code,
                "name": name,
                "status": status,
                "statusLabel": status_label,
                "observation": observation,
                "frameTs": frame_ts,
                "frame": frame,
                "quote": quote,
                "quoteZh": quote_zh,
                "reference": reference,
                "referenceNote": "暂未提供同类参考。" if not reference else "",
                "linked": linked_id,
                "confidence": confidence,
            }
        )

    highlights = _build_highlights(semantic.creator_context)
    return {
        "brand": "Flayr · 心译复盘",
        "metadata": build_report_metadata("creator-v2", "flayr_core.creator_report"),
        "title": "这条视频，我们一起复盘",
        "context": _context_line(product),
        "intent": _first_text(
            semantic.creator_context.get("content_intent"),
            semantic.creator_context.get("creator_content_intent"),
            creator_understanding.get("content_intent"),
            creator_understanding.get("content_summary"),
            semantic.get("one_line_summary"),
            semantic.get("executive_summary"),
        )
        or "目前还无法判断这条视频正在尝试完成什么。",
        "highlights": highlights,
        "topExperiment": top_experiment,
        "experiments": experiments,
        "stages": stages,
        "continuity": _build_continuity(semantic.creator_context.get("continuity_record")),
        "keep": _build_keep(semantic.creator_context),
        "analysisState": _safe_text(semantic.get("analysis_run_state")),
        "degradedFlags": _as_list(semantic.get("degraded_flags")),
    }


def _build_experiments(
    semantic: SemanticAnalysis,
    stage_by_code: dict[str, dict[str, Any]],
    creator_understanding: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_candidates = semantic.creator_context.get("candidate_experiments")
    if not isinstance(raw_candidates, list):
        raw_candidates = [item.data for item in semantic.improvements]
    if not isinstance(raw_candidates, list):
        return []

    ordered = list(enumerate(raw_candidates))
    ordered.sort(key=lambda pair: _priority_key(pair[1], pair[0]))
    experiments: list[dict[str, Any]] = []
    for original_index, raw in ordered:
        if not isinstance(raw, dict):
            continue
        target_stage = _stage_code(
            _first_text(raw.get("target_stage"), raw.get("stage"), raw.get("stage_code")),
            0,
        )
        source_stage = stage_by_code.get(target_stage, {})
        source_units = referenced_evidence_units(
            source_stage.get("creator_evidence_ids"),
            creator_understanding,
        )
        observation = _first_text(
            raw.get("observation"),
            raw.get("problem"),
            source_stage.get("creator_key_message"),
            source_stage.get("creator_summary"),
        )
        action = _first_text(raw.get("action"), raw.get("suggestion"), _list_text(raw.get("actions")))
        title = _first_text(raw.get("title"), raw.get("name"))
        evidence_value = raw.get("evidence")
        has_evidence = bool(
            source_units
            or _safe_text(source_stage.get("creator_time_range"))
            or _safe_text(evidence_value)
            or _as_list(raw.get("creator_evidence_ids"))
            or _as_list(raw.get("benchmark_evidence_ids"))
        )
        if not title or not observation or not action or not has_evidence:
            continue

        experiment_id = _safe_text(raw.get("experiment_id")) or f"exp{len(experiments) + 1}"
        hypothesis = _first_text(
            raw.get("hypothesis"),
            raw.get("expected_effect"),
        ) or "目前还无法判断观众会如何反应。"
        demonstration = _first_text(
            raw.get("demonstration"),
            raw.get("creator_script_zh"),
            raw.get("creator_script"),
        )
        verification = _first_text(raw.get("verification"), raw.get("check"))
        if not verification:
            verification = f"是否完成“{action}”并在对应片段看见这项改动。"
        experiments.append(
            {
                "id": experiment_id,
                "title": title,
                "targetStage": target_stage,
                "observation": observation,
                "hypothesis": hypothesis,
                "action": action,
                "demonstration": demonstration,
                "verification": verification,
                "why": _first_text(raw.get("why"), hypothesis, observation),
                "confidence": _safe_text(raw.get("confidence")),
                "status": _safe_text(raw.get("status")) or "proposed",
                "sourceIndex": original_index,
            }
        )
        if len(experiments) >= 3:
            break
    return experiments


def _build_highlights(analysis: dict[str, Any]) -> list[dict[str, str]]:
    raw = analysis.get("highlights")
    if not isinstance(raw, list):
        raw = analysis.get("creator_highlights")
    if not isinstance(raw, list):
        return []
    highlights = []
    for item in raw:
        if isinstance(item, dict):
            text = _first_text(
                item.get("text"),
                item.get("observation"),
                item.get("description"),
                item.get("reason"),
            )
            timestamp = _first_text(item.get("timestamp"), item.get("time_range"), item.get("creator_time_range"))
        else:
            text = _safe_text(item)
            timestamp = ""
        if not text:
            continue
        highlights.append({"timestamp": timestamp, "text": text})
        if len(highlights) >= 2:
            break
    return highlights


def _build_continuity(value: Any) -> dict[str, Any] | None:
    record = _as_dict(value)
    if not record:
        return None
    if record.get("comparable") is False or record.get("comparability") in {"not_comparable", "incomparable"}:
        return {
            "title": "上次约定回顾",
            "notComparable": True,
            "note": "本次内容场景不同，暂不验证上次实验",
        }
    raw_items = record.get("items") or record.get("experiments")
    if not isinstance(raw_items, list):
        raw_items = []
        for key, label in (("adoption", "采用情况"), ("content", "内容变化"), ("outcome", "结果观察")):
            item = record.get(key)
            if item:
                raw_items.append({"text": label, "status": item})
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = _first_text(item.get("text"), item.get("experiment"), item.get("title"))
        if not text:
            continue
        status_class, status_label = _continuity_status(item.get("status", item.get("state")))
        items.append({"text": text, "statusClass": status_class, "statusLabel": status_label})
    return {
        "title": "上次约定回顾",
        "items": items,
    } if items else None


def _build_keep(creator_context: Mapping[str, Any]) -> str:
    value = creator_context.get("retained_points")
    if value is None:
        value = creator_context.get("creator_retain")
    if isinstance(value, list):
        return "、".join(item for item in (_safe_text(entry) for entry in value) if item)
    return _safe_text(value)


def _reference_payload(
    stage: dict[str, Any],
    experiment: dict[str, Any] | None,
) -> dict[str, str] | None:
    text = _first_text(
        stage.get("reference_relevance"),
        stage.get("reference_observation"),
        stage.get("benchmark_reference"),
        experiment.get("benchmark_reference") if experiment else "",
    )
    if not text:
        return None
    return {
        "ts": _first_text(stage.get("benchmark_time_range"), experiment.get("benchmark_time_range") if experiment else ""),
        "text": text,
    }


def _stage_observation(stage: dict[str, Any], units: list[dict[str, Any]]) -> str:
    observation = _first_text(
        stage.get("creator_observation"),
        stage.get("creator_key_message"),
        stage.get("creator_summary"),
    )
    if observation:
        return observation
    return _first_text(
        *(unit.get(key) for unit in units for key in ("information", "visual_fact", "subtitle_fact"))
    )


def _stage_confidence(stage: dict[str, Any], insufficient: bool) -> str:
    explicit = _first_text(
        stage.get("confidence_note"),
        stage.get("confidence_notes"),
        stage.get("evidence_confidence"),
    )
    if explicit:
        return explicit
    return "目前还无法判断，这一段证据不足。" if insufficient else ""


def _stage_status(
    stage: dict[str, Any],
    linked_id: str,
    experiments: list[dict[str, Any]],
    insufficient: bool,
) -> tuple[str, str]:
    if linked_id:
        top_id = experiments[0].get("id") if experiments else ""
        if linked_id == top_id:
            return "top", "本次最值得先试"
        return "boost", "可以继续强化"
    retained = stage.get("retain") or stage.get("creator_retain") or stage.get("creator_retention")
    if retained is True or str(retained or "").lower() in {"keep", "retain", "值得保留"}:
        return "keep", "值得保留"
    if insufficient:
        return "low", "暂不优先"
    return "", ""


def _linked_experiment_id(
    stage: dict[str, Any],
    code: str,
    experiments: list[dict[str, Any]],
    experiments_by_stage: dict[str, dict[str, Any]],
) -> str:
    explicit = _safe_text(stage.get("linked_experiment_id"))
    ids = {str(item.get("id")) for item in experiments}
    if explicit in ids:
        return explicit
    item = experiments_by_stage.get(code)
    return str(item.get("id")) if item else ""


def _frame_payload(frame: dict[str, Any] | None, assets: ReportAssetContext, stage_name: str) -> dict[str, str] | None:
    if not frame:
        return None
    src = assets.image_src(frame)
    if not src:
        return None
    return {
        "src": src,
        "timestamp": format_seconds(frame.get("timestamp_seconds")),
        "alt": f"{stage_name} 证据画面",
    }


def _context_line(product: dict[str, Any]) -> str:
    name = _safe_text(product.get("name"))
    if name:
        return f"基于这次的「{name}」视频和同类参考，我们一起看了完整版本。"
    return "基于这次上传的达人视频和同类参考，我们一起看了完整版本。"


def _continuity_status(value: Any) -> tuple[str, str]:
    if isinstance(value, dict):
        value = value.get("status") or value.get("state")
    raw = str(value or "").strip().lower()
    if raw in {"done", "adopted", "complete", "completed", "已采用"}:
        return "done", "已采用"
    if raw in {"partial", "partially_adopted", "部分采用"}:
        return "partial", "部分采用"
    return "none", "暂未采用"


def _low_confidence_codes(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {_stage_code(item, 0) for item in values if _stage_code(item, 0)}


def _priority_key(item: Any, original_index: int) -> tuple[int, int]:
    if isinstance(item, dict):
        try:
            return int(item.get("priority")), original_index
        except (TypeError, ValueError):
            pass
    return 999, original_index


def _stage_code(value: Any, index: int) -> str:
    match = _STAGE_CODE_RE.search(str(value or ""))
    if match:
        return f"S{match.group(1)}"
    return f"S{index}" if index else ""


def _first_text(*values: Any) -> str:
    for value in values:
        text = _safe_text(value)
        if text:
            return text
    return ""


def _list_text(value: Any) -> str:
    if not isinstance(value, list):
        return _safe_text(value)
    return "；".join(item for item in (_safe_text(entry) for entry in value) if item)


def _safe_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value or "").strip()
    if not text or text in {"无", "暂无", "未知", "null"} or text.startswith(_PLACEHOLDER_PREFIXES):
        return ""
    if any(phrase in text for phrase in _FORBIDDEN_CREATOR_PHRASES):
        return ""
    return text


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
