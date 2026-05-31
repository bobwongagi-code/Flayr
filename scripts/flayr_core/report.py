"""HTML report rendering for Flayr."""

from __future__ import annotations

import base64
import html
import mimetypes
import re
from pathlib import Path
from typing import Any

from .artifacts import format_seconds, select_frame_near_timestamp, select_frames_for_time_range


ROOT = Path(__file__).resolve().parents[2]
REPORT_TEMPLATE = ROOT / "assets" / "report.html"


def write_report(run_dir: Path, analysis: dict[str, Any], plan: dict[str, Any] | None) -> Path:
    template = REPORT_TEMPLATE.read_text(encoding="utf-8")
    report = template
    report = report.replace("{{generated_at}}", escape(format_generated_at(analysis.get("generated_at"))))
    report = report.replace("{{executive_summary}}", escape(executive_summary(analysis)))
    report = report.replace("{{overview_cards}}", render_overview_cards(analysis, run_dir))
    report = report.replace("{{holistic_assessment}}", render_key_conclusions(analysis))
    report = report.replace("{{gap_overview}}", render_gap_overview(analysis))
    report = report.replace("{{stage_rows}}", render_stage_rows(analysis))
    report = report.replace("{{improvement_cards}}", render_improvement_cards(analysis))

    report_path = run_dir / "report.html"
    report_path.write_text(report, encoding="utf-8")
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


