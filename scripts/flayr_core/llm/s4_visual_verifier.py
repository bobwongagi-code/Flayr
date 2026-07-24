"""S3/S4 visual verifier：独立复核使用过程与效果呈现的视觉质量。

主分析模型容易把"准备动作"误写成真实使用，或把"结构存在"自证成"效果成立"。
本模块只看 S3/S4 evidence 对应帧和适用的产品视觉命题，不读取主分析的 severity，
专门复核实际目标接触、关键动作连续性与效果差异是否真的可见。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds, parse_time_range_seconds, parse_timestamp_seconds, select_frames_for_time_range
from ..postprocess.chain import finalize_severity_after_repairs
from ..postprocess.repair import reconcile_s3_s4_evidence_coherence, stabilize_improvement_priorities
from ..utils import write_json, write_text
from .api import call_llm_api, extract_chat_completion_text, image_to_data_url, video_to_data_url
from .parse import normalize_demo_flag, normalize_s4_effect_salience, normalize_s4_effect_type, parse_json_text


TEMPORAL_REVIEW_FPS = 3.0
TEMPORAL_REVIEW_MAX_WIDTH = 480
TEMPORAL_REVIEW_PADDING_SECONDS = 1.0


def maybe_apply_s4_visual_verifier(
    *,
    args: Any,
    api_key: str,
    result: dict[str, Any],
    analysis: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """独立复核 S3 真实使用与 S4 视觉差异；失败时只写状态，不中断主流程。"""
    if getattr(args, "llm_dry_run", False):
        return result
    s4 = _s4_stage(result)
    if not s4:
        return result
    contract_reason = _visual_verifier_skip_reason(result)
    review_s4 = not bool(contract_reason)
    payload = build_s4_visual_verifier_payload(
        getattr(args, "llm_model", ""),
        result,
        analysis,
        review_s4=review_s4,
        budget=getattr(args, "_resource_budget", None),
    )
    if payload is None:
        result["s4_visual_verifier"] = {"applied": False, "reason": "缺少 S3/S4 帧证据，跳过视觉复核。"}
        return result

    request_path = run_dir / "llm_s4_visual_verifier_request.json"
    response_path = run_dir / "llm_s4_visual_verifier_response.json"
    write_json(request_path, payload)
    try:
        raw_text = call_llm_api(
            getattr(args, "llm_api_url"),
            api_key,
            request_path,
            response_path,
            budget=getattr(args, "_resource_budget", None),
        )
        parsed = parse_json_text(extract_chat_completion_text(json.loads(raw_text)))
        applied = apply_s4_visual_verifier_result(result, parsed, analysis, review_s4=review_s4)
    except (Exception, SystemExit) as exc:  # verifier 是降级增强，不允许拖垮主链
        result["s4_visual_verifier"] = {"applied": False, "reason": f"S4 视觉复核失败：{exc}"}
        return result

    result["s4_visual_verifier"] = {
        "applied": applied,
        "response_retention": "ephemeral",
        "reason": (
            "已用原片时序复核覆盖 S3 使用真实性与 S4 视觉质量字段。"
            if applied and review_s4
            else "已用原片时序复核覆盖 S3 使用真实性；S4 未满足独立覆盖合同。"
            if applied
            else "S3/S4 视觉复核返回空结果。"
        ),
    }
    if contract_reason:
        result["s4_visual_verifier"]["s4_skip_reason"] = contract_reason
    return result


def build_s4_visual_verifier_payload(
    model: str,
    result: dict[str, Any],
    analysis: dict[str, Any],
    *,
    review_s4: bool = True,
    budget: Any = None,
) -> dict[str, Any] | None:
    """构造 S3/S4 原片短片优先、静帧兜底的独立复核 payload。"""
    s4 = _s4_stage(result)
    if not s4:
        return None
    s3 = _s3_stage(result)
    product_profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    structural_scope = _stage_is_structural(result, "S4")
    scope_rule = _visual_verifier_scope_rule(product_profile, structural_scope)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "你是独立的 S3/S4 视觉复核器。只根据下面给你的原片阶段短片与关键帧判断，"
                "不要沿用主分析结论，不评价口播好坏，不改 S1/S2/S5/S6。\n"
                "S3 任务：分别判断产品/材料是否真实接触并作用于目标对象、动作是否新施加/位移/激活材料或直接改变目标状态、"
                "以及关键作用动作与目标状态是否可追踪。"
                "不要求拍到绝对的接触前状态，允许从刷洗/涂抹/挤出等真实动作中途开始；但帧序列必须直接看见动作使材料或目标发生可观察变化。"
                "手持展示、剪裁准备、空中比划、按压或搅动已在目标上的材料、或从准备直接跳到完成态都不是可确认的真实使用。"
                "不要求全程一镜到底，但必须看见作用于目标的关键动作、动作引发的变化和合理状态承接。\n"
                + (
                    "S4 任务：分别判断达人和标杆是否真的把效果差异拍出来，并判断是否存在可信因果桥。\n"
                    if review_s4
                    else "本轮 S4 证明合同不允许独立覆盖：两侧 s4 必须填 null，只复核 S3。\n"
                )
                + scope_rule + "\n"
                + "S4 的 true 必须由帧中直接可见的前后状态、A/B 对照、第三方参照物测试，或过程中的可视化变化支持；"
                "不得把静止结果、手在水中移动、或口播承诺推断为“状态已改变”。"
                "module_constraints_met 按 structure_library S4-A~F 硬约束判断；effect_maximized 只有差异明显、画面聚焦、无需停下来找变化才 true。\n"
                "输出严格 JSON："
                "{\"creator\":{\"s3\":{\"evidence_sufficient\":bool,\"action_target_contact_met\":bool,\"action_application_change_visible\":bool,\"critical_action_continuity_met\":bool,\"reason\":\"一句话\"},"
                "\"s4\":{\"effect_type\":\"before_after|split_screen|person_vs_person|product_vs_alt|quantified_test|process_visualization|aesthetic_display|none\","
                "\"evidence_sufficient\":bool,\"effect_proposition_matched\":bool,\"visual_difference_observed\":bool,\"module_constraints_met\":bool,"
                "\"effect_salience\":\"none|subtle|clear|strong\",\"requires_close_inspection\":bool,\"effect_maximized\":bool,\"reason\":\"一句话\"}},"
                "\"benchmark\":{同字段}}。某侧没有 S3 帧时，该侧 s3 必须填 null；不要从 S4 结果反推 S3 为 true。"
                "evidence_sufficient 只回答当前原片短片或兜底帧是否完整覆盖要判断的关键动作或效果状态。"
                "有原片短片时必须逐帧核对，不得把已经贴好/涂好/清洁好的结果态上的按压、触摸、搅动或效果测试，"
                "倒推成此前发生过的新施加动作。若短片从结果态开始且直到结束都没有新施加/位移/激活材料，"
                "evidence_sufficient=true、action_application_change_visible=false、critical_action_continuity_met=false。"
                "只有在没有原片、仅有静帧且无法区分动作时，才因覆盖不足填 evidence_sufficient=false；"
                "证据不足不等于动作/效果不存在。"
            ),
        },
    ]
    if not structural_scope:
        content.append({"type": "text", "text": "product_profile=" + json.dumps(product_profile, ensure_ascii=False)})

    role_payloads = []
    for role in ("creator", "benchmark"):
        s3_video = _collect_stage_video(role, s3, analysis, "S3", budget=budget) if s3 else None
        s4_video = _collect_stage_video(role, s4, analysis, "S4", budget=budget) if review_s4 else None
        s3_frames = _collect_stage_frames(role, s3, result, analysis, "s3", limit=3) if s3 else []
        s4_frames = _collect_stage_frames(role, s4, result, analysis, "s4", limit=3) if review_s4 else []
        if not s3_video and not s4_video and not s3_frames and not s4_frames:
            continue
        content.append(
            {
                "type": "text",
                "text": f"\n【{role} 待复核】以下帧按时间顺序提供；只以画面为依据。",
            }
        )
        for clip in (s3_video, s4_video):
            if not clip:
                continue
            content.append({"type": "text", "text": clip["label"]})
            content.append({"type": "video_url", "video_url": {"url": clip["data_url"]}})
        for frame in s3_frames:
            content.append({"type": "text", "text": "S3 " + frame["label"]})
            content.append({"type": "image_url", "image_url": {"url": frame["data_url"], "detail": "high"}})
        for frame in s4_frames:
            content.append({"type": "text", "text": "S4 " + frame["label"]})
            content.append({"type": "image_url", "image_url": {"url": frame["data_url"], "detail": "high"}})
        role_payloads.append(role)
    if len(role_payloads) < 2:
        return None
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只做视觉复核，严格输出 JSON，不输出解释文本。"},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
    }


def _visual_verifier_skip_reason(result: dict[str, Any]) -> str:
    """同任务结构对标可直接复核共同任务；其余只消费通过合同校验的直接视觉合同。"""
    if _stage_is_structural(result, "S4"):
        return ""
    profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    source = str(profile.get("proof_contract_source") or "inferred").strip().lower()
    if source not in {"operator", "curated"}:
        return "S4 proof_contract 仅来自模型推断，视觉复核只做辅助判断，不覆盖主分析。"
    contract = profile.get("proof_contract") if isinstance(profile.get("proof_contract"), dict) else None
    if contract is None:
        return ""
    if contract.get("valid") is not True:
        reason = str(contract.get("validation_reason") or "证明合同未通过校验")
        return f"proof_contract 无效，跳过直接视觉复核：{reason}。"
    if contract.get("mode") not in {"instant_visual", "process_result"}:
        return f"proof_contract={contract.get('mode')}，不适用直接视觉差异复核。"
    return ""


def _stage_is_structural(result: dict[str, Any], stage_code: str) -> bool:
    """只读取指定阶段的资格，避免整体商品关系替阶段判断。"""
    contract = result.get("comparison_contract") or result.get("comparison_eligibility") or {}
    from .parse import normalize_comparison_contract

    contract = normalize_comparison_contract(contract)
    stages = contract.get("stage_eligibility") if isinstance(contract, dict) else {}
    stage = stages.get(stage_code) if isinstance(stages, dict) else {}
    return isinstance(stage, dict) and stage.get("status") == "structural"


def apply_s4_visual_verifier_result(
    result: dict[str, Any],
    verifier_result: dict[str, Any],
    analysis: dict[str, Any] | None = None,
    *,
    review_s4: bool = True,
) -> bool:
    """把独立复核结果写回 S3/S4 flags，并重推 severity。"""
    s4 = _s4_stage(result)
    if not s4:
        return False
    s3 = _s3_stage(result)
    applied = False
    for role in ("creator", "benchmark"):
        patch = verifier_result.get(role)
        s4_flag = s4.get(f"{role}_s4")
        if not isinstance(patch, dict):
            continue
        # 兼容旧 verifier 返回的 role 顶层 S4 字段，避免既有离线结果/脚本失效。
        s3_patch = patch.get("s3") if isinstance(patch.get("s3"), dict) else None
        s4_patch = patch.get("s4") if isinstance(patch.get("s4"), dict) else patch
        s3_flag = s3.get(f"{role}_s3") if isinstance(s3, dict) and isinstance(s3.get(f"{role}_s3"), dict) else None
        if isinstance(s3_patch, dict) and isinstance(s3_flag, dict):
            s3_sufficient = normalize_demo_flag(s3_patch.get("evidence_sufficient")) is True
            for key in (
                "action_target_contact_met",
                "action_application_change_visible",
                "critical_action_continuity_met",
            ):
                value = normalize_demo_flag(s3_patch.get(key))
                if _can_apply_visual_value(s3_flag.get(key), value, s3_sufficient):
                    s3_flag[key] = value
                    applied = True
            reason = str(s3_patch.get("reason") or "").strip()
            if reason:
                s3_flag["visual_verifier_reason"] = reason
            if not s3_sufficient:
                s3_flag["visual_verifier_coverage"] = "insufficient_for_negative_override"
        if not review_s4 or not isinstance(s4_flag, dict) or not isinstance(s4_patch, dict):
            continue
        effect_type = normalize_s4_effect_type(s4_patch.get("effect_type"))
        if effect_type != "none":
            s4_flag["effect_type"] = effect_type
            applied = True
        s4_sufficient = normalize_demo_flag(s4_patch.get("evidence_sufficient")) is True
        for key in ("effect_proposition_matched", "visual_difference_observed", "module_constraints_met", "requires_close_inspection", "effect_maximized"):
            value = normalize_demo_flag(s4_patch.get(key))
            if _can_apply_visual_value(s4_flag.get(key), value, s4_sufficient):
                s4_flag[key] = value
                applied = True
        # 视觉复核明确看见差异时，不能保留主分析中已经被复核推翻的 effect_visible=false。
        if s4_flag.get("visual_difference_observed") is True:
            s4_flag["effect_visible"] = True
            applied = True
        salience = normalize_s4_effect_salience(s4_patch.get("effect_salience"))
        if salience in {"none", "subtle", "clear", "strong"} and (
            salience not in {"none", "subtle"}
            or s4_sufficient
            or s4_flag.get("effect_salience") in {None, "none", "subtle"}
        ):
            s4_flag["effect_salience"] = salience
            applied = True
        reason = str(s4_patch.get("reason") or "").strip()
        if reason:
            s4_flag["visual_verifier_reason"] = reason
            s4_flag["effect_reason"] = reason
            applied = True
        if s4_sufficient is False:
            s4_flag["visual_verifier_coverage"] = "insufficient_for_negative_override"
        if s4_sufficient and s4_flag.get("visual_difference_observed") is False:
            s4_flag["effect_visible"] = False
            s4_flag["effect_proposition_matched"] = False
            applied = True
    if applied:
        reconcile_s3_s4_evidence_coherence(result)
        finalize_severity_after_repairs(result, analysis)
        stabilize_improvement_priorities(result)
    return applied


def _can_apply_visual_value(current: Any, proposed: bool | None, evidence_sufficient: bool) -> bool:
    """静帧复核可补强正向事实；要推翻主链正向事实必须证明帧覆盖充分。"""
    if proposed is None:
        return False
    if proposed is True:
        return True
    return current is not True or evidence_sufficient


def _s4_stage(result: dict[str, Any]) -> dict[str, Any] | None:
    stages = result.get("stage_analysis")
    if not isinstance(stages, list) or len(stages) < 4:
        return None
    stage = stages[3]
    if not isinstance(stage, dict) or not str(stage.get("stage") or "").upper().startswith("S4"):
        return None
    return stage


def _s3_stage(result: dict[str, Any]) -> dict[str, Any] | None:
    stages = result.get("stage_analysis")
    if not isinstance(stages, list) or len(stages) < 3:
        return None
    stage = stages[2]
    if not isinstance(stage, dict) or not str(stage.get("stage") or "").upper().startswith("S3"):
        return None
    return stage


def _collect_stage_video(
    role: str,
    stage: dict[str, Any],
    analysis: dict[str, Any],
    stage_code: str,
    budget: Any = None,
) -> dict[str, str] | None:
    """截取指定阶段原片；时序动作判断优先消费短片，静帧只作定位兜底。"""
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos.get(role), dict) else {}
    video_path = Path(str(info.get("path") or ""))
    if not video_path.is_file():
        return None
    time_range = str(stage.get(f"{role}_time_range") or "")
    duration = info.get("duration_seconds")
    parsed = parse_time_range_seconds(time_range, duration)
    if parsed is None:
        return None
    start, end = parsed
    duration_value = parse_timestamp_seconds(duration)
    if duration is not None and str(duration).strip() and duration_value is None:
        return None
    duration_value = end if duration_value is None else duration_value
    padded_start = max(0.0, start - TEMPORAL_REVIEW_PADDING_SECONDS)
    padded_end = min(duration_value, end + TEMPORAL_REVIEW_PADDING_SECONDS) if duration_value > 0 else end
    if padded_end <= padded_start:
        return None
    data_url = video_to_data_url(
        video_path,
        fps=TEMPORAL_REVIEW_FPS,
        max_width=TEMPORAL_REVIEW_MAX_WIDTH,
        start=padded_start,
        duration=padded_end - padded_start,
        budget=budget,
    )
    if data_url is None:
        return None
    return {
        "label": (
            f"{stage_code} 原片短片｜{role}｜{format_seconds(padded_start)} - {format_seconds(padded_end)}｜"
            f"fps≈{TEMPORAL_REVIEW_FPS:g}｜max_width={TEMPORAL_REVIEW_MAX_WIDTH}"
        ),
        "data_url": data_url,
    }


def _collect_stage_frames(
    role: str,
    stage: dict[str, Any],
    result: dict[str, Any],
    analysis: dict[str, Any],
    flag_name: str,
    limit: int,
) -> list[dict[str, str]]:
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos.get(role), dict) else {}
    units = {
        str(unit.get("id")): unit
        for unit in (((result.get("video_understanding") or {}).get(role) or {}).get("evidence_units") or [])
        if isinstance(unit, dict)
    }
    flag = stage.get(f"{role}_{flag_name}") if isinstance(stage.get(f"{role}_{flag_name}"), dict) else {}
    ids = [str(value) for value in (flag.get("evidence_ids") or stage.get(f"{role}_evidence_ids") or []) if str(value).strip()]
    frames: list[dict[str, str]] = []
    used_paths: set[str] = set()
    for evidence_id in ids:
        unit = units.get(evidence_id)
        if not unit:
            continue
        time_range = str(unit.get("time_range") or "")
        for frame in select_frames_for_time_range(info, time_range, limit=limit):
            path = Path(str(frame.get("path") or ""))
            if not path.is_file() or str(path) in used_paths:
                continue
            timestamp = format_seconds(frame.get("timestamp_seconds"))
            label = f"{role} evidence={evidence_id} time={time_range} frame={timestamp}"
            frames.append({"label": label, "data_url": image_to_data_url(path)})
            used_paths.add(str(path))
            if len(frames) >= limit:
                return frames
    return frames


def _visual_verifier_scope_rule(product_profile: dict[str, Any], structural_scope: bool) -> str:
    """隔离结构对标与同品证明合同，避免 SKU 目标污染视觉证据判断。"""
    if structural_scope:
        return (
            "本次是同类同任务、非同 SKU 的结构执行对标。忽略输入产品的具体配方、参数、品牌及 primary proof contract，"
            "不要求两侧证明相同的具体功效。effect_proposition_matched 只判断该侧画面是否直接证明其自身正在宣称的可见结果；"
            "例如承重/粘合、防水/止漏、去污、肤色或妆面变化都可成立，但必须由对应的画面测试或状态变化支持。"
        )
    return (
        "本次是同品或可直接比较范围。effect_proposition_matched 必须命中 product_profile 的 primary 视觉证明点。"
        "若 product_profile.short_video_proof_plan 存在，先确认 S4 anchor，再按由该 anchor 生成的 priority=primary 证明点判断核心效果是否成立；"
        "priority=secondary 的证明点只能作为补充说明，不能替代 primary，也不能让 primary 已成立的一侧被判为无效。"
        "若 primary 文本误把多个卖点写成复合条件，按最核心的消费者最终结果判断 primary；机制/附加卖点缺失只能写进 reason，不能直接把 primary 判 false。"
        "若无 visual_proof_points，则回退 core_visual_proposition + visual_diff_dimensions。"
    )
