"""S4 visual verifier：独立复核效果呈现的视觉质量。

主分析模型容易把"结构存在"自证成"效果成立"。本模块只看 S4 evidence
对应帧和产品视觉命题，不读取主分析的 severity，专门复核效果差异是否真的可见。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds, select_frames_for_time_range
from ..postprocess.derive import derive_severity_from_facts
from ..postprocess.repair import stabilize_improvement_priorities
from ..utils import write_json
from .api import call_llm_api, extract_chat_completion_text, image_to_data_url
from .parse import normalize_demo_flag, normalize_s4_effect_salience, parse_json_text


def maybe_apply_s4_visual_verifier(
    *,
    args: Any,
    api_key: str,
    result: dict[str, Any],
    analysis: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """独立复核 S4 视觉差异；失败时只写状态，不中断主流程。"""
    if getattr(args, "llm_dry_run", False):
        return result
    s4 = _s4_stage(result)
    if not s4:
        return result
    contract_reason = _visual_verifier_skip_reason(result)
    if contract_reason:
        result["s4_visual_verifier"] = {"applied": False, "reason": contract_reason}
        return result
    payload = build_s4_visual_verifier_payload(getattr(args, "llm_model", ""), result, analysis)
    if payload is None:
        result["s4_visual_verifier"] = {"applied": False, "reason": "缺少 S4 帧证据，跳过视觉复核。"}
        return result

    request_path = run_dir / "llm_s4_visual_verifier_request.json"
    response_path = run_dir / "llm_s4_visual_verifier_response.json"
    write_json(request_path, payload)
    try:
        raw_text = call_llm_api(getattr(args, "llm_api_url"), api_key, request_path, response_path)
        response_path.write_text(raw_text, encoding="utf-8")
        parsed = parse_json_text(extract_chat_completion_text(json.loads(raw_text)))
        applied = apply_s4_visual_verifier_result(result, parsed, analysis)
    except (Exception, SystemExit) as exc:  # verifier 是降级增强，不允许拖垮主链
        result["s4_visual_verifier"] = {"applied": False, "reason": f"S4 视觉复核失败：{exc}"}
        return result

    result["s4_visual_verifier"] = {
        "applied": applied,
        "response_path": str(response_path),
        "reason": "已用独立视觉复核覆盖 S4 视觉质量字段。" if applied else "S4 视觉复核返回空结果。",
    }
    return result


def build_s4_visual_verifier_payload(
    model: str,
    result: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any] | None:
    """构造只含 S4 帧证据的独立复核 payload。"""
    s4 = _s4_stage(result)
    if not s4:
        return None
    product_profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "你是独立的 S4 效果呈现视觉复核器。只根据下面给你的 S4 关键帧判断，"
                "不要沿用主分析结论，不评价口播好坏，不改 S1/S2/S3/S5/S6。\n"
                "任务：分别判断达人和标杆是否真的把 product_profile 的 S4 视觉证明拍出来。\n"
                "若 product_profile.short_video_proof_plan 存在，先确认 S4 anchor，再按由该 anchor 生成的 priority=primary 证明点判断核心效果是否成立；"
                "priority=secondary 的证明点只能作为补充说明，不能替代 primary，也不能让 primary 已成立的一侧被判为无效。"
                "若 primary 文本误把多个卖点写成复合条件（例如清洁结果+刷头溶解），按最核心的消费者最终结果判断 primary；"
                "机制/附加卖点缺失只能写进 reason，不能直接把 primary 判 false。"
                "若无 visual_proof_points，则回退 core_visual_proposition + visual_diff_dimensions。\n"
                "判定标准：visual_difference_observed 优先看 primary.visual_diff_dimensions 指定维度是否肉眼可见；"
                "module_constraints_met 按 structure_library S4-A~F 硬约束判断；effect_maximized 只有差异明显、画面聚焦、无需停下来找变化才 true。\n"
                "输出严格 JSON："
                "{\"creator\":{\"visual_difference_observed\":bool,\"module_constraints_met\":bool,\"effect_salience\":\"none|subtle|clear|strong\","
                "\"requires_close_inspection\":bool,\"effect_maximized\":bool,\"reason\":\"一句话\"},"
                "\"benchmark\":{同字段}}。"
            ),
        },
        {
            "type": "text",
            "text": "product_profile=" + json.dumps(product_profile, ensure_ascii=False),
        },
    ]

    role_payloads = []
    for role in ("creator", "benchmark"):
        frames = _collect_s4_frames(role, s4, result, analysis, limit=4)
        if not frames:
            continue
        flag = s4.get(f"{role}_s4") if isinstance(s4.get(f"{role}_s4"), dict) else {}
        content.append(
            {
                "type": "text",
                "text": f"\n【{role} S4 待复核】existing_s4_type={flag.get('effect_type')}，以下是该侧 S4 evidence 对应帧：",
            }
        )
        for frame in frames:
            content.append({"type": "text", "text": frame["label"]})
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
    """直接视觉复核只消费通过合同校验的 instant_visual/process_result。"""
    profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    contract = profile.get("proof_contract") if isinstance(profile.get("proof_contract"), dict) else None
    if contract is None:
        return ""
    if contract.get("valid") is not True:
        reason = str(contract.get("validation_reason") or "证明合同未通过校验")
        return f"proof_contract 无效，跳过直接视觉复核：{reason}。"
    if contract.get("mode") not in {"instant_visual", "process_result"}:
        return f"proof_contract={contract.get('mode')}，不适用直接视觉差异复核。"
    return ""


def apply_s4_visual_verifier_result(
    result: dict[str, Any],
    verifier_result: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> bool:
    """把独立复核结果写回 S4 flags，并重推 severity。"""
    s4 = _s4_stage(result)
    if not s4:
        return False
    applied = False
    for role in ("creator", "benchmark"):
        patch = verifier_result.get(role)
        flag = s4.get(f"{role}_s4")
        if not isinstance(patch, dict) or not isinstance(flag, dict):
            continue
        for key in ("visual_difference_observed", "module_constraints_met", "requires_close_inspection", "effect_maximized"):
            value = normalize_demo_flag(patch.get(key))
            if value in {True, False}:
                flag[key] = value
                applied = True
        salience = normalize_s4_effect_salience(patch.get("effect_salience"))
        if salience in {"none", "subtle", "clear", "strong"}:
            flag["effect_salience"] = salience
            applied = True
        reason = str(patch.get("reason") or "").strip()
        if reason:
            flag["visual_verifier_reason"] = reason
            flag["effect_reason"] = reason
            applied = True
        if flag.get("visual_difference_observed") is False:
            flag["effect_visible"] = False
            flag["effect_proposition_matched"] = False
            applied = True
    if applied:
        derive_severity_from_facts(result, analysis)
        stabilize_improvement_priorities(result)
    return applied


def _s4_stage(result: dict[str, Any]) -> dict[str, Any] | None:
    stages = result.get("stage_analysis")
    if not isinstance(stages, list) or len(stages) < 4:
        return None
    stage = stages[3]
    if not isinstance(stage, dict) or not str(stage.get("stage") or "").upper().startswith("S4"):
        return None
    return stage


def _collect_s4_frames(
    role: str,
    s4: dict[str, Any],
    result: dict[str, Any],
    analysis: dict[str, Any],
    limit: int,
) -> list[dict[str, str]]:
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos.get(role), dict) else {}
    units = {
        str(unit.get("id")): unit
        for unit in (((result.get("video_understanding") or {}).get(role) or {}).get("evidence_units") or [])
        if isinstance(unit, dict)
    }
    flag = s4.get(f"{role}_s4") if isinstance(s4.get(f"{role}_s4"), dict) else {}
    ids = [str(value) for value in (flag.get("evidence_ids") or s4.get(f"{role}_evidence_ids") or []) if str(value).strip()]
    frames: list[dict[str, str]] = []
    used_paths: set[str] = set()
    for evidence_id in ids:
        unit = units.get(evidence_id)
        if not unit:
            continue
        time_range = str(unit.get("time_range") or "")
        for frame in select_frames_for_time_range(info, time_range, limit=2):
            path = Path(str(frame.get("path") or ""))
            if not path.is_file() or str(path) in used_paths:
                continue
            timestamp = format_seconds(frame.get("timestamp_seconds"))
            label = (
                f"{role} evidence={evidence_id} time={time_range} frame={timestamp} "
                f"fact={str(unit.get('information') or '')[:120]}"
            )
            frames.append({"label": label, "data_url": image_to_data_url(path)})
            used_paths.add(str(path))
            if len(frames) >= limit:
                return frames
    return frames