def render_stage_rows(analysis: dict[str, Any]) -> str:
    rows = []
    stages = analysis["stage_analysis"]
    benchmark_info = analysis["videos"].get("benchmark", {})
    creator_info = analysis["videos"].get("creator", {})
    understanding = analysis.get("video_understanding", {})
    for index, stage in enumerate(stages, start=1):
        skipped, reason = stage_skipped(stage)
        if skipped:
            rows.append(render_skipped_stage(stage, index, reason))
            continue

        creator_range = stage.get("creator_time_range") or stage.get("time_range", "")
        benchmark_range = stage.get("benchmark_time_range") or stage.get("time_range", "")
        creator_cells = role_cells(stage, "creator", creator_info, creator_range, understanding.get("creator", {}))
        benchmark_cells = role_cells(stage, "benchmark", benchmark_info, benchmark_range, understanding.get("benchmark", {}))

        parts = [
            f'<div class="stage" id="{escape(stage_anchor(index))}">',
            # 跨两列的阶段头：阶段名 + 差距等级
            '<div class="stage-header">',
            f"<h3>{escape(stage['stage'])}</h3>",
            render_gap_badge(stage.get("severity", "medium")),
            "</div>",
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


def stage_skipped(stage: dict[str, Any]) -> tuple[bool, str]:
    """判断阶段是否双方都未设计（如 S5 都无信任背书），如是则折叠不展开分析。"""
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
        # 检查是否有实质信任信号（认证、背书、权威信息等）
        trust_signals = ("认证", "背书", "权威", "批准", "临床", "医生", "专家", "测试", "检测",
                         "certif", "approve", "doctor", "clinical", "test", "trust",
                         "推荐", "获奖", "专利", "实验室")
        has_trust = any(s in creator or s in benchmark for s in trust_signals)
        if not has_trust:
            return True, "双方均未设计信任背书环节，不单独分析。"

    if (creator_empty and benchmark_empty) or gap_skip:
        return True, "双方均未设计该环节，不单独分析。"
    return False, ""


def render_skipped_stage(stage: dict[str, Any], index: int, reason: str) -> str:
    return "\n".join(
        [
            f'<div class="stage stage-skipped" id="{escape(stage_anchor(index))}">',
            '<div class="stage-header">',
            f"<h3>{escape(stage['stage'])}</h3>",
            '<span class="gap-badge gap-low"><span>未涉及</span></span>',
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
    shot_cell = render_shot_grid(frames)
    visual_cell = render_fact_list(visual_evidence)
    return [conclusion_cell, quote_cell, shot_cell, visual_cell]


def render_gap_badge(severity: str) -> str:
    labels = {
        "large": ("high", "差距明显"),
        "medium": ("mid", "差距中等"),
        "small": ("low", "差距较小"),
    }
    normalized = normalize_severity(severity)
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
    summary = str(analysis.get("one_line_summary") or analysis.get("executive_summary") or "").strip()
    if summary:
        return summary
    improvements = sorted(analysis.get("improvements", []), key=lambda item: item.get("priority", 999))
    if improvements:
        return f"达人视频优先改：{improvements[0].get('title', '核心成交阻力')}。"
    large_stages = [
        item.get("stage", "")
        for item in analysis.get("stage_analysis", [])
        if item.get("severity") == "large"
    ]
    if large_stages:
        return f"达人视频最大差距集中在：{'、'.join(large_stages[:2])}。"
    return "当前报告已完成阶段拆解，建议优先查看 Top 提升点。"


def render_overview_cards(analysis: dict[str, Any], run_dir: Path) -> str:
    """概览仅保留三张卡：产品、标杆视频、达人视频。

    视频卡显示时长 + 代表帧缩略图，点击放大。
    （当前放大的是代表帧，不是视频播放；真正视频播放需要内嵌视频文件，体积大，留待后续。）
    """
    product = analysis.get("product", {})
    videos = analysis.get("videos", {})
    cards = [
        "\n".join(
            [
                '<div class="overview-card">',
                '<div class="label">产品</div>',
                f'<div class="value">{escape(product.get("name") or "未填写")}</div>',
                "</div>",
            ]
        ),
        render_video_card("标杆视频", videos.get("benchmark", {})),
        render_video_card("达人视频", videos.get("creator", {})),
    ]
    return "\n".join(cards)


def render_video_card(label: str, info: dict[str, Any]) -> str:
    duration = format_seconds(info.get("duration_seconds"))
    rows = [
        '<div class="overview-card">',
        f'<div class="label">{escape(label)}</div>',
        f'<div class="value">{escape(duration)}</div>',
    ]
    frame = representative_frame(info)
    if frame:
        image_src = image_src_for_frame(frame)
        if image_src:
            rows.append('<div class="video-thumb">')
            rows.append(f'<img src="{escape(image_src)}" alt="{escape(label)}代表帧">')
            rows.append("</div>")
    rows.append("</div>")
    return "\n".join(rows)


def representative_frame(info: dict[str, Any]) -> dict[str, Any] | None:
    # 取视频中段的一帧作为代表缩略图
    duration = info.get("duration_seconds")
    mid = float(duration) / 2 if isinstance(duration, (int, float)) and duration else 1.0
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
    for index, stage in enumerate(analysis.get("stage_analysis", []), start=1):
        short_name, full_name = stage_display_names(stage.get("stage", ""), index)
        skipped, _ = stage_skipped(stage)
        if skipped:
            css_level, label = "skip", "未涉及"
        else:
            severity = normalize_severity(stage.get("severity"))
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


def render_shot_grid(frames: list[dict[str, Any]]) -> str:
    if not frames:
        return '<div class="meta">暂无对应画面。</div>'
    return "\n".join(["<div class=\"thumb-grid\">", *(render_thumb(frame) for frame in frames), "</div>"])


def render_thumb(frame: dict[str, Any]) -> str:
    image_src = image_src_for_frame(frame)
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


def render_shot(frame: dict[str, Any] | None) -> str:
    if not frame or not frame.get("path"):
        return '<div class="meta">暂无对应画面。</div>'
    image_src = image_src_for_frame(frame)
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


def image_src_for_frame(frame: dict[str, Any]) -> str:
    path = Path(str(frame.get("path") or ""))
    if not path.exists() or not path.is_file():
        return ""
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def render_improvement_cards(analysis: dict[str, Any]) -> str:
    improvements = sorted(analysis["improvements"], key=lambda item: item.get("priority", 999))
    if not improvements:
        # 根据 improvements_status 给出不同提示，避免空数据被误读为"无问题"
        status = analysis.get("improvements_status", "not_applicable")
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

    proposal_units = proposal_units_by_rank(analysis)
    cards = []
    for rank, item in enumerate(improvements, start=1):
        creator_range = item.get("creator_time_range") or item.get("time_range", "")
        base_frame_time = item.get("best_base_frame_time") or ""
        creator_frame = select_base_frame(analysis["videos"].get("creator", {}), item)
        proposal_unit = proposal_units.get(rank)
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
                    render_proposal_unit(proposal_unit),
                    "</div>",
                    '<div class="ai-reference">',
                    "<h3>AI 改造参考</h3>",
                    render_ai_visual(item, creator_frame, base_frame_time),
                    render_base_frame_reason(item.get("base_frame_reason")),
                    '<div class="prompt-label">改造方向</div>',
                    f'<div class="prompt">{escape(item.get("aigc_prompt") or aigc_prompt_fallback(item))}</div>',
                    "</div>",
                    "</div>",
                ]
            )
        )
    return "\n".join(cards)


def proposal_units_by_rank(analysis: dict[str, Any]) -> dict[int, dict[str, Any]]:
    proposal = analysis.get("proposal_clips", {})
    units = proposal.get("units", []) if isinstance(proposal, dict) else []
    result: dict[int, dict[str, Any]] = {}
    for unit in units:
        if not isinstance(unit, dict):
            continue
        try:
            rank = int(unit.get("rank"))
        except (TypeError, ValueError):
            continue
        result[rank] = unit
    return result


