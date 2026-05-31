"""flayr_core.llm.payload：LLM 请求 payload 构造。

每个 build_*_payload 都返回 OpenAI 兼容的 chat completions 请求体。
不调用 LLM、不解析响应，纯粹组装文本 + 图片 + system prompt。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import (
    format_seconds,
    get_focus_frame_entries,
    get_frame_entries,
    parse_time_range_seconds,
    sample_evenly,
    select_frames_for_time_range,
)
from .api import audio_to_mp3_data_url, image_to_data_url, video_to_data_url

ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# 视觉素材选取（喂给多模态 LLM 的关键帧）
# ---------------------------------------------------------------------------

def select_role_visual_inputs(info: dict[str, Any], role: str, image_limit: int) -> list[dict[str, str]]:
    """为单视频事实抽取选关键帧，最多 image_limit 张。"""
    selected: list[dict[str, str]] = []
    for entry in get_llm_frame_candidates(info, image_limit):
        frame = Path(str(entry.get("path", "")))
        if not frame.exists():
            continue
        timestamp = format_seconds(entry.get("timestamp_seconds"))
        selected.append(
            {
                "role": role,
                "path": str(frame),
                "label": f"{role} {entry.get('stage') or entry.get('label', 'frame')} @ {timestamp} {frame.name}",
                "data_url": image_to_data_url(frame),
            }
        )
    return selected[:image_limit]


def select_llm_visual_inputs(analysis: dict[str, Any], image_limit: int) -> list[dict[str, str]]:
    """跨视频选关键帧（同时含 benchmark 和 creator）。"""
    if image_limit <= 0:
        return []

    videos = analysis.get("videos", {})
    roles = [role for role in ("benchmark", "creator") if role in videos]
    if not roles:
        return []

    per_role_limit = max(1, image_limit // len(roles))
    selected: list[dict[str, str]] = []
    for role in roles:
        entries = get_llm_frame_candidates(videos[role], per_role_limit)
        for entry in entries[:per_role_limit]:
            frame = Path(str(entry.get("path", "")))
            if not frame.exists():
                continue
            timestamp = format_seconds(entry.get("timestamp_seconds"))
            selected.append(
                {
                    "role": role,
                    "path": str(frame),
                    "label": f"{role} {entry.get('stage') or entry.get('label', 'frame')} @ {timestamp} {frame.name}",
                    "data_url": image_to_data_url(frame),
                }
            )

    if len(selected) < image_limit:
        used_paths = {item["path"] for item in selected}
        for role in roles:
            entries = get_llm_frame_candidates(videos[role], image_limit)
            for entry in entries:
                if len(selected) >= image_limit:
                    break
                frame = Path(str(entry.get("path", "")))
                if not frame.exists():
                    continue
                if str(frame) in used_paths:
                    continue
                timestamp = format_seconds(entry.get("timestamp_seconds"))
                selected.append(
                    {
                        "role": role,
                        "path": str(frame),
                        "label": f"{role} {entry.get('stage') or entry.get('label', 'frame')} @ {timestamp} {frame.name}",
                        "data_url": image_to_data_url(frame),
                    }
                )
                used_paths.add(str(frame))

    return selected[:image_limit]


def get_llm_frame_candidates(info: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """从一个视频的全片帧 + 加密 focus 帧中选候选帧，去重后返回。"""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    focus_limit = 2 if limit >= 6 else 0
    timeline_limit = max(1, limit - focus_limit)
    timeline_entries = sample_evenly(get_frame_entries(info), timeline_limit)
    focus_entries = sample_evenly(get_focus_frame_entries(info), focus_limit)
    for entry in timeline_entries + focus_entries:
        path = str(entry.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        candidates.append(entry)
    return candidates


def read_text_if_exists(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip() if path.is_file() else "（缺失）"


# ---------------------------------------------------------------------------
# Payload 构造
# ---------------------------------------------------------------------------

def build_video_fact_payload(
    model: str,
    role: str,
    analysis: dict[str, Any],
    visual_inputs: list[dict[str, str]],
) -> dict[str, Any]:
    """单视频事实抽取请求 payload。

    主路径（omni + ffmpeg 可用）：把原生视频（重编码 fps=3 + 降分辨率，含完整音轨）
    直接喂给模型，让它像人一样看连续画面 + 听声音，自定位变化点。
    降级路径（无 ffmpeg 或视频转码失败）：回退到关键帧抽帧 + 完整音频，
    沿用 visual_inputs（由 select_role_visual_inputs 提供）。
    """
    info = analysis.get("videos", {}).get(role, {})
    code = "B" if role == "benchmark" else "C"
    role_dir = Path(str(info.get("work_dir") or ""))

    # 优先走原生视频；失败则降级为抽帧。
    video_path = Path(str(info.get("path") or ""))
    video_data_url = video_to_data_url(video_path) if video_path.is_file() else None
    native_video = video_data_url is not None

    visual_source_hint = (
        "随请求附带本视频的原生画面（已抽帧为连续序列）和完整音轨。"
        if native_video
        else "随请求附带本视频的若干关键帧和完整音频。"
    )

    text = "\n\n".join(
        [
            f"# 单视频事实抽取：{role}",
            "",
            f"- 产品：{analysis.get('product', {}).get('name') or '未填写'}",
            f"- 原视频：{info.get('path') or ''}",
            f"- 时长：{format_seconds(info.get('duration_seconds'))}",
            "",
            "## 本地语言转写",
            read_text_if_exists(role_dir / "transcript.txt"),
            "",
            "## 带时间戳口播分段（口播时间归因的权威依据）",
            read_text_if_exists(role_dir / "transcript.srt"),
            "",
            "## 中文翻译",
            read_text_if_exists(role_dir / "transcript.zh.txt"),
            "",
            "## 输出 JSON",
            json.dumps(
                {
                    "content_summary": "只概括这条视频，不比较另一条视频。",
                    "communication_strategy": "只描述这条视频的口播、字幕、画面、BGM如何配合推进。",
                    "evidence_units": [
                        {
                            "id": f"{code}1",
                            "time_range": "0.0s - 3.0s",
                            "information": "该变化点实际传递的信息，不做 S1-S6 阶段推断。",
                            "voiceover": "只能摘录本视频 transcript.srt 中真实出现的原句；没有则留空。",
                            "voiceover_zh": "中文翻译；没有则留空。",
                            "visual_fact": "该时刻画面中实际可见的事实：主体、动作、表情变化、字幕叠字、特效。",
                            "subtitle_fact": "可读字幕；没有则留空。",
                            "audio_fact": "该时刻的 BGM（有/无、风格情绪）、口播语气（热情/平淡/亲和）、特殊音效；无则写无。",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if native_video:
        content.append(
            {"type": "video_url", "video_url": {"url": video_data_url}}
        )
    else:
        for item in visual_inputs:
            content.extend(
                [
                    {"type": "text", "text": f"图片：{item['label']}，本地路径：{item['path']}"},
                    {"type": "image_url", "image_url": {"url": item["data_url"], "detail": "low"}},
                ]
            )
        audio_data_url = audio_to_mp3_data_url(role_dir / "audio.wav")
        if audio_data_url is not None:
            content.append(
                {"type": "text", "text": "以下是本视频的完整音频，用于判断 BGM、口播语气、特殊音效。"}
            )
            content.append(
                {"type": "input_audio", "input_audio": {"data": audio_data_url, "format": "mp3"}}
            )

    system_prompt = (
        "你是单视频事实抽取器。只输出严格 JSON，不要 Markdown。"
        "只分析当前这一条视频，禁止引用、比较或猜测另一条视频。"
        f"{visual_source_hint}"
        "你能同时看到连续画面、听到声音。请像人一样观看：沿时间线找出所有关键变化点"
        "（转场、产品出现、表情突变、字幕高亮、特效、情绪转折、BGM 起落），"
        "在变化点处切分 evidence_units，输出 4 到 8 条，沿时间线排列，id 必须使用指定前缀，"
        "time_range 用真实时间（如 2.5s - 4.0s）。"
        "每条都要据实填 visual_fact（画面/表情/字幕/特效）和 audio_fact（BGM/语气/音效）；"
        "voiceover 必须逐字来自当前视频 transcript.srt，画面看不清的时段在 visual_fact 写画面证据不足待复核；"
        "无 BGM 或无明显音效时 audio_fact 写无，不要臆造。"
        "不得臆造牙齿前后对比、用户评论、证书、检测报告、认证、价格、优惠或功效。"
    )

    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.0,
    }


def build_evidence_sensory_inputs(
    analysis: dict[str, Any],
    facts: dict[str, Any],
    frames_per_unit: int = 1,
) -> list[dict[str, Any]]:
    """为阶段二对比判断准备"感官素材"：给每条 evidence_unit 配关键帧 + 切片音频。

    Phase B 核心：让判断环节能"看着证据、听着声音"做比较，而不是只读 facts 文字。
    - 关键帧：按 evidence 的 time_range 从该 role 的全片帧中取（带 MM:SS 标注）；
    - 音频：按 time_range 切对应时间窗（声画对齐，不丢整条）；
    严格只服务"已存在的 evidence"，不新增事实单元（防串供基线不动）。
    """
    content: list[dict[str, Any]] = []
    videos = analysis.get("videos", {})
    for role in ("benchmark", "creator"):
        role_facts = facts.get(role) or {}
        units = role_facts.get("evidence_units") or []
        info = videos.get(role) or {}
        audio_path = Path(str(info.get("work_dir") or "")) / "audio.wav"
        duration = info.get("duration_seconds")
        for unit in units:
            uid = str(unit.get("id") or "")
            time_range = str(unit.get("time_range") or "")
            if not uid or not time_range:
                continue
            start, end = parse_time_range_seconds(time_range, duration)
            label = f"{role} {uid} @ {time_range}"
            # 关键帧
            frames = select_frames_for_time_range(info, time_range, limit=frames_per_unit)
            for fr in frames:
                frame_path = Path(str(fr.get("path") or ""))
                if not frame_path.is_file():
                    continue
                content.append({"type": "text", "text": f"【{label}｜画面帧】"})
                content.append(
                    {"type": "image_url", "image_url": {"url": image_to_data_url(frame_path), "detail": "low"}}
                )
            # 切片音频（声画对齐）
            seg = audio_to_mp3_data_url(audio_path, start=start, duration=max(0.1, end - start))
            if seg is not None:
                content.append({"type": "text", "text": f"【{label}｜该时段音频】"})
                content.append({"type": "input_audio", "input_audio": {"data": seg, "format": "mp3"}})
    return content


def build_llm_comparison_payload(
    model: str,
    analysis_input: str,
    facts: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """基于已校验的单视频事实清单做对比分析的 payload。

    Phase B：facts 文字仍是唯一事实源（防串供基线），但额外附上每条 evidence 的
    关键帧 + 切片音频，让判断环节能"看着证据、听着声音"评估声画质感与情绪强度，
    从而给出更准的 severity 和对比结论。感官素材不可用于新增/改写事实。
    """
    context = extract_comparison_context(analysis_input)
    commercial_framework = read_text_if_exists(ROOT / "references" / "commercial-judgement-framework.md")
    market_knowledge = read_text_if_exists(ROOT / "references" / "market-knowledge-my.md")
    user_text = "\n\n".join(
        [
            context,
            "## 商业评判框架（判断差距权重的方法）",
            commercial_framework,
            "## 目标市场知识库（仅作判断依据，不在报告呈现）",
            (
                "目标市场未确认时，以下 SEA/MY seed 仅作文化视角和误判防护提示；"
                "发现明确马来语或马来市场信号时可提高权重，但不得当作已确认事实。\n\n"
                + market_knowledge
            ),
            "## 已校验单视频事实清单（唯一事实来源）",
            json.dumps(facts, ensure_ascii=False, indent=2),
            "## 输出要求",
            "只输出严格 JSON，不要 Markdown。字段必须使用 references/analysis-output-schema.json 的字段名。",
            "必须输出：one_line_verdict, one_line_summary, executive_summary, holistic_assessment（每维独立）, key_conclusions（1-5 条消费者视角）, product_visibility, loop_closure, video_understanding, stage_analysis[6], improvements（1-5 条，按 GMV 杠杆排序）。",
            "stage_analysis 每项必须含：stage, time_range, benchmark_time_range, creator_time_range, core_question, creator_module_id, benchmark_module_id, module_fit, module_fit_reason, task_completion, gap_type, gap_summary, voice_performance, benchmark_summary, benchmark_key_message, benchmark_evidence_ids, benchmark_visual_evidence, benchmark_support_status, benchmark_quote, benchmark_quote_zh, creator_summary, creator_key_message, creator_evidence_ids, creator_visual_evidence, creator_support_status, creator_quote, creator_quote_zh, gap, evidence, severity。",
            "improvements 每项必须含：title,target_stage,gmv_impact,gap_type,time_range,creator_time_range,benchmark_time_range,problem,benchmark_reference,benchmark_evidence_ids,suggestion,actions,gmv_reason,evidence,creator_script,creator_script_zh,base_frame_suitability,best_base_frame_time,base_frame_evidence_id,base_frame_reason,aigc_prompt,aigc_image_path,expected_effect,priority。",
            "所有数组最多 1 条。所有描述字段最多一句且不超过 40 个汉字。video_understanding 必须原样使用事实清单，不得新增、改写或跨视频移动 evidence_units。",
        ]
    )
    payload = build_llm_payload(model, user_text, [])
    # temperature=0：对比判断要可复现，消除 severity 在边界 case（如 S3）上的抖动。
    payload["temperature"] = 0.0
    # 16384：观察到 8192 在 qwen-vl-max-latest 下被截断（中文 schema + 完整 stage_analysis + 多条 improvements）。
    # qwen 支持到 16K-32K，提到 16K 留出余量。如还不够可继续调，或在 prompt 里强制精简。
    payload["max_tokens"] = 16384

    # Phase B：把每条 evidence 的关键帧 + 切片音频挂到 user message（增强判断的感官输入）。
    if analysis is not None:
        sensory = build_evidence_sensory_inputs(analysis, facts)
        if sensory:
            user_msg = payload["messages"][1]
            base_text = user_msg["content"] if isinstance(user_msg["content"], str) else ""
            user_msg["content"] = [
                {"type": "text", "text": base_text},
                {
                    "type": "text",
                    "text": (
                        "## 各 evidence 对应的画面帧与切片音频（仅辅助判断声画质感，不可据此新增或改写事实）\n"
                        "下面按 role 和 evidence id 附上对应时段的关键帧与音频。"
                        "请按 S1-S6 功能阶段自行对齐两条视频的 evidence 做横向对比。"
                    ),
                },
                *sensory,
            ]

    payload["messages"][0]["content"] += (
        "本次对比分析的唯一事实来源是用户提供的已校验单视频事实清单；"
        "不得新增 evidence_unit，不得改写口播，不得把 benchmark 与 creator 的口播或画面互换。"
        "\n\n## 感官素材使用规则（Phase B）\n"
        "随请求附带了每条 evidence 对应时段的关键帧和切片音频。"
        "帧和音频仅用于评估已有 evidence 的声画质感、强度与情绪，以支撑 severity 和对比结论；"
        "不得据此新增或改写 facts 的事实单元；"
        "当帧/音频与 facts 文字描述冲突时以 facts 为准，可在该处判断理由中标注'此处存在感知歧义'。"
        "请按 S1-S6 功能阶段对齐两条视频的 evidence 再做横向对比，不要按绝对时间对齐。"
    )
    return payload


def extract_comparison_context(analysis_input: str) -> str:
    """从 analysis_input.md 中提取"分析等级"和"产品信息"两段。"""
    sections = []
    for heading in ("## 分析等级与结论边界", "## 产品信息"):
        pattern = rf"{re.escape(heading)}\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, analysis_input, flags=re.S)
        if match:
            sections.append(f"{heading}\n{match.group(1).strip()}")
    return "\n\n".join(sections) or "## 产品信息\n（缺失）"


def build_llm_payload(
    model: str,
    analysis_input: str,
    visual_inputs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """通用对比分析 payload。"""
    user_content: str | list[dict[str, Any]]
    if visual_inputs:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"{analysis_input}\n\n"
                    "## 随请求附带的关键帧\n\n"
                    "以下图片覆盖爆款/达人视频的全片时间线，并额外包含 Hook/CTA 加密关键帧。"
                    "必须先浏览全片时间线，再识别每个视频自己的 S1-S6 阶段边界。"
                    "不要因为参考结构里的常见秒数，把长视频的中后段误判成早期阶段。"
                ),
            }
        ]
        for item in visual_inputs:
            content.extend(
                [
                    {"type": "text", "text": f"图片：{item['label']}，本地路径：{item['path']}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": item["data_url"],
                            "detail": "low",
                        },
                    },
                ]
            )
        user_content = content
    else:
        user_content = analysis_input

    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr 的 TikTok Shop 带货短视频分析器。"
                    "只输出严格 JSON，不要 Markdown，不要解释。"
                    "建议必须围绕 GMV、停留、信任、下单行动。"
                    "分析必须严格遵循输入中的 ANALYSIS-PROMPT.md：第一步，整体感知并输出 one_line_verdict、holistic_assessment，不引用具体证据；"
                    "第二步，输出 product_visibility，并将事实证据映射到 structure_library_full.md 的 S1-S6 语义阶段、官方模块编号、模块适配性和真实时间边界；"
                    "第三步，输出 loop_closure，并基于被引用证据比较 gap_type 和提升点。"
                    "输出必须精炼：每个视频列出 3 到 6 个关键 evidence_units；任何 evidence、visual_evidence、gap_summary 或 actions 数组最多 3 条；"
                    "每个描述字段最多一句，improvements 按 GMV 杠杆排序输出 1-5 条；视频值得改的点确实多就给 3-5 条，确实只有 1-2 个 GMV 杠杆点就给 1-2 条，不要为凑数编造。"
                    "禁止重复同一判断，禁止为了描述缺失而枚举不存在的音效、卡点、镜头或功能；缺失内容用一句“未发现对应证据”概括。"
                    "不要把 0~3s、3~6s 等参考时间当作固定切片。"
                    "stage_analysis 必须固定输出六项且顺序为 S1 Hook、S2 产品引出、S3 使用过程、S4 效果呈现、S5 信任放大、S6 CTA；每个阶段都必须分别写 benchmark_time_range 和 creator_time_range，并写 creator_module_id、benchmark_module_id、module_fit、module_fit_reason、task_completion、gap_type、gap_summary 和 voice_performance。"
                    "有有效口播时，benchmark_key_message/creator_key_message 必须以该段实际口播传递的信息为核心，再选择确实支持该信息的画面证据。"
                    "没有有效口播时，必须以可见画面与字幕为核心，不得把音乐或推测写成信息。"
                    "每个阶段必须引用 video_understanding 中的 evidence_ids，并写 visual_evidence 和 support_status："
                    "口播与画面共同支持为 supported；口播提及但画面不能验证为 voice_only；仅画面/字幕承载信息为 visual_only；两者矛盾为 conflict。"
                    "阶段引用的事实时间必须与该阶段时间相交；若某阶段确实不存在独立内容，仍应建立该时间段的 evidence_unit，明确说明未发现对应口播或画面，而不是借用其他阶段事实。"
                    "输入中如提供 transcript.srt，其时间戳是口播归因的权威依据；口播不在阶段时间内时必须调整阶段边界或不得引用。"
                    "不得写某张画面展示了认证、成分或效果，除非附带关键帧中实际可见。"
                    "只可把请求中实际附带的关键帧视为已观察画面；未被附图覆盖的时段不得臆造镜头内容，应写为画面证据不足待复核。"
                    "同一关键信息只归入一个最主要阶段，禁止在多个阶段重复作为表现依据。"
                    "认证或审批信息，例如 KKM、KKMA、认证，不属于 Hook。若其与首次产品卖点一起被口播引出，应只归入 S2 作为产品信任支撑；"
                    "认证与营养成分、产品名称等产品属性连续表达时，即使发生在 10 秒之后也仍属于 S2，应延长 S2 的真实边界，不得塞入 S3/S4。"
                    "只有出现独立证明环节时才归入 S5。若口播说到 KKM 但画面不显示认证标记，必须标记 voice_only，并表述为口播声称、画面未验证。"
                    "每个阶段都应从转写中摘录对应本地语言口播到 benchmark_quote/creator_quote，并附中文翻译；没有明确口播时留空。"
                    "每个阶段和提升点都必须写 evidence，引用时间段、画面或口播证据。"
                    "提升点按 GMV 杠杆排序，不按 S1-S6 顺序凑数：CTA 与 Hook 的大差距优先于中等信息传递差距。"
                    "达人建议话术必须使用达人口播语言，creator_script_zh 只放中文翻译。"
                    "如果达人没有有效口播或语言识别不可靠，则根据标杆视频语言/目标市场语言撰写全新的本地语言建议话术，不得把音乐、噪音或无关字幕当作话术。"
                    "达人执行话术必须是针对达人素材重新设计的原创表达，不得抄写或轻微改写标杆口播。"
                    "每个提升点必须输出 base_frame_suitability。只有达人现有画面确实适合作为目标改造基底时，才可写 usable 和 best_base_frame_time；"
                    "如达人素材缺少目标所需的人物、产品或场景，必须写 no_suitable_frame，best_base_frame_time 留空，并在建议中明确需补拍或补素材。"
                    "每项提升点还必须输出 benchmark_evidence_ids 与 base_frame_evidence_id；前者只可指向所属阶段的标杆事实证据，后者必须指向基底帧所在的达人事实证据。"
                    "base_frame_reason 只能描述该达人证据中真实可见的素材。aigc_prompt 只能基于真实存在的达人原始画面写具体改造提示词，不得把不存在的人物、口播或场景说成已有素材。aigc_image_path 留空。"
                    "严禁臆造品牌、型号、价格、优惠、参数或功效。"
                    "只有产品信息、转写或画面证据明确出现时才能写具体品牌；不确定时用用户提供的产品名或本地语言中的中性产品指代。"
                    "对于维生素、营养补充品等健康品类，不得在建议话术中声称治疗疾病、调节激素、改善月经、排出血块或保证效果；标杆中出现此类表达时只能作为合规风险指出。"
                    "\n\n## 关键质量约束（必须遵守）\n"
                    "1. holistic_assessment 六维必须独立评估：structure_integrity 回答'结构是否连贯'，selling_point_efficiency 回答'卖点讲清楚没'，"
                    "audience_resonance 回答'目标用户有没有代入感'，pace_and_emotion 回答'节奏让不让人想看下去'，"
                    "trust_and_purchase_impulse 回答'看完想不想买'，conversion_prediction 回答'购买意愿是立刻想买/犹豫/完全不想买'。"
                    "每维用不同措辞从不同角度写，禁止复制粘贴同一段话。\n"
                    "2. 必须输出 key_conclusions 数组（1-5 条）：完成 S1-S6 对比后，代入本地目标消费者视角，回答'为什么看完标杆想买、看完达人不想买'。"
                    "每条说：达人做了什么→标杆做了什么→对购买意愿的影响。可跨阶段，用消费者语言，不用技术术语。按 GMV 影响从大到小排列。\n"
                    "3. severity 评级（必须差异化，large/medium/small 至少出现 2 种）。判级前先在 gap 字段写清判断依据"
                    "（达人做了什么→标杆做了什么→对目标消费者购买意愿的影响），再据此给 severity，做到推理在前、结论在后。\n"
                    "   可操作判据（按对购买意愿的影响定级，而非按画面差异大小）：\n"
                    "   - large：直接影响购买意愿的硬伤——该环节功能缺失或严重跑偏，会让目标消费者明显更不想买（如 Hook 留不住人、核心卖点讲错、CTA 缺失）；\n"
                    "   - medium：削弱说服力但不致命——功能基本完成，但执行短板让消费者购买意愿打折扣（如卖点讲了但不突出、场景代入感不足）；\n"
                    "   - small：细节瑕疵或达人不输标杆——功能完成且到位，仅细微差距，或达人做得持平甚至更优。\n"
                    "   达人做到位或持平的阶段必须给 small；gap 判定'无明显差距'时 severity 必须是 small。\n"
                    "4. 商业权重必须按品类自适应：Hook 恒高权重；儿童牙膏这类低客单但需说服的功能理性品类，Hook、核心卖点、效果验证和清晰 CTA 优先于调性/BGM。"
                    "关键结论和 improvements 中，Hook/卖点/效果验证/CTA 不得被低权重调性问题排到后面。\n"
                    "5. 达人有效、标杆弱时要记为达人亮点，不判达人差距；例如达人有明确购买指令而标杆没有独立 CTA，S6 应判达人略优或 small，不得判差距中等。\n"
                    "6. S3 只判 how-to 是否看懂；闻香、口味、质感等感官体验归 S4 效果验证。给理由归 S5，给下单指令归 S6。\n"
                    "7. 使用目标市场知识库做文化视角校准：马来/东南亚语境下，真实生活感、轻语气、本地口语、划算/省/方便、节日紧迫感等可能是正向信号；"
                    "但知识库只用于判断有效性，不得替代视频证据，不得在报告中直接展开。\n"
                    "8. gap_type 判断：模块不同=structural，模块同但执行差=execution，资源条件限制=resource。\n"
                    "9. 同一信息只归入功能最匹配的一个阶段，后续阶段不重复。S1 提过的关键词 S2 不再重复分析。"
                    "双方都没有独立设计的阶段（如 S5），key_message 写'均未设计该环节'。"
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "temperature": 0.2,
        # 16384：Qwen 默认 max_tokens 偏低（2048-4096），完整 stage_analysis + improvements 需要 12K+ tokens。
        "max_tokens": 16384,
    }


def build_llm_repair_payload(
    model: str,
    raw_result_text: str,
    error_message: str,
    analysis_input: str,
) -> dict[str, Any]:
    """JSON 修复请求 payload。校验失败时由 pipeline 触发。

    设 max_tokens=16384 与 build_llm_comparison_payload 一致；
    否则 qwen 等 provider 默认 max_tokens 偏低，重新输出完整结构会被截断成残缺 JSON。
    """
    return {
        "model": model,
        "max_tokens": 16384,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Flayr JSON 修复器。只输出严格 JSON，不要 Markdown，不要解释。"
                    "必须符合 references/analysis-output-schema.json：保留 one_line_verdict、holistic_assessment（每维独立评估）、key_conclusions（1-5 条消费者视角）、product_visibility、loop_closure，6 个 stage_analysis，1-5 个 improvements（按 GMV 杠杆排序）。"
                    "如果原始输出缺少 improvements（如 JSON 被截断），必须基于 stage_analysis 的差距分析补充 1-5 条。"
                    "severity 必须差异化：功能没完成=large，有短板=medium，做到位或持平=small。"
                    "必须保留 video_understanding 证据事实清单。stage_analysis 必须严格按 S1、S2、S3、S4、S5、S6 顺序输出六项；阶段必须保留 benchmark_time_range、creator_time_range、证据引用、核心信息、画面证据和 support_status；达人话术必须保留本地语言和中文翻译。"
                    "每个阶段引用的事实单元时间必须与阶段时间相交；缺少独立内容的阶段也要提供该时段的无对应内容事实单元。"
                    "提供了 transcript.srt 时，以其时间戳重新校对口播对应阶段；认证与产品属性连续表达时只归入 S2。"
                    "一条事实只归属一个主要阶段；KKM 等认证信息不能写入 Hook，与产品引出同段出现时仅归入 S2；口播提及但画面不可见时标记 voice_only。"
                    "提升点必须保留 benchmark_evidence_ids、base_frame_suitability、best_base_frame_time、base_frame_evidence_id、base_frame_reason 和 aigc_prompt；无可用达人素材时写 no_suitable_frame 且时间与 base_frame_evidence_id 留空。aigc_image_path 留空。"
                    "健康品类建议不得声称调节激素、改善月经、治疗症状或虚构优惠。建议话术必须重新设计，不得复制标杆原句。"
                    "输出必须精炼，每个描述字段最多一句，improvements 按 GMV 杠杆排序保留 1-5 条；不要为凑数编造。"
                    "任何列表最多 3 条；不要枚举或重复不存在的音效、镜头或功能，缺失证据只写一句概括。"
                    "保留原分析含义，但补齐缺失字段、修正字段类型和 JSON 语法。"
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(
                    [
                        "原始分析输入摘要：",
                        analysis_input[:12000],
                        "校验错误：",
                        error_message,
                        "模型原始输出：",
                        raw_result_text[:12000],
                    ]
                ),
            },
        ],
        "temperature": 0.0,
    }
