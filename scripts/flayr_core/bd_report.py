"""BD/internal report rendering backed by the frontend report prototype."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analysis_model import AnalysisResult
from .report import (
    evidence_quotes,
    format_generated_at,
    referenced_evidence_units,
    severity_value,
    stage_display_names,
    stage_skipped,
)
from .resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits
from .utils import write_text


ROOT = Path(__file__).resolve().parents[2]
BD_REPORT_TEMPLATE = ROOT / "assets" / "bd_report.html"
BD_REPORT_NAME = "bd_report.html"

_MARKET_LABELS = {
    "my": "马来西亚",
    "th": "泰国",
    "id": "印度尼西亚",
    "sg": "新加坡",
    "vn": "越南",
    "ph": "菲律宾",
    "sea": "东南亚",
    "auto": "未指定市场",
}
_SEVERITY_LABELS = {"large": "大", "medium": "中", "small": "小", "skip": "未涉及", "unknown": "未分析"}
_SEVERITY_CLASSES = {
    "large": "sev-large",
    "medium": "sev-medium",
    "small": "sev-small",
    "skip": "sev-small",
    "unknown": "sev-small",
}
_SEVERITY_DOTS = {
    "large": "var(--red)",
    "medium": "var(--amber)",
    "small": "var(--gray-chip)",
    "skip": "var(--gray-chip)",
    "unknown": "var(--gray-chip)",
}
_GAP_TYPES = {"structural": "结构性", "execution": "执行性", "resource": "资源性"}
_IMPACT_LEVELS = {"blocking": "P0", "major": "P1", "minor": "P2"}


def write_bd_report(
    run_dir: Path,
    analysis: dict[str, Any],
    *,
    budget: ResourceBudget | None = None,
) -> Path:
    """Write the prototype-backed BD/internal report for one run."""
    template = BD_REPORT_TEMPLATE.read_text(encoding="utf-8")
    payload = build_bd_report_data(analysis)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    if "{{bd_report_data}}" not in template:
        raise ValueError("BD report template is missing bd_report_data token")
    report = template.replace("{{bd_report_data}}", serialized)

    report_bytes = report.encode("utf-8")
    if budget is not None:
        budget.reserve_report(len(report_bytes))
    elif len(report_bytes) > ResourceLimits().max_report_bytes:
        raise ResourceBudgetExceeded(
            f"BD report exceeds max_report_bytes={ResourceLimits().max_report_bytes}: {len(report_bytes)} bytes"
        )
    report_path = run_dir / BD_REPORT_NAME
    write_text(report_path, report)
    return report_path


def build_bd_report_data(analysis: dict[str, Any]) -> dict[str, Any]:
    """Project the shared analysis into the internal report vocabulary."""
    result = AnalysisResult.from_mapping(analysis)
    product = _as_dict(analysis.get("product"))
    understanding = _as_dict(analysis.get("video_understanding"))
    creator_understanding = _as_dict(understanding.get("creator"))
    benchmark_understanding = _as_dict(understanding.get("benchmark"))
    scope = _as_dict(analysis.get("analysis_scope"))
    state = _safe_text(analysis.get("analysis_run_state"))

    stages = [
        _stage_payload(stage, index, creator_understanding, benchmark_understanding, state)
        for index, stage in enumerate(result.stages(), start=1)
        if isinstance(stage, dict)
    ]
    improvements = [
        _improvement_payload(item, rank, benchmark_understanding)
        for rank, item in enumerate(_sorted_improvements(result.improvements())[:3], start=1)
        if isinstance(item, dict)
    ]
    summary = _summary_payload(analysis)
    return {
        "title": f"{_safe_text(product.get('name')) or '未命名分析'} · 提升报告",
        "product": _safe_text(product.get("name")) or "未填写",
        "market": _MARKET_LABELS.get(_safe_text(product.get("target_market")).lower(), _safe_text(product.get("target_market")) or "未指定市场"),
        "generatedAt": format_generated_at(analysis.get("generated_at")),
        "strategyLevel": _safe_text(scope.get("level")) == "strategy",
        "degraded": state == "degraded",
        "degradedReason": _degraded_reason(analysis),
        "summary": summary,
        "gates": _gate_payload(analysis),
        "stages": stages,
        "improvements": improvements,
    }


def _stage_payload(
    stage: dict[str, Any],
    index: int,
    creator_understanding: dict[str, Any],
    benchmark_understanding: dict[str, Any],
    analysis_state: str,
) -> dict[str, Any]:
    code, name = stage_display_names(stage.get("stage"), index)
    skipped, _ = stage_skipped(stage)
    if skipped:
        severity = "skip"
    elif analysis_state in {"degraded", "not_run"}:
        severity = "unknown"
    else:
        severity = severity_value(stage.get("severity")) or "skip"
    creator_units = referenced_evidence_units(stage.get("creator_evidence_ids"), creator_understanding)
    benchmark_units = referenced_evidence_units(stage.get("benchmark_evidence_ids"), benchmark_understanding)
    gap = _first_text(
        _join_text(stage.get("gap_summary")),
        stage.get("gap"),
        stage.get("comparison_reason"),
    ) or "暂无明确差距描述。"
    creator_text = _side_text(stage, "creator", creator_units)
    benchmark_text = _side_text(stage, "benchmark", benchmark_units)
    communication = _first_text(
        stage.get("communication_strategy"),
        stage.get("communication_advice"),
        stage.get("talking_point"),
        _as_dict(_as_dict(stage.get("communication")).get("creator")).get("text"),
        _as_dict(_as_dict(stage.get("communication")).get("internal")).get("text"),
        _as_dict(_as_dict(stage.get("_communication")).get("creator")).get("text"),
    )
    if not communication:
        communication = f"可以围绕“{gap}”与达人确认：这段是想重点讲解，还是希望观众看到具体效果？"
    return {
        "code": code,
        "name": name,
        "severityClass": _SEVERITY_CLASSES[severity],
        "severityLabel": _SEVERITY_LABELS[severity],
        "dotColor": _SEVERITY_DOTS[severity],
        "creator": {
            "ts": _safe_text(stage.get("creator_time_range")) or "待确认",
            "text": creator_text or "暂无明确达人证据。",
        },
        "benchmark": {
            "ts": _safe_text(stage.get("benchmark_time_range")) or "待确认",
            "text": benchmark_text or "暂无明确标杆证据。",
        },
        "gap": gap,
        "talkingPoint": communication,
    }


def _improvement_payload(item: dict[str, Any], rank: int, benchmark_understanding: dict[str, Any]) -> dict[str, Any]:
    impact = _safe_text(item.get("gmv_impact")) or "待评估"
    gap_type = _GAP_TYPES.get(_safe_text(item.get("gap_type")), "待确认")
    action = _first_text(_join_text(item.get("actions")), item.get("suggestion"), item.get("expected_effect"))
    script = _safe_text(item.get("creator_script_zh"))
    if script and action:
        action = f"{action} 建议话术：{script}"
    benchmark = _first_text(item.get("benchmark_reference"))
    benchmark_range = _safe_text(item.get("benchmark_time_range"))
    units = referenced_evidence_units(item.get("benchmark_evidence_ids"), benchmark_understanding)
    facts = _join_text(
        [
            _first_text(unit.get("information"), unit.get("visual_fact"), unit.get("subtitle_fact"))
            for unit in units
        ]
    )
    if benchmark_range and benchmark:
        plan_b = f"标杆片段 {benchmark_range}：{benchmark}"
    elif benchmark:
        plan_b = benchmark
    elif benchmark_range and facts:
        plan_b = f"标杆片段 {benchmark_range}：{facts}"
    elif facts:
        plan_b = facts
    else:
        plan_b = "暂无明确标杆对应镜头。"
    return {
        "rank": rank,
        "title": _safe_text(item.get("title")) or "待确认提升点",
        "gmvImpact": impact,
        "gmvClass": _impact_class(impact),
        "gapType": gap_type,
        "planA": action or "暂无明确拍摄或执行方案。",
        "planB": plan_b,
    }


def _summary_payload(analysis: dict[str, Any]) -> dict[str, str]:
    verdict = _first_text(
        analysis.get("one_line_verdict"),
        analysis.get("commercial_priority_summary"),
        analysis.get("executive_summary"),
    )
    detail = _first_text(
        analysis.get("executive_summary"),
        analysis.get("one_line_summary"),
        analysis.get("commercial_priority_summary"),
    )
    if detail == verdict:
        detail = "请结合下方阶段证据、差距和沟通素材查看。"
    return {
        "verdict": verdict or "本次报告暂无一句话结论。",
        "detail": detail or "请结合下方阶段证据、差距和沟通素材查看。",
    }


def _gate_payload(analysis: dict[str, Any]) -> list[dict[str, str]]:
    diagnosis = _as_dict(analysis.get("global_diagnosis"))
    findings = [item for item in diagnosis.get("findings") or [] if isinstance(item, dict)]
    impact_order = {"blocking": 0, "major": 1, "minor": 2}
    findings.sort(key=lambda item: impact_order.get(_safe_text(item.get("impact")), 9))
    gates = []
    for item in findings[:2]:
        level = _IMPACT_LEVELS.get(_safe_text(item.get("impact")), "P2")
        title = _safe_text(item.get("title")) or _global_finding_title(_safe_text(item.get("id")))
        summary = _safe_text(item.get("summary")) or _safe_text(item.get("downstream_impact"))
        gates.append({"level": level, "text": f"{title}：{summary}" if summary else title})
    return gates


def _side_text(stage: dict[str, Any], role: str, units: list[dict[str, Any]]) -> str:
    value = _first_text(
        stage.get(f"{role}_key_message"),
        stage.get(f"{role}_summary"),
        _join_text(stage.get(f"{role}_visual_evidence")),
    )
    local_quote, translated_quote = evidence_quotes(units)
    quote = _first_text(local_quote, translated_quote)
    if value and quote:
        return f"{value} 口播：{quote}"
    if value:
        return value
    return _first_text(
        _join_text(
            [
                _first_text(unit.get("information"), unit.get("visual_fact"), unit.get("subtitle_fact"))
                for unit in units
            ]
        )
    )


def _degraded_reason(analysis: dict[str, Any]) -> str:
    flags = analysis.get("degraded_flags")
    if isinstance(flags, list) and flags:
        return "；".join(_safe_text(item) for item in flags if _safe_text(item))
    return "辅助产物已降级，不影响报告结论。"


def _sorted_improvements(items: list[Any]) -> list[dict[str, Any]]:
    valid = [item for item in items if isinstance(item, dict)]
    return sorted(valid, key=lambda item: _priority_key(item))


def _priority_key(item: dict[str, Any]) -> tuple[int, str]:
    try:
        priority = int(item.get("priority"))
    except (TypeError, ValueError):
        priority = 999
    return priority, _safe_text(item.get("title"))


def _impact_class(value: str) -> str:
    if "高" in value or value.lower() in {"high", "very_high"}:
        return "gmv-high"
    if "中" in value or value.lower() == "medium":
        return "gmv-mid"
    return ""


def _global_finding_title(value: str) -> str:
    return {
        "selling_point_route": "主卖点路线",
        "focus_coherence": "产品焦点一致性",
        "attention_cleanliness": "画面注意力洁净度",
    }.get(value, value or "视频级问题")


def _join_text(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(item for item in (_safe_text(entry) for entry in value) if item)
    return _safe_text(value)


def _first_text(*values: Any) -> str:
    for value in values:
        text = _safe_text(value)
        if text:
            return text
    return ""


def _safe_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value or "").strip()
    if text in {"无", "暂无", "未知", "null"}:
        return ""
    return text


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