def render_proposal_unit(unit: dict[str, Any] | None) -> str:
    if not unit:
        return ""
    original_uri = str(unit.get("clip_original_uri") or "").strip()
    ai_uri = str(unit.get("clip_ai_uri") or "").strip()
    rows = [
        '<div class="proposal-unit">',
        '<div class="proposal-head">',
        "<h4>提案样片</h4>",
        f'<span class="proposal-chip">{escape(proposal_status_label(unit))}</span>',
        "</div>",
        '<div class="proposal-grid">',
        render_video_slot("达人原片", original_uri, unit.get("duration_sec")),
        render_video_slot("AI 示意", ai_uri, unit.get("duration_sec"), placeholder=proposal_ai_placeholder(unit)),
        "</div>",
        '<div class="proposal-copy">',
        f'<div><span class="label-inline">本地话术：</span>{escape(unit.get("line") or "待补充")}</div>',
    ]
    if unit.get("line_zh"):
        rows.append(f'<div class="meta">中文：{escape(unit.get("line_zh"))}</div>')
    rows.extend(
        [
            f'<div class="proposal-rationale"><span class="label-inline">改造理由：</span>{escape(unit.get("rationale") or "待补充")}</div>',
            "</div>",
            "</div>",
        ]
    )
    return "\n".join(rows)


def proposal_status_label(unit: dict[str, Any]) -> str:
    status = str(unit.get("ai_generation_status") or "").strip()
    if status == "ready":
        return "AI 示意已生成"
    if status == "submitted":
        return "AI 任务已提交"
    return "需达人确认"


def proposal_ai_placeholder(unit: dict[str, Any]) -> str:
    status = str(unit.get("ai_generation_status") or "").strip()
    if status == "submitted":
        return f"AI 任务已提交，task_id：{unit.get('ai_task_id') or '待查询'}。"
    error = str(unit.get("ai_generation_error") or "").strip()
    if error:
        return f"AI 示意暂未生成：{error}"
    return "AI 样片后端未配置，本次先展示原片切片 + 改造文案。"


def render_video_slot(label: str, uri: str, duration: Any, placeholder: str = "暂无样片。") -> str:
    rows = ['<div class="proposal-video-slot">', f'<div class="label">{escape(label)}</div>']
    if uri:
        rows.append(
            f'<video class="proposal-video" controls preload="metadata" src="{escape(uri)}"></video>'
        )
        rows.append(f'<div class="proposal-caption">{escape(format_seconds(duration))}</div>')
    else:
        rows.append(f'<div class="proposal-empty">{escape(placeholder)}</div>')
    rows.append("</div>")
    return "\n".join(rows)


def render_improvement_meta(item: dict[str, Any], creator_range: str) -> str:
    gap_types = {"structural": "结构性", "execution": "执行性", "resource": "资源性"}
    fields = [
        f"改造槽位：{item.get('target_stage') or '待确认'}",
        f"达人位置：{creator_range or '待确认'}",
        f"成交影响：{item.get('gmv_impact') or '待评估'}",
        f"差距类型：{gap_types.get(item.get('gap_type'), '待确认')}",
    ]
    return f'<div class="improvement-meta">{escape(" · ".join(fields))}</div>'


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


def render_ai_visual(item: dict[str, Any], frame: dict[str, Any] | None, base_frame_time: str) -> str:
    generated_path = Path(str(item.get("aigc_image_path") or ""))
    if generated_path.is_file():
        return "\n".join(
            [
                '<div class="meta">AI 构图参考图 · 包装文字与信息以原实拍为准</div>',
                render_generated_shot(generated_path),
            ]
        )
    return render_base_frame(item, frame, base_frame_time)


def render_generated_shot(path: Path) -> str:
    image_src = image_src_for_frame({"path": str(path)})
    if not image_src:
        return '<div class="meta">AI 改造图文件不可读取。</div>'
    return "\n".join(
        [
            '<div class="shot">',
            f'<img src="{escape(image_src)}" alt="AI 改造效果图">',
            '<div class="shot-caption">AI 构图参考图</div>',
            "</div>",
        ]
    )


def render_base_frame(item: dict[str, Any], frame: dict[str, Any] | None, base_frame_time: str) -> str:
    if item.get("base_frame_suitability") == "no_suitable_frame" or not frame:
        return '<div class="material-needed">当前达人素材无合适基底帧，需补拍或补充素材。</div>'
    return "\n".join(
        [
            f'<div class="meta">达人基底帧：{escape(base_frame_time)} · 依据 {escape(item.get("base_frame_evidence_id") or "待确认")}</div>',
            render_shot(frame),
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


def aigc_prompt_fallback(item: dict[str, Any]) -> str:
    suggestion = str(item.get("suggestion") or "").strip()
    return f"以达人该时间段原始画面为素材，保留真实人物、产品和场景，按以下方向改造：{suggestion}"


def normalize_severity(value: Any) -> str:
    severity = str(value or "medium").strip().lower()
    if severity not in {"large", "medium", "small"}:
        return "medium"
    return severity


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)
