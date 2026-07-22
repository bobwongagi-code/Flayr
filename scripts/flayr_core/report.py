"""HTML report rendering for Flayr."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analysis_model import AnalysisResult
from .artifacts import format_seconds, parse_timestamp_seconds, select_frame_near_timestamp, select_frames_for_time_range
from .resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits, encode_file_data_url
from .utils import write_text


ROOT = Path(__file__).resolve().parents[2]
REPORT_TEMPLATE = ROOT / "assets" / "report.html"
REPORT_IMAGE_MAX_BYTES = 8 * 1024 * 1024
REPORT_MAX_EMBEDDED_BYTES = ResourceLimits().max_report_bytes


@dataclass
class ReportAssetContext:
    """Resolve report media only from the current run and cache encodings."""

    run_dir: Path
    image_cache: dict[Path, str] = field(default_factory=dict)
    max_embedded_bytes: int = REPORT_MAX_EMBEDDED_BYTES
    embedded_bytes: int = 0

    def __post_init__(self) -> None:
        self.run_dir = self.run_dir.expanduser().resolve()

    def safe_file(self, value: Any) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.run_dir / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        if not resolved.is_file():
            return None
        try:
            resolved.relative_to(self.run_dir)
        except ValueError:
            # resolve(strict=True) follows symlinks, so this rejects symlink
            # targets that escape the run directory as well as ../ traversal.
            return None
        return resolved

    def image_src(self, frame: dict[str, Any]) -> str:
        path = self.safe_file(frame.get("path"))
        if path is None:
            return ""
        cached = self.image_cache.get(path)
        if cached is not None:
            return cached
        try:
            encoded = encode_file_data_url(path, max_bytes=REPORT_IMAGE_MAX_BYTES, expected_kind="image")
        except (OSError, ResourceBudgetExceeded, ValueError):
            return ""
        encoded_bytes = len(encoded.encode("utf-8"))
        if self.embedded_bytes + encoded_bytes > self.max_embedded_bytes:
            return ""
        self.embedded_bytes += encoded_bytes
        self.image_cache[path] = encoded
        return encoded

def write_report(
    run_dir: Path,
    analysis: dict[str, Any],
    *,
    budget: ResourceBudget | None = None,
) -> Path:
    template = REPORT_TEMPLATE.read_text(encoding="utf-8")
    assets = ReportAssetContext(
        run_dir,
        max_embedded_bytes=budget.limits.max_report_bytes if budget is not None else REPORT_MAX_EMBEDDED_BYTES,
    )
    report = template
    report = report.replace("{{generated_at}}", escape(format_generated_at(analysis.get("generated_at"))))
    report = report.replace("{{executive_summary}}", escape(executive_summary(analysis)))
    report = report.replace("{{overview_cards}}", render_overview_cards(analysis, run_dir, assets))
    report = report.replace("{{global_diagnosis}}", render_global_diagnosis(analysis))
    report = report.replace("{{holistic_assessment}}", render_key_conclusions(analysis))
    report = report.replace("{{gap_overview}}", render_gap_overview(analysis))
    report = report.replace("{{stage_rows}}", render_stage_rows(analysis, assets))
    report = report.replace("{{improvement_cards}}", render_improvement_cards(analysis, assets))

    report_bytes = report.encode("utf-8")
    if budget is not None:
        budget.reserve_report(len(report_bytes))
    elif len(report_bytes) > ResourceLimits().max_report_bytes:
        raise ResourceBudgetExceeded(
            f"report exceeds max_report_bytes={ResourceLimits().max_report_bytes}: {len(report_bytes)} bytes"
        )
    report_path = run_dir / "report.html"
    write_text(report_path, report)
    return report_path


def format_generated_at(value: Any) -> str:
    # 去掉 ISO 时间里的 T，仅显示日期+时间，例如 2026-05-28 22:34:27
    # 同时去掉可能附加的后缀（如 " · improve"）
    text = str(value or "").strip()
    text = text.replace("T", " ")
    # 去掉 " · improve" 等工作目录后缀
    for sep in (" · ", " - "):
        if sep in text:
            parts = text.split(sep)
            # 保留第一部分（时间戳），去掉后面的目录名
            text = parts[0]
            break
    return text


def render_stage_rows(analysis: dict[str, Any], assets: ReportAssetContext | None = None) -> str:
    assets = assets or ReportAssetContext(Path(str(analysis.get("run_dir") or ".")))
    rows = []
    result = AnalysisResult.from_mapping(analysis)
    stages = result.stages()
    benchmark_info = analysis["videos"].get("benchmark", {})
    creator_info = analysis["videos"].get("creator", {})
    understanding = analysis.get("video_understanding", {})
    for index, stage in enumerate(stages, start=1):
        skipped, reason = stage_skipped(stage)
        if skipped:
            rows.append(render_skipped_stage(stage, index, reason))
            continue

        # The combined ``time_range`` is display-only and contains both roles.
        # Never use it to select one role's frames, or evidence can cross sides.
        creator_range = str(stage.get("creator_time_range") or "")
        benchmark_range = str(stage.get("benchmark_time_range") or "")
        creator_cells = role_cells(
            stage, "creator", creator_info, creator_range, understanding.get("creator", {}), assets
        )
        benchmark_cells = role_cells(
            stage, "benchmark", benchmark_info, benchmark_range, understanding.get("benchmark", {}), assets
        )

        parts = [
            f'<div class="stage" id="{escape(stage_anchor(index))}">',
            # 跨两列的阶段头：阶段名 + 差距等级
            '<div class="stage-header">',
            f"<h3>{escape(stage['stage'])}</h3>",
            render_gap_badge(stage.get("severity")),
            "</div>",
            render_global_cause_note(stage.get("affected_by_global_issues")),
            # 列头
            '<div class="stage-col-head">达人表现</div>',
            '<div class="stage-col-head">标杆表现</div>',
        ]
        # 逐段落成对输出，保证两列行对齐
        section_labels = ["核心结论", "口播证据", "画面截图", "画面证据"]
        for label, c_cell, b_cell in zip(section_labels, creator_cells, benchmark_cells):
            parts.append(f'<div class="section-label">{escape(label)}</div>')
            parts.append(f'<div class="stage-col">{c_cell}</div>')
            parts.append(f'<div class="stage-col">{b_cell}</div>')
        parts.append("</div>")
        rows.append("\n".join(parts))
    return "\n".join(rows)


def render_global_cause_note(value: Any) -> str:
    ids = value if isinstance(value, list) else []
    labels = [global_finding_title(str(item)) for item in ids if str(item).strip()]
    if not labels:
        return ""
    return (
        '<div class="cause-note"><strong>先处理根因：</strong>'
        f'{escape("、".join(labels))}。本阶段结论保留，但执行建议受上述问题影响。</div>'
    )


def stage_skipped(stage: dict[str, Any]) -> tuple[bool, str]:
    """判断阶段是否双方都未设计（如 S5 都无信任背书），如是则折叠不展开分析。"""
    comparison_status = str(stage.get("comparison_status") or "")
    if comparison_status == "not_directly_comparable":
        reason = str(stage.get("comparison_reason") or "两条视频不能在该阶段做产品级直接比较。")
        return True, f"该阶段没有共同的比较合同，不输出差距判断。{reason}"
    if comparison_status == "not_applicable":
        reason = str(stage.get("comparison_reason") or "双方在该阶段均未涉及可比较内容。")
        return True, reason
    if normalize_severity(stage.get("severity")) != "small":
        return False, ""
    skip_phrases = (
        "没有形成可独立核验", "均未设计", "未发现对应", "无明显设计",
        "均未", "无明显", "双方均无", "未涉及", "无法识别",
    )
    # 检查 key_message / summary / gap 三个维度的内容
    creator_parts = [
        str(stage.get(f"creator_{k}") or "") for k in ("key_message", "summary", "gap")
    ]
    benchmark_parts = [
        str(stage.get(f"benchmark_{k}") or "") for k in ("key_message", "summary", "gap")
    ]
    creator = " ".join(creator_parts)
    benchmark = " ".join(benchmark_parts)
    gap = str(stage.get("gap") or "")

    def is_effectively_empty(text: str) -> bool:
        t = text.strip()
        if not t or t in {"无", "无。", "无。", "—"}:
            return True
        return any(p in t for p in skip_phrases)

    # 双方 key_message/summary 都为空 或 包含跳过标记
    creator_empty = is_effectively_empty(creator)
    benchmark_empty = is_effectively_empty(benchmark)
    gap_skip = any(p in gap for p in skip_phrases)

    # S5 特殊处理：如果双方都没有实质信任背书内容，跳过
    stage_name = str(stage.get("stage", "") or "")
    is_s5 = "S5" in stage_name or "信任" in stage_name or "trust" in stage_name.lower()

    if is_s5:
        # 单一来源：跟随 derive 的 S5 硬背书判定，不再独立扫关键词（旧扫描含软背书 test/推荐，
        # 与 derive 的 hard-only 口径分叉，会出现 derive 打分、报告却跳过的矛盾）。
        derive_reason = str((stage.get("severity_derivation") or {}).get("reason") or "")
        if "均无硬背书" in derive_reason:
            return True, "双方均未提供硬背书（机构/检测/权威），S5 信任放大环节不单独分析。"

    if (creator_empty and benchmark_empty) or gap_skip:
        return True, "双方均未设计该环节，不单独分析。"
    return False, ""


def render_skipped_stage(stage: dict[str, Any], index: int, reason: str) -> str:
    label = "不比较" if str(stage.get("comparison_status") or "") == "not_directly_comparable" else "未涉及"
    return "\n".join(
        [
            f'<div class="stage stage-skipped" id="{escape(stage_anchor(index))}">',
            '<div class="stage-header">',
            f"<h3>{escape(stage['stage'])}</h3>",
            f'<span class="gap-badge gap-low"><span>{escape(label)}</span></span>',
            "</div>",
            f'<div class="meta skip-note">{escape(reason)}</div>',
            "</div>",
        ]
    )


def role_cells(
    stage: dict[str, Any],
    prefix: str,
    info: dict[str, Any],
    time_range: str,
    understanding: dict[str, Any],
    assets: ReportAssetContext,
) -> list[str]:
    """返回一个角色的 4 个段落 cell：核心结论 / 口播证据 / 画面截图 / 画面证据。"""
    units = referenced_evidence_units(stage.get(f"{prefix}_evidence_ids", []), understanding)
    frames = select_referenced_frames(info, units, time_range)
    quote, quote_zh = evidence_quotes(units)
    visual_evidence = list(
        dict.fromkeys(
            [
                *[str(v) for v in stage.get(f"{prefix}_visual_evidence", []) if str(v).strip()],
                *evidence_visual_facts(units),
            ]
        )
    )[:5]

    # 核心结论：用 summary（达人/标杆在该阶段的结论），口播/截图/画面证据为支撑
    conclusion = stage.get(f"{prefix}_summary") or stage.get(f"{prefix}_key_message") or ""
    conclusion_cell = "\n".join(
        [
            f'<div class="meta">阶段时间：{escape(time_range)}</div>',
            "<ol>",
            *render_list_items(conclusion),
            "</ol>",
            render_support_badge(stage.get(f"{prefix}_support_status", "")),
        ]
    )
    quote_cell = render_quote(
        quote or stage.get(f"{prefix}_quote"), quote_zh or stage.get(f"{prefix}_quote_zh")
    ) or '<div class="meta">无口播证据。</div>'
    shot_cell = render_shot_grid(frames, assets)
    visual_cell = render_fact_list(visual_evidence)
    return [conclusion_cell, quote_cell, shot_cell, visual_cell]


def render_gap_badge(severity: str) -> str:
    labels = {
        "large": ("high", "差距明显"),
        "medium": ("mid", "差距中等"),
        "small": ("low", "差距较小"),
    }
    normalized = severity_value(severity)
    if normalized is None:
        return (
            '<span class="gap-badge gap-unavailable">'
            '<span class="gap-dot" aria-hidden="true"></span>'
            '<span>未分析</span>'
            "</span>"
        )
    css_level, label = labels[normalized]
    return (
        f'<span class="gap-badge gap-{escape(css_level)}">'
        '<span class="gap-dot" aria-hidden="true"></span>'
        f'<span>{escape(label)}</span>'
        "</span>"
    )


def render_list_items(text: Any, limit: int = 3) -> list[str]:
    parts = split_readable_points(text, limit)
    return [f"<li>{escape(part)}</li>" for part in parts]


def split_readable_points(text: Any, limit: int = 3) -> list[str]:
    if isinstance(text, list):
        points = [str(item).strip() for item in text if str(item).strip()]
        if not points:
            return ["暂无明确描述。"]
        if len(points) > limit:
            return points[: limit - 1] + ["；".join(points[limit - 1 :])]
        return points
    raw = str(text or "").strip()
    if not raw:
        return ["暂无明确描述。"]
    normalized = re.sub(r"\s+", " ", raw)
    normalized = re.sub(r"(?<!\d)[。；;](?!\d)", "。|", normalized)
    parts = [part.strip(" |。；;") for part in normalized.split("|") if part.strip(" |。；;")]
    if len(parts) <= 1 and len(normalized) > 60:
        parts = [part.strip() for part in re.split(r"，|,", normalized) if part.strip()]
    if not parts:
        parts = [normalized]
    if len(parts) > limit:
        head = parts[: limit - 1]
        tail = "，".join(parts[limit - 1:])
        parts = head + [tail]
    return parts


def executive_summary(analysis: dict[str, Any]) -> str:
    result = AnalysisResult.from_mapping(analysis)
    state = str(result.get("analysis_run_state") or "").strip().lower()
    mode = str(result.get("mode") or "").strip().lower()
    if state == "degraded" or (state == "not_run" and mode in {"compare", "improve"}):
        return "本报告未完成大模型分析，仅展示预处理和占位信息；阶段差距与提升点不可作为业务判断。"
    eligibility = result.get("comparison_contract") or result.get("comparison_eligibility")
    if isinstance(eligibility, dict):
        from .llm.parse import normalize_comparison_contract

        eligibility = normalize_comparison_contract(eligibility)
    if isinstance(eligibility, dict) and str(eligibility.get("overall_status") or "") == "selective_structural":
        from .postprocess.repair_stages import comparison_scope_summary

        summary = str(
            result.get("commercial_priority_summary")
            or result.get("one_line_summary")
            or result.get("executive_summary")
            or ""
        ).strip()
        scope_note = comparison_scope_summary(eligibility)
        return f"{scope_note} {summary}".strip()
    if isinstance(eligibility, dict) and str(eligibility.get("overall_status") or "") in {"not_comparable", "uncertain"}:
        # 兼容旧运行结果：即便 JSON 尚未走过最新后处理，报告也不能展示跨品产品结论。
        from .postprocess.repair_stages import comparison_scope_summary

        return comparison_scope_summary(eligibility)
    commercial_summary = str(result.get("commercial_priority_summary") or "").strip()
    if commercial_summary:
        return commercial_summary
    summary = str(result.get("one_line_summary") or result.get("executive_summary") or "").strip()
    if summary:
        return summary
    improvements = sorted(result.improvements(), key=lambda item: item.get("priority", 999))
    if improvements:
        return f"达人视频优先改：{improvements[0].get('title', '核心成交阻力')}。"
    large_stages = [
        item.get("stage", "")
        for item in result.stages()
        if item.get("severity") == "large"
        and str(item.get("comparison_status") or "") not in {"not_directly_comparable", "not_applicable"}
    ]
    if large_stages:
        return f"达人视频最大差距集中在：{'、'.join(large_stages[:2])}。"
    return "当前报告已完成阶段拆解，建议优先查看 Top 提升点。"


def render_global_diagnosis(analysis: dict[str, Any]) -> str:
    result = AnalysisResult.from_mapping(analysis)
    diagnosis = result.get("global_diagnosis") if isinstance(result.get("global_diagnosis"), dict) else {}
    findings = [
        item for item in diagnosis.get("findings") or []
        if isinstance(item, dict) and item.get("impact") in {"blocking", "major", "minor"}
    ]
    if not findings:
        return ""
    impact_order = {"blocking": 0, "major": 1, "minor": 2}
    gate_order = {"selling_point_route": 0, "focus_coherence": 1, "attention_cleanliness": 2}
    findings.sort(key=lambda item: (impact_order.get(str(item.get("impact")), 9), gate_order.get(str(item.get("id")), 9)))
    labels = {"blocking": "根本阻断", "major": "显著影响", "minor": "轻度影响"}
    rows = []
    for item in findings:
        impact = str(item.get("impact") or "major")
        affected = "、".join(str(stage) for stage in item.get("affected_stages") or [])
        rows.append(
            "\n".join(
                [
                    f'<article class="root-finding root-{escape(impact)}">',
                    '<div class="root-finding-head">',
                    f'<span class="root-impact">{escape(labels.get(impact, impact))}</span>',
                    f'<h3>{escape(global_finding_title(str(item.get("id") or "")))}</h3>',
                    "</div>",
                    f'<p class="root-summary">{escape(item.get("summary"))}</p>',
                    f'<p><strong>影响：</strong>{escape(item.get("downstream_impact"))}</p>',
                    f'<p><strong>先做：</strong>{escape(item.get("suggested_action"))}</p>',
                    f'<div class="meta">关联阶段：{escape(affected or "全局")}</div>',
                    "</article>",
                ]
            )
        )
    capability = diagnosis.get("temporal_capability") if isinstance(diagnosis.get("temporal_capability"), dict) else {}
    reliability = ""
    if str(capability.get("comparative") or "") in {"static_only", "unknown"}:
        reliability = '<div class="root-reliability">部分时序对比证据不足；相关门控只保留单侧事实，不作阻断性比较。</div>'
    priorities = [item for item in analysis.get("commercial_priorities") or [] if isinstance(item, dict)][:6]
    priority_block = ""
    if priorities:
        priority_block = "\n".join(
            [
                '<div class="commercial-order">',
                '<div class="label">商业处理顺序</div>',
                '<ol>',
                *(
                    f'<li><strong>{escape(item.get("tier"))} · {escape(item.get("title"))}</strong>：{escape(item.get("summary"))}</li>'
                    for item in priorities
                ),
                '</ol>',
                '</div>',
            ]
        )
    return "\n".join(
        [
            '<div class="root-diagnosis-block">',
            "<h2>根本性问题</h2>",
            '<section class="root-diagnosis">',
            *rows,
            priority_block,
            reliability,
            "</section>",
            "</div>",
        ]
    )


def global_finding_title(gate_id: str) -> str:
    return {
        "selling_point_route": "主卖点路线",
        "focus_coherence": "产品焦点一致性",
        "attention_cleanliness": "画面注意力洁净度",
    }.get(gate_id, gate_id or "视频级问题")


def render_overview_cards(
    analysis: dict[str, Any],
    run_dir: Path,
    assets: ReportAssetContext | None = None,
) -> str:
    """概览仅保留三张卡：产品、标杆视频、达人视频。

    视频卡显示时长 + 代表帧缩略图，点击放大。
    （当前放大的是代表帧，不是视频播放；真正视频播放需要内嵌视频文件，体积大，留待后续。）
    """
    assets = assets or ReportAssetContext(run_dir)
    product = analysis.get("product", {})
    videos = analysis.get("videos", {})
    audio = analysis.get("audio_assessment") if isinstance(analysis.get("audio_assessment"), dict) else {}
    audio_boundary = (
        "口播内容来自转录；当前模型不直接评价音轨表现。"
        if audio.get("native_audio_analysis") is False
        else "语气、BGM、音效仅作观察，不参与差距等级。"
    )
    cards = [
        "\n".join(
            [
                '<div class="overview-card">',
                '<div class="label">产品</div>',
                f'<div class="value">{escape(product.get("name") or "未填写")}</div>',
                f'<div class="meta">{escape(audio_boundary)}</div>',
                "</div>",
            ]
        ),
        render_video_card("标杆视频", videos.get("benchmark", {}), assets),
        render_video_card("达人视频", videos.get("creator", {}), assets),
    ]
    return "\n".join(cards)


def render_video_card(label: str, info: dict[str, Any], assets: ReportAssetContext) -> str:
    duration = format_seconds(info.get("duration_seconds"))
    rows = [
        '<div class="overview-card">',
        f'<div class="label">{escape(label)}</div>',
        f'<div class="value">{escape(duration)}</div>',
    ]
    frame = representative_frame(info)
    if frame:
        image_src = image_src_for_frame(frame, assets)
        if image_src:
            rows.append('<div class="video-thumb">')
            rows.append(f'<img src="{escape(image_src)}" alt="{escape(label)}代表帧">')
            rows.append("</div>")
    rows.extend(render_audio_quality_rows(info.get("audio_quality")))
    rows.append("</div>")
    return "\n".join(rows)


def render_audio_quality_rows(value: Any) -> list[str]:
    quality = value if isinstance(value, dict) else {}
    if not quality:
        return []
    status = str(quality.get("status") or "unavailable")
    labels = {"ok": "音频技术质量正常", "warning": "音频质量需关注", "unavailable": "音频质量不可用"}
    rows = [f'<div class="meta">{escape(labels.get(status, "音频质量不可用"))}</div>']
    issues = quality.get("hard_issues") if isinstance(quality.get("hard_issues"), list) else []
    for issue in issues[:2]:
        if isinstance(issue, dict) and issue.get("message"):
            rows.append(f'<div class="meta">{escape(issue["message"])}</div>')
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    lufs = metrics.get("integrated_lufs")
    silence = metrics.get("silence_ratio")
    metric_parts = []
    if isinstance(lufs, (int, float)):
        metric_parts.append(f"{lufs:g} LUFS")
    if isinstance(silence, (int, float)):
        metric_parts.append(f"静音 {silence * 100:.0f}%")
    if metric_parts:
        rows.append(f'<div class="meta">{escape(" · ".join(metric_parts))}</div>')
    return rows


def representative_frame(info: dict[str, Any]) -> dict[str, Any] | None:
    # 取视频中段的一帧作为代表缩略图
    duration = info.get("duration_seconds")
    duration_value = parse_timestamp_seconds(duration)
    if duration_value is None or duration_value <= 0:
        return None
    mid = duration_value / 2
    return select_frame_near_timestamp(info, mid)


def render_key_conclusions(analysis: dict[str, Any]) -> str:
    """关键结论：一句话定性 + 最多 5 点短句结论。

    优先读 LLM 输出的 key_conclusions 数组（理想状态，由 prompt 迭代产出）；
    缺失时回退到 holistic_assessment 六维去重收敛为列表，避免六块重复内容。
    """
    verdict = str(analysis.get("one_line_verdict") or "").strip()
    rows = [
        '<div class="verdict">',
        '<div class="label">一句话定性</div>',
        f'<div class="verdict-value">{escape(verdict or "待模型按完整方法重新分析")}</div>',
        "</div>",
    ]

    conclusions = collect_key_conclusions(analysis)
    if conclusions:
        rows.append('<ol class="conclusion-list">')
        for idx, point in enumerate(conclusions, start=1):
            rows.append(f'<li><span class="conclusion-index">{idx}</span>{escape(point)}</li>')
        rows.append("</ol>")
    else:
        rows.append('<div class="meta">待完整分析后输出关键结论。</div>')
    return "\n".join(rows)


def collect_key_conclusions(analysis: dict[str, Any], limit: int = 5) -> list[str]:
    # 1) 优先用 LLM 的 key_conclusions 数组
    raw = analysis.get("key_conclusions")
    if isinstance(raw, list):
        points = [str(item).strip() for item in raw if str(item).strip()]
        if points:
            return points[:limit]
    # 2) 回退：holistic_assessment 六维去重收敛
    assessment = analysis.get("holistic_assessment", {})
    if not isinstance(assessment, dict):
        return []
    seen: set[str] = set()
    points = []
    for value in assessment.values():
        text = str(value or "").strip()
        if not text or text == "未完成评估。":
            continue
        key = re.sub(r"\W+", "", text)[:20]
        if key in seen:
            continue
        seen.add(key)
        points.append(text)
    return points[:limit]


def render_gap_overview(analysis: dict[str, Any]) -> str:
    rows = []
    result = AnalysisResult.from_mapping(analysis)
    for index, stage in enumerate(result.stages(), start=1):
        short_name, full_name = stage_display_names(stage.get("stage", ""), index)
        skipped, _ = stage_skipped(stage)
        if skipped:
            css_level = "skip"
            label = "不比较" if str(stage.get("comparison_status") or "") == "not_directly_comparable" else "未涉及"
        else:
            severity = severity_value(stage.get("severity"))
            if severity is None:
                css_level = "unknown"
                label = "未分析"
            else:
                css_level = {"large": "high", "medium": "mid", "small": "low"}[severity]
                label = {"large": "差距明显", "medium": "差距中等", "small": "差距较小"}[severity]
        rows.append(
            "\n".join(
                [
                    f'<a class="gap-tile gap-tile-{escape(css_level)}" href="#{escape(stage_anchor(index))}" aria-label="{escape(full_name)} {escape(label)}">',
                    f'<span class="gap-code">{escape(short_name)}</span>',
                    '<span class="gap-swatch" aria-hidden="true"></span>',
                    f'<span class="gap-stage-name">{escape(full_name)}</span>',
                    f'<span class="gap-level">{escape(label)}</span>',
                    "</a>",
                ]
            )
        )
    legend = '<div class="gap-legend">红 = 差距明显，橙 = 差距中等，绿 = 差距较小，灰 = 双方均未涉及。点击阶段查看证据和细节。</div>'
    return "\n".join(["<div class=\"gap-matrix\">", *rows, "</div>", legend])


def stage_anchor(index: int) -> str:
    return f"stage-{index}"


def stage_display_names(stage_name: Any, index: int) -> tuple[str, str]:
    text = str(stage_name or "").strip()
    short = f"S{index}"
    # 去掉可能的前缀：S1、_ 等
    if text.startswith(short):
        text = text[len(short):].strip()
    text = text.lstrip("_").strip()
    # 统一阶段中文名
    text = text.replace("产品引出", "引出").replace("使用过程", "使用").replace("效果呈现", "效果").replace("信任放大", "信任")
    return short, text or f"阶段 {index}"


def referenced_evidence_units(ids: Any, understanding: dict[str, Any]) -> list[dict[str, Any]]:
    references = {str(value) for value in ids} if isinstance(ids, list) else set()
    units = understanding.get("evidence_units", []) if isinstance(understanding, dict) else []
    return [unit for unit in units if isinstance(unit, dict) and str(unit.get("id")) in references]


def select_referenced_frames(
    info: dict[str, Any],
    units: list[dict[str, Any]],
    fallback_range: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        for frame in select_frames_for_time_range(info, str(unit.get("time_range") or ""), limit=1):
            path = str(frame.get("path") or "")
            if path not in seen:
                selected.append(frame)
                seen.add(path)
    return selected[:3] or select_frames_for_time_range(info, fallback_range, limit=3)


def evidence_visual_facts(units: list[dict[str, Any]]) -> list[str]:
    facts: list[str] = []
    for unit in units:
        for key in ("visual_fact", "subtitle_fact"):
            value = str(unit.get(key) or "").strip()
            if value:
                facts.append(value)
    return facts[:5]


def evidence_quotes(units: list[dict[str, Any]]) -> tuple[str, str]:
    local = [str(unit.get("voiceover") or "").strip() for unit in units]
    translated = [str(unit.get("voiceover_zh") or "").strip() for unit in units]
    return " ".join(item for item in local if item), " ".join(item for item in translated if item)


def render_support_badge(status: Any) -> str:
    labels = {
        "supported": ("supported", "口播与画面一致"),
        "voice_only": ("voice-only", "口播提及，画面未验证"),
        "visual_only": ("visual-only", "以画面/字幕为核心"),
        "conflict": ("conflict", "口播与画面不一致"),
    }
    css_name, label = labels.get(str(status or "").strip().lower(), labels["visual_only"])
    return f'<span class="support-status status-{escape(css_name)}">{escape(label)}</span>'


def render_fact_list(value: Any) -> str:
    facts = value if isinstance(value, list) else []
    if not facts:
        return '<div class="meta">无独立可验证画面证据。</div>'
    return "\n".join(["<ul class=\"visual-evidence\">", *(f"<li>{escape(item)}</li>" for item in facts), "</ul>"])


def render_quote(quote: Any, translation: Any) -> str:
    local_text = str(quote or "").strip()
    zh_text = str(translation or "").strip()
    if not local_text and not zh_text:
        return ""
    rows = ['<div class="quote">', '<div class="quote-label">口播证据</div>']
    if local_text:
        rows.append(f'<div class="quote-local">{escape(local_text)}</div>')
    if zh_text:
        rows.append(f'<div class="quote-zh">中文：{escape(zh_text)}</div>')
    rows.append("</div>")
    return "\n".join(rows)


def render_shot_grid(frames: list[dict[str, Any]], assets: ReportAssetContext) -> str:
    if not frames:
        return '<div class="meta">暂无对应画面。</div>'
    return "\n".join(["<div class=\"thumb-grid\">", *(render_thumb(frame, assets) for frame in frames), "</div>"])


def render_thumb(frame: dict[str, Any], assets: ReportAssetContext) -> str:
    image_src = image_src_for_frame(frame, assets)
    if not image_src:
        return '<div class="meta">暂无对应画面。</div>'
    timestamp = format_seconds(frame.get("timestamp_seconds"))
    label = frame.get("stage") or frame.get("label") or "画面"
    return "\n".join(
        [
            '<div class="thumb">',
            f'<img src="{escape(image_src)}" alt="{escape(label)} {escape(timestamp)}">',
            f'<div>{escape(timestamp)} · {escape(label)}</div>',
            "</div>",
        ]
    )


def render_shot(frame: dict[str, Any] | None, assets: ReportAssetContext) -> str:
    if not frame or not frame.get("path"):
        return '<div class="meta">暂无对应画面。</div>'
    image_src = image_src_for_frame(frame, assets)
    if not image_src:
        return '<div class="meta">暂无对应画面。</div>'
    timestamp = format_seconds(frame.get("timestamp_seconds"))
    label = frame.get("stage") or frame.get("label") or "画面"
    return "\n".join(
        [
            '<div class="shot">',
            f'<img src="{escape(image_src)}" alt="{escape(label)} {escape(timestamp)}">',
            f'<div class="shot-caption">{escape(timestamp)} · {escape(label)}</div>',
            "</div>",
        ]
    )


def image_src_for_frame(frame: dict[str, Any], assets: ReportAssetContext | None = None) -> str:
    # Without an explicit run context, do not trust a result-provided path.
    return assets.image_src(frame) if assets is not None else ""


def render_improvement_cards(
    analysis: dict[str, Any],
    assets: ReportAssetContext | None = None,
) -> str:
    assets = assets or ReportAssetContext(Path(str(analysis.get("run_dir") or ".")))
    result = AnalysisResult.from_mapping(analysis)
    improvements = sorted(result.improvements(), key=lambda item: item.get("priority", 999))
    if not improvements:
        # 根据 improvements_status 给出不同提示，避免空数据被误读为"无问题"
        status = result.get("improvements_status", "not_applicable")
        if status == "llm_unavailable":
            return (
                '<div class="card warning">'
                "LLM 分析未运行或失败，本次报告无可展示的提升点。"
                "请检查 --llm-model / API key 配置后重跑。"
                "</div>"
            )
        if status == "llm_completed":
            return '<div class="card">LLM 本次未给出提升点建议。</div>'
        return '<div class="card">拆解模式暂无对比提升点。</div>'

    cards = []
    for rank, item in enumerate(improvements, start=1):
        creator_range = str(item.get("creator_time_range") or "")
        base_frame_time = item.get("best_base_frame_time") or ""
        creator_frame = select_base_frame(analysis["videos"].get("creator", {}), item)
        cards.append(
            "\n".join(
                [
                    '<div class="card improvement">',
                    '<div class="improvement-main">',
                    '<div class="improvement-heading">',
                    f'<span class="priority">优先 {rank}</span>',
                    f"<h3>{escape(item['title'])}</h3>",
                    "</div>",
                    render_improvement_meta(item, creator_range),
                    "<ol>",
                    *render_list_items(item.get("actions") or item.get("suggestion", ""), limit=3),
                    "</ol>",
                    render_expected_effect(item),
                    render_script_block(item),
                    "</div>",
                    '<div class="material-reference">',
                    "<h3>素材参考</h3>",
                    render_material_reference(item, creator_frame, base_frame_time, assets),
                    render_base_frame_reason(item.get("base_frame_reason")),
                    "</div>",
                    "</div>",
                ]
            )
        )
    return "\n".join(cards)


def render_improvement_meta(item: dict[str, Any], creator_range: str) -> str:
    gap_types = {"structural": "结构性", "execution": "执行性", "resource": "资源性"}
    fields = [
        f"改造槽位：{item.get('target_stage') or '待确认'}",
        f"达人位置：{creator_range or '待确认'}",
        f"成交影响：{item.get('gmv_impact') or '待评估'}",
        f"差距类型：{gap_types.get(item.get('gap_type'), '待确认')}",
    ]
    roots = [global_finding_title(str(root)) for root in item.get("root_cause_ids") or [] if str(root).strip()]
    root_note = f'<div class="cause-note"><strong>根因关联：</strong>{escape("、".join(roots))}</div>' if roots else ""
    return f'<div class="improvement-meta">{escape(" · ".join(fields))}</div>{root_note}'


def render_expected_effect(item: dict[str, Any]) -> str:
    value = str(item.get("expected_effect") or item.get("gmv_reason") or "").strip()
    if not value:
        return ""
    return f'<div class="expected-effect"><span class="label-inline">预期改善：</span>{escape(value)}</div>'


def select_base_frame(info: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("base_frame_suitability") == "no_suitable_frame":
        return None
    best_time = item.get("best_base_frame_time")
    if best_time:
        return select_frame_near_timestamp(info, best_time)
    return None


def render_material_reference(
    item: dict[str, Any],
    frame: dict[str, Any] | None,
    base_frame_time: str,
    assets: ReportAssetContext,
) -> str:
    return render_base_frame(item, frame, base_frame_time, assets)


def render_base_frame(
    item: dict[str, Any],
    frame: dict[str, Any] | None,
    base_frame_time: str,
    assets: ReportAssetContext,
) -> str:
    if item.get("base_frame_suitability") == "no_suitable_frame" or not frame:
        return '<div class="material-needed">当前达人素材无合适基底帧，需补拍或补充素材。</div>'
    return "\n".join(
        [
            f'<div class="meta">达人基底帧：{escape(base_frame_time)} · 依据 {escape(item.get("base_frame_evidence_id") or "待确认")}</div>',
            render_shot(frame, assets),
        ]
    )


def render_script_block(item: dict[str, Any]) -> str:
    script = str(item.get("creator_script") or "").strip()
    script_zh = str(item.get("creator_script_zh") or "").strip()
    if not script and not script_zh:
        return ""
    rows = ['<div class="script-box">']
    if script:
        rows.append(f"<div><strong>建议话术：</strong>{escape(script)}</div>")
    if script_zh:
        rows.append(f"<div class=\"meta\">中文：{escape(script_zh)}</div>")
    rows.append("</div>")
    return "\n".join(rows)


def render_base_frame_reason(value: Any) -> str:
    reason = str(value or "").strip()
    if not reason:
        return ""
    return f'<div class="base-reason">{escape(reason)}</div>'


def severity_value(value: Any) -> str | None:
    severity = str(value or "").strip().lower()
    return severity if severity in {"large", "medium", "small"} else None


def normalize_severity(value: Any) -> str:
    """Normalize legacy completed results; unavailable values stay out of rendering paths."""
    return severity_value(value) or "medium"


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)
