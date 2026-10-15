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

from .artifacts import (
    format_seconds, get_analysis_frame_entries, get_frame_entries,
    parse_time_range_seconds, parse_timestamp_seconds,
)
from .human_review import _remove_performance_forecast_sentences, model_stage_values
from .report import (
    REPORT_MAX_EMBEDDED_BYTES,
    ReportAssetContext,
    evidence_quotes,
    referenced_evidence_units,
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
# Apply only to delivery prose, never to evidence quotes or source identities.
_DELIVERY_WORDING = (
    ("建议达人", "可以"),
    ("建议你", "可以"),
    ("你可以", "可以"),
    ("你的这条视频", "你的视频"),
    ("达人的视频", "你的视频"),
    ("达人视频", "你的视频"),
    ("当前视频", "你的视频"),
    ("参考视频中的出镜者", "爆款视频的出镜者"),
    ("参考视频", "爆款视频"),
    ("标杆视频", "爆款视频"),
    ("达人更", "你的视频更"),
    ("标杆更", "爆款视频更"),
    ("达人", "你的视频的出镜者"),
    ("标杆", "爆款视频的出镜者"),
)
_LITERAL_PROSE_RE = re.compile(
    r'(“[^”]*”|「[^」]*」|\"[^\"]*\"|`[^`]*`|https?://\S+'
    r'|(?:[A-Za-z]:[\\/]|/)[^\s，。；]+'
    r'|\b[\w.-]+\.(?:mp4|mov|png|jpg|json)\b)'
)
_INTERNAL_ROLE_RE = re.compile(r"\b(creator|benchmark)\b", re.IGNORECASE)
_INTERNAL_PROSE_RE = re.compile(
    r"\b(?:S[1-6]|Stage[12]|small|medium|large|none|NA|uncertain|DEGRADED|FAILED|"
    r"PROCESSING|UNPUBLISHED|COMPLETED|prediction_unavailable|pending|grounded)\b"
    r"|差距|档位|门禁|资格层|资格响应|内部备注|改判|沿用用户已确认结论",
    re.IGNORECASE,
)
_STAGE_NAMES = ("吸引注意", "产品引出", "功能与使用", "效果呈现", "信任表达", "购买提醒")


def write_creator_report(
    run_dir: Path,
    analysis: dict[str, Any],
    *,
    budget: ResourceBudget | None = None,
    output_name: str = CREATOR_REPORT_NAME,
    review_status: str | None = None,
) -> Path:
    """Write the prototype-backed creator report for one completed analysis."""
    template = CREATOR_REPORT_TEMPLATE.read_text(encoding="utf-8")
    assets = ReportAssetContext(
        run_dir,
        max_embedded_bytes=budget.limits.max_report_bytes if budget is not None else REPORT_MAX_EMBEDDED_BYTES,
    )
    payload = build_creator_report_data(analysis, assets, review_status=review_status)
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
    report_path = run_dir / output_name
    write_text(report_path, report)
    return report_path


def build_creator_report_data(
    analysis: dict[str, Any],
    assets: ReportAssetContext,
    *,
    review_status: str | None = None,
) -> dict[str, Any]:
    """Project semantic analysis into the creator report's public vocabulary."""
    semantic = SemanticAnalysis.from_mapping(analysis)
    effective_review_status = _safe_text(review_status or semantic.get("review_status")) or "pending"
    product = semantic.product
    videos = semantic.videos
    creator_info = _as_dict(videos.get("creator"))
    creator_understanding = semantic.side("creator")
    benchmark_info = _as_dict(videos.get("benchmark"))
    benchmark_understanding = semantic.side("benchmark")

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
    experiments_by_stage = {
        _stage_code(item.get("targetStage"), index): item
        for index, item in enumerate(experiments, start=1)
        if item.get("targetStage")
    }
    top_experiment = experiments[0] if experiments else None

    stages = []
    figure_number = 0
    for index, stage in enumerate(raw_stages, start=1):
        _, name = stage_display_names(stage.get("stage", ""), index)
        code = _stage_code(stage.get("stage"), index)
        name = _STAGE_NAMES[int(code[1:]) - 1] if re.fullmatch(r"S[1-6]", code) else name
        units = referenced_evidence_units(stage.get("creator_evidence_ids"), creator_understanding, code)
        benchmark_units = referenced_evidence_units(stage.get("benchmark_evidence_ids"), benchmark_understanding, code)
        creator_evidence = _bound_evidence(creator_info, units, assets, "creator", name)
        benchmark_evidence = _bound_evidence(benchmark_info, benchmark_units, assets, "benchmark", name)
        for item in [*creator_evidence, *benchmark_evidence]:
            if item["frame"]:
                figure_number += 1
                item["figure"] = f"图 {figure_number}"
        review_decision = _safe_text(stage.get("review_decision"))
        if not review_decision and model_stage_values(stage)["not_applicable"]:
            review_decision = "not_applicable"
        observation = _stage_observation(stage, units)
        if review_decision == "not_applicable":
            observation = _review_delivery_text(stage.get("review_note")) or "本次不作比较。"
        elif review_decision == "insufficient_evidence":
            observation = "这部分暂不作具体比较。"
        elif review_decision == "corrected":
            observation = _review_delivery_text(stage.get("review_note"))
        observation = _delivery_text(_review_delivery_text(observation))
        # A comparison sentence cannot borrow the other video's screenshot or an unbound time label.
        if review_decision not in {"not_applicable", "insufficient_evidence"}:
            observation = _bound_delivery_text(observation, creator_evidence, benchmark_evidence)
        insufficient = bool(stage.get("insufficient_evidence")) or code in low_confidence_codes
        if not observation and not units:
            insufficient = True
        linked_id = _linked_experiment_id(stage, code, experiments, experiments_by_stage)
        if linked_id and linked_id not in {item.get("id") for item in experiments}:
            linked_id = ""

        reference = _reference_payload(benchmark_evidence)
        if review_decision == "not_applicable":
            linked_id = ""
            status, status_label = "not_applicable", "本次不比较"
        elif review_decision == "insufficient_evidence":
            linked_id = ""
            status, status_label = "insufficient_evidence", "暂不作比较"
        else:
            status, status_label = _stage_status(stage, linked_id, experiments, insufficient)
        stages.append(
            {
                "code": code,
                "name": name,
                "status": status,
                "statusLabel": status_label,
                "deliveryText": observation,
                "creatorEvidence": creator_evidence,
                "reference": reference,
                "referenceNote": "爆款视频的对应片段暂未展示。" if not reference else "",
                "linked": linked_id,
                "confidence": "",
            }
        )

    by_code = {stage["code"]: stage for stage in stages}
    for experiment in experiments:
        target = by_code.get(experiment.get("targetStage"), {})
        evidence = [*target.get("creatorEvidence", []), *_as_dict(target.get("reference")).get("evidence", [])]
        experiment["evidenceFigures"] = [item["figure"] for item in evidence if item["figure"]]

    highlights = _build_highlights(semantic.creator_context)
    review_summary = semantic.get("review_summary")
    review_delivery = _as_dict(analysis.get("review_delivery"))
    if (
        review_delivery.get("summary_status") == "replaced_after_stage_review"
        and isinstance(review_summary, dict)
    ):
        approved_actions = [
            item["action"] for item in experiments if item["status"] == "human_approved_action"
        ]
        intent = "\n".join(approved_actions) or "本次先查看下面的视频片段，暂不提供拍摄建议。"
    else:
        intent = _first_text(
            semantic.creator_context.get("content_intent"),
            semantic.creator_context.get("creator_content_intent"),
            creator_understanding.get("content_intent"),
            creator_understanding.get("content_summary"),
            semantic.get("one_line_summary"),
            semantic.get("executive_summary"),
        )
    payload = {
        "brand": "Flayr · 心译复盘",
        "reviewStatus": effective_review_status,
        "reviewMode": _safe_text(semantic.get("review_mode")) or "draft_visible",
        "reviewSummary": {
            "verdict": _delivery_text(intent),
            "detail": "可以先查看下面的视频片段和拍摄建议。",
        } if isinstance(review_summary, dict) else None,
        "metadata": build_report_metadata("creator-v2", "flayr_core.creator_report"),
        "title": "这条视频，我们一起复盘",
        "context": _context_line(product),
        "intent": _delivery_text(intent) or "目前还无法判断这条视频正在尝试完成什么。",
        "highlights": highlights,
        "topExperiment": top_experiment,
        "experiments": experiments,
        "stages": stages,
        "continuity": _build_continuity(semantic.creator_context.get("continuity_record")),
        "keep": _build_keep(semantic.creator_context),
        "analysisState": _safe_text(semantic.get("analysis_run_state")),
        "degradedFlags": ["部分片段暂不提供比较说明，请以页面中已有的片段和建议为准。"]
            if semantic.get("analysis_run_state") == "degraded" or semantic.get("degraded_flags") else [],
    }
    _validate_delivery_payload(payload)
    return payload


def _build_experiments(
    semantic: SemanticAnalysis,
    stage_by_code: dict[str, dict[str, Any]],
    creator_understanding: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_candidates = semantic.creator_context.get("candidate_experiments")
    if isinstance(raw_candidates, list):
        raw_candidates = list(raw_candidates) + [
            item.data
            for item in semantic.improvements
            if _safe_text(item.data.get("status")).lower() == "human_approved_action"
        ]
    else:
        raw_candidates = [item.data for item in semantic.improvements]

    ordered = list(enumerate(raw_candidates))
    ordered.sort(
        key=lambda pair: (
            0
            if isinstance(pair[1], dict)
            and _safe_text(pair[1].get("status")).lower() == "human_approved_action"
            else 1,
            *_priority_key(pair[1], pair[0]),
        )
    )
    experiments: list[dict[str, Any]] = []
    approved_actions: set[tuple[str, str]] = set()
    for original_index, raw in ordered:
        if not isinstance(raw, dict):
            continue
        action_status = _safe_text(raw.get("status")).lower()
        if action_status == "needs_human_confirmation":
            continue
        target_stage = _stage_code(
            _first_text(raw.get("target_stage"), raw.get("stage"), raw.get("stage_code")),
            0,
        )
        if action_status == "human_approved_action":
            action = _first_text(raw.get("action"), raw.get("suggestion"))
            action_key = (target_stage, action)
            if not action or action_key in approved_actions:
                continue
            approved_actions.add(action_key)
            experiments.append(
                {
                    "id": _safe_text(raw.get("experiment_id")) or f"reviewed-{original_index + 1}",
                    "title": "可以试试这项拍摄调整",
                    "targetStage": target_stage,
                    "observation": "",
                    "hypothesis": "",
                    "action": _delivery_action(action),
                    "demonstration": "",
                    "verification": "下次核对是否完成这条拍摄动作。",
                    "why": "先观察这项调整是否让内容表达更清楚，不预设销量变化。",
                    "confidence": "",
                    "status": action_status,
                    "sourceIndex": original_index,
                }
            )
            if len(experiments) >= 3:
                break
            continue
        source_stage = stage_by_code.get(target_stage, {})
        source_units = referenced_evidence_units(
            source_stage.get("creator_evidence_ids"),
            creator_understanding,
            target_stage,
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
        public_action = _delivery_action(action)
        if not verification:
            verification = f"是否完成“{public_action}”并在对应片段看见这项改动。"
        experiments.append(
            {
                "id": experiment_id,
                "title": _experiment_delivery_text(title),
                "targetStage": target_stage,
                "observation": _experiment_delivery_text(observation),
                "hypothesis": _experiment_delivery_text(hypothesis),
                "action": public_action,
                "demonstration": _experiment_delivery_text(demonstration),
                "verification": _experiment_delivery_text(verification),
                "why": _experiment_delivery_text(_first_text(raw.get("why"), hypothesis, observation)),
                "confidence": _delivery_text(raw.get("confidence")),
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
        highlights.append({"timestamp": timestamp, "text": _delivery_text(text)})
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
        items.append({"text": _delivery_text(text), "statusClass": status_class, "statusLabel": status_label})
    return {
        "title": "上次约定回顾",
        "items": items,
    } if items else None


def _build_keep(creator_context: Mapping[str, Any]) -> str:
    value = creator_context.get("retained_points")
    if value is None:
        value = creator_context.get("creator_retain")
    if isinstance(value, list):
        return "、".join(item for item in (_delivery_text(entry) for entry in value) if item)
    return _delivery_text(value)


def _reference_payload(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not evidence:
        return None
    return {
        "ts": evidence[0]["timeRange"],
        "text": "；".join(item["text"] for item in evidence if item["text"]),
        "evidence": evidence,
    }


def _bound_evidence(
    info: dict[str, Any], units: list[dict[str, Any]], assets: ReportAssetContext,
    role: str, stage_name: str,
) -> list[dict[str, Any]]:
    """Bind each displayed excerpt to one role's real ledger window, never a stage bucket."""
    entries = get_analysis_frame_entries(info) + get_frame_entries(info)
    role_root = (assets.run_dir / role).resolve()
    evidence = []
    for unit in units:
        raw_range = unit.get("time_range")
        bounds = parse_time_range_seconds(raw_range, info.get("duration_seconds"))
        if bounds is None:
            continue
        start, end = bounds
        candidates = []
        for entry in entries:
            timestamp = parse_timestamp_seconds(entry.get("timestamp_seconds"))
            path = assets.safe_file(entry.get("path"))
            if timestamp is None or not start <= timestamp <= end or path is None:
                continue
            if not path.is_relative_to(role_root):
                continue
            candidates.append(entry)
        frame = None
        for entry in sorted(candidates, key=lambda item: abs(parse_timestamp_seconds(item["timestamp_seconds"]) - (start + end) / 2)):
            frame = _frame_payload(entry, assets, stage_name)
            if frame:
                break
        quote, quote_zh = evidence_quotes([unit])
        if not frame and not (quote or quote_zh):
            continue
        text = _first_text(unit.get("visual_fact"), unit.get("subtitle_fact"), unit.get("information")) if frame else ""
        evidence.append({
            "evidenceId": str(unit.get("id") or ""),
            "timeRange": f"{format_seconds(start)} - {format_seconds(end)}",
            "frame": frame,
            "frameTs": frame["timestamp"] if frame else "",
            "figure": "",
            "quote": quote,
            "quoteZh": quote_zh,
            "text": _delivery_text(_review_delivery_text(text), evidence_role=role),
            "sourceKind": "visual" if frame else "speech",
        })
        if len(evidence) >= 3:
            break
    return evidence


def _review_delivery_text(value: Any) -> str:
    """Keep factual wording separate from the untouched internal rationale."""
    text = _safe_text(_remove_performance_forecast_sentences(value))
    # Do not extract an unqualified conclusion from a rationale whose conditions were internal.
    if _INTERNAL_PROSE_RE.search(text):
        return ""
    kept = []
    for clause in re.split(r"(?<=[。！？!?；;\n])", text):
        clause = re.sub(r"^(?:历史人工确认|用户本轮听音确认|人工确认|用户确认)[：:]?", "", clause.strip())
        quoted_internal = any(
            part.startswith(('“', '「', '"')) and re.search(r"达人|标杆|当前视频|参考视频|\b(?:creator|benchmark)\b", part, re.I)
            for part in _LITERAL_PROSE_RE.split(clause)[1::2]
        )
        if clause and not (quoted_internal or "模型" in clause):
            kept.append(clause.replace("本轮", "本次"))
    return "".join(kept)


def _bound_delivery_text(text: str, creator: list[dict[str, Any]], benchmark: list[dict[str, Any]]) -> str:
    kept = []
    for sentence in re.split(r"(?<=[。！？!?；;\n])", text):
        if not (creator or benchmark):
            continue
        if ("你的视频" in sentence and not creator) or ("爆款视频" in sentence and not benchmark):
            continue
        comparative = bool(re.search(r"双方|两条|相当|相近|相比|更好|更强|略好|优于|好过|不如|弱于", sentence))
        if comparative and not (creator and benchmark):
            continue
        # Speech-only evidence is shown as its literal quote, not converted into free visual prose.
        sides = [benchmark] if "爆款视频" in sentence and "你的视频" not in sentence else [creator]
        if comparative or ("你的视频" in sentence and "爆款视频" in sentence):
            sides = [creator, benchmark]
        if any(not any(item["frame"] for item in side) for side in sides):
            continue
        kept.append(sentence)
    return "".join(kept)


def _validate_delivery_payload(payload: dict[str, Any]) -> None:
    visible = {
        "title", "context", "intent", "keep", "verdict", "detail", "name", "deliveryText",
        "observation", "referenceNote", "confidence", "text", "hypothesis", "action",
        "demonstration", "verification", "why", "statusLabel", "degradedFlags", "timestamp",
    }
    def check(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in visible:
                    texts = item if isinstance(item, list) else [item]
                    for text in texts:
                        if isinstance(text, str):
                            for part in _LITERAL_PROSE_RE.split(text):
                                if part.startswith(('`', 'http://', 'https://', '/', '\\')) or re.fullmatch(r"[\w.-]+\.(?:mp4|mov|png|jpg|json)", part):
                                    continue
                                if _INTERNAL_PROSE_RE.search(part) or re.search(r"达人|标杆|当前视频|参考视频|\b(?:creator|benchmark)\b", part, re.I):
                                    raise ValueError(f"对外交付文字包含内部用语：{key}")
                if key not in {"quote", "quoteZh", "metadata", "frame"}:
                    check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(payload)


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
        return f"「{name}」视频的内容复盘与拍摄建议。对比你的视频与本次提供的爆款视频。"
    return "本次视频的内容复盘与拍摄建议。对比你的视频与本次提供的爆款视频。"


def _delivery_text(value: Any, *, evidence_role: str | None = None) -> str:
    text = _safe_text(_remove_performance_forecast_sentences(value))
    text = re.sub(r"^(?:历史人工确认|用户本轮听音确认|人工确认|用户确认)[：:]?", "", text)
    text = text.removesuffix("；沿用用户已确认结论。")
    parts = _LITERAL_PROSE_RE.split(text)
    for index in range(0, len(parts), 2):
        for old, new in _DELIVERY_WORDING:
            # In a single-video ledger, "达人" names that video's speaker, not the compared creator.
            if evidence_role == "benchmark" and old.startswith("达人"):
                new = new.replace("你的视频", "爆款视频")
            parts[index] = parts[index].replace(old, new)
        parts[index] = _INTERNAL_ROLE_RE.sub(
            lambda match: "你的视频" if match.group(1).lower() == "creator" and evidence_role != "benchmark" else "爆款视频",
            parts[index],
        )
        if re.search(r"(?<!迷)你(?!的视频)", parts[index]):
            raise ValueError("对外交付文字仍有直接称呼，请改为具体行动建议后再输出")
    return "".join(parts)


def _delivery_action(value: Any) -> str:
    text = _delivery_text(value)
    if text.startswith("建议"):
        text = "可以" + text[2:].lstrip("：: ")
    if not text or text.startswith(("可以", "可尝试", "建议", "试试看")):
        return text
    return f"可以{text}"


def _experiment_delivery_text(value: Any) -> str:
    """Translate stage references in descriptive prose, not literal quotes or actions."""
    parts = _LITERAL_PROSE_RE.split(_delivery_text(value))
    for index in range(0, len(parts), 2):
        parts[index] = _STAGE_CODE_RE.sub(
            lambda match: _STAGE_NAMES[int(match.group(1)) - 1], parts[index],
        )
    return "".join(parts)


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
