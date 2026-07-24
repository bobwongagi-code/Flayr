"""flayr_core.postprocess.derive：severity 约束解析器。

derive 是模型 severity 的安全网，不是第二个评分模型。它只允许把确定性事实
转换为 floor/ceiling 约束，再由一个 resolver 统一收口：

* 没有确定性约束时保留 model_severity；
* 多条 floor 取 max，多条 ceiling 取 min；
* floor == ceiling 是合法 clamp，floor > ceiling 是冲突，冲突时保留模型并交给 Phase C；
* 缺失、unknown、uncertain 和不足以证明的事实绝不触发规则；
* E/W/C 连续公式不再参与 severity，painpoint_relevance 由商业优先级层单独消费。

所有异常都必须优雅降级，写入 severity_derivation，不得拖垮主分析流程。
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from ..multimodal import channel_requirement_for, has_multimodal_assessment, multimodal_execution

_STAGE_RE = re.compile(r"(S[1-6])")

SEVERITIES = ("small", "medium", "large")
SEVERITY_RANK = {value: index for index, value in enumerate(SEVERITIES)}
EVIDENCE_STRENGTHS = ("direct", "explicit", "inferred", "absent")
_EXPLICIT_STRENGTHS = {"direct", "explicit"}
_S1_REPAIR_STATE_KEY = "s1_hook_boundaries"
_S1_REPAIR_STATE_VALUE = "repaired"


class _Endorsement(NamedTuple):
    """该侧硬背书聚合结果。具名避免 (verbal, visual, available) 位置元组解包错位。"""
    verbal: bool      # 口播/字幕出现硬背书来源词
    visual: bool      # 画面出现独立硬背书视觉证据
    available: bool   # 该侧 unit 是否有结构化 endorsement 字段


_NO_ENDORSEMENT = _Endorsement(False, False, False)


def _side_endorsement(result: dict[str, Any], side: str) -> _Endorsement:
    """从 Stage1 facts 聚合该侧硬背书存在性（口播/画面各一）：代码聚合、不让 Stage2 重判（绕过判断层）。
    作用域：该侧【全部 unit】--证书/背书出现在任一 unit 即算，不依赖 functions 阶段标记
    （functions 是模型 descriptive 输出、会误标，挂上去会漏检真背书；背书归不归 S5 由本就只在 S5 闸消费保证）。"""
    vu = result.get("video_understanding")
    side_vu = vu.get(side) if isinstance(vu, dict) else None
    units = side_vu.get("evidence_units") if isinstance(side_vu, dict) else None
    if not isinstance(units, list):
        return _NO_ENDORSEMENT
    units = [u for u in units if isinstance(u, dict)]
    verbal = any(u.get("endorsement_verbal") is True for u in units)
    visual = any(u.get("endorsement_visual") is True for u in units)
    # normalize 层会保留缺失字段为 None；只有明确出现 true/false 才算该观察
    # 信道可用，不能因为字段被补成 None 就把 unknown 当作 false。
    available = any(
        u.get("endorsement_verbal") is not None or u.get("endorsement_visual") is not None
        for u in units
    )
    return _Endorsement(verbal, visual, available)


def _s1_hook_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S1 Hook flag 化：四维 bool 命中数 → 执行分（met/4×2，落回现有 0-2 尺度）。
    两侧 hook flag 任一缺失 → 返回 None（derive 回退模型执行分，优雅降级）。
    hook_exists 是红线/前置（达人无 Hook、标杆有 Hook → large），不混进四维执行分。"""
    c = stage.get("creator_hook")
    b = stage.get("benchmark_hook")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None
    if b.get("exists") is True and c.get("exists") is False:
        return {"redline": True}
    c_met = sum(1 for v in (c.get("dims") or {}).values() if v is True)
    b_met = sum(1 for v in (b.get("dims") or {}).values() if v is True)
    c_exec, b_exec = c_met / 4 * 2, b_met / 4 * 2
    # landing 封顶：钩子没打穿（landing_met=false）→ 该侧执行分最高 1.0，结构件齐全也不算"出色"。
    if c.get("landing_met") is False:
        c_exec = min(c_exec, 1.0)
    if b.get("landing_met") is False:
        b_exec = min(b_exec, 1.0)
    return {"redline": False, "creator_exec": c_exec, "bench_exec": b_exec}


def _s2_contract_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S2 产品引出契约 flag：只校准"自然承接 + 产品身份/角色明确"，不做四维主观分。

    任一侧缺 flag → 返回 None，derive 保留模型执行分。merged_with_s3=true 时不因独立 S2 短/弱扣分。
    """
    c = stage.get("creator_s2")
    b = stage.get("benchmark_s2")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None

    def side_exec(flag: dict[str, Any]) -> float:
        if flag.get("exists") is False:
            return 0.0
        # S1 缺失会让 S2 没有可承接对象，但如果产品身份和解决方案角色已经清楚，
        # 这个问题应由 S1 承担，避免在 S2 重复计罚。
        if (
            flag.get("merged_with_s3") is True
            and flag.get("product_identity_clear") is True
            and flag.get("product_role_clear") is True
        ):
            return 2.0
        s1_s2_compatible = flag.get("computed_s1_s2_compatible")
        if s1_s2_compatible not in {True, False}:
            s1_s2_compatible = flag.get("s1_s2_compatible")
        met = sum(
            1
            for key in ("handoff_met", "s1_s2_compatible", "product_identity_clear", "product_role_clear")
            if (s1_s2_compatible if key == "s1_s2_compatible" else flag.get(key)) is True
        )
        if met >= 4:
            return 2.0
        if met >= 3:
            return 1.0
        if met >= 1:
            return 0.5
        return 0.0

    return {"creator_exec": side_exec(c), "bench_exec": side_exec(b)}


def _s3_strong_scene(flag: dict[str, Any]) -> bool:
    """S3 场景/表现层是否把使用过程做厚。

    S3 主轴是"动作里证明核心卖点"。多场景/丰富度只是表现层，不能补偿核心卖点缺口；
    单场景全流程如果只有动作链完整，只能算合格；要到强 S3，必须把同一卖点做厚
    （多角度/多卖点/时间变化/角色互动等），避免把"结构存在"误当"执行出色"。
    """
    missing = flag.get("missing_selling_points")
    has_missing_core = isinstance(missing, list) and any(str(item).strip() for item in missing)
    if has_missing_core:
        return False
    if flag.get("action_proof_met") is False:
        return False
    if flag.get("action_target_contact_met") is False:
        return False
    if flag.get("action_application_change_visible") is False:
        return False
    if flag.get("critical_action_continuity_met") is False:
        return False
    mode = str(flag.get("scene_mode") or "unknown")
    if mode == "single_scene":
        return (
            (flag.get("single_scene_continuity_met") is True or flag.get("continuity_met") is True)
            and (flag.get("richness_met") is True or flag.get("single_scene_variation_met") is True)
        )
    if mode == "multi_scene":
        return (
            flag.get("multi_scene_logic_met") is True
            and flag.get("multi_scene_transition_met") is True
            and flag.get("multi_scene_role_adaptation_met") is True
        )
    if mode == "multi_person":
        return flag.get("role_design_met") is True and flag.get("role_interaction_met") is True
    if mode == "hybrid":
        return flag.get("continuity_met") is True and flag.get("richness_met") is True
    return flag.get("continuity_met") is True and flag.get("richness_met") is True


def _s3_usage_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S3 使用过程 flag：真实使用 + 核心卖点可见是主轴，场景组织/表现层只在主轴成立后加分。"""
    c = stage.get("creator_s3")
    b = stage.get("benchmark_s3")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None

    def side_exec(flag: dict[str, Any]) -> float:
        if flag.get("exists") is False:
            return 0.0
        if flag.get("mouth_only_or_static") is True:
            return 0.0
        if flag.get("result_only_without_process") is True:
            return 0.5
        has_usage = flag.get("usage_process_visible")
        if has_usage is None:
            has_usage = flag.get("real_usage_met")
        if has_usage is False or flag.get("fake_or_staged") is True:
            return 0.0
        if flag.get("core_selling_point_visible") is not True:
            return 0.5
        if flag.get("action_proof_met") is False:
            return 0.5
        if flag.get("action_target_contact_met") is False:
            return 0.0
        if flag.get("action_application_change_visible") is False:
            return 0.0
        if flag.get("critical_action_continuity_met") is False:
            return 0.0
        if flag.get("usage_context_fit") is not True:
            return 0.5
        missing = flag.get("missing_selling_points")
        if isinstance(missing, list) and any(str(item).strip() for item in missing):
            return 1.0
        return 2.0 if _s3_strong_scene(flag) else 1.0

    return {"creator_exec": side_exec(c), "bench_exec": side_exec(b)}


def _s3_complete_real_usage(flag: dict[str, Any]) -> bool:
    """S3-A~E 共同底线：真实作用、关键动作闭环、核心卖点在动作中可见。"""
    return (
        (flag.get("usage_process_visible") is True or flag.get("real_usage_met") is True)
        and flag.get("core_selling_point_visible") is True
        and flag.get("action_proof_met") is True
        and flag.get("action_target_contact_met") is True
        and flag.get("action_application_change_visible") is True
        and flag.get("critical_action_continuity_met") is True
    )


def _s3_explicitly_missing_real_usage(flag: dict[str, Any]) -> bool:
    """只在事实明确否定使用过程时触发，未知字段不冒充缺失。"""
    return (
        flag.get("exists") is False
        or flag.get("mouth_only_or_static") is True
        or flag.get("result_only_without_process") is True
        or flag.get("usage_process_visible") is False
        or flag.get("real_usage_met") is False
        or flag.get("action_target_contact_met") is False
        or flag.get("action_application_change_visible") is False
        or flag.get("critical_action_continuity_met") is False
    )


def _s4_strong_visible_effect(flag: dict[str, Any]) -> bool:
    """S4-A~F 的共同底线：用户无需脑补即可看到并理解效果。"""
    return (
        flag.get("effect_visible") is True
        and flag.get("visual_difference_observed") is True
        and flag.get("module_constraints_met") is True
        and str(flag.get("effect_salience") or "") in {"clear", "strong"}
        and flag.get("effect_attribution_supported") is True
        and flag.get("requires_close_inspection") is not True
        and flag.get("tamper_or_cut_risk") is not True
    )


def _s4_explicitly_missing_visible_effect(flag: dict[str, Any]) -> bool:
    """效果不存在或视觉验证明确失败；不把未知结果当成缺失。"""
    return (
        flag.get("effect_visible") is False
        or flag.get("visual_difference_observed") is False
        or str(flag.get("effect_salience") or "") == "none"
    )


def _attach_pending_flag_trace(stage_id: str, stage: dict[str, Any], trace: dict[str, Any]) -> dict[str, Any]:
    """记录尚在留出验证期的观察字段，不让它们抢先影响 severity。"""
    if stage_id == "S3":
        c = stage.get("creator_s3")
        b = stage.get("benchmark_s3")
        framing: dict[str, Any] = {}
        if isinstance(c, dict) and "process_framing_met" in c:
            framing["creator"] = c.get("process_framing_met")
        if isinstance(b, dict) and "process_framing_met" in b:
            framing["benchmark"] = b.get("process_framing_met")
        if framing:
            trace["s3_process_framing"] = framing
        action_proof: dict[str, Any] = {}
        if isinstance(c, dict) and "action_proof_met" in c:
            action_proof["creator"] = c.get("action_proof_met")
        if isinstance(b, dict) and "action_proof_met" in b:
            action_proof["benchmark"] = b.get("action_proof_met")
        if action_proof:
            trace["s3_action_proof"] = action_proof
        contact: dict[str, Any] = {}
        application_change: dict[str, Any] = {}
        continuity: dict[str, Any] = {}
        for role, flag in (("creator", c), ("benchmark", b)):
            if isinstance(flag, dict) and "action_target_contact_met" in flag:
                contact[role] = flag.get("action_target_contact_met")
            if isinstance(flag, dict) and "action_application_change_visible" in flag:
                application_change[role] = flag.get("action_application_change_visible")
            if isinstance(flag, dict) and "critical_action_continuity_met" in flag:
                continuity[role] = flag.get("critical_action_continuity_met")
        if contact:
            trace["s3_action_target_contact"] = contact
        if application_change:
            trace["s3_action_application_change"] = application_change
        if continuity:
            trace["s3_critical_action_continuity"] = continuity
        presentation: dict[str, dict[str, Any]] = {}
        for role, flag in (("creator", c), ("benchmark", b)):
            if isinstance(flag, dict):
                presentation[role] = {
                    key: flag.get(key)
                    for key in ("distinct_personas_met", "steps_clear_met", "pov_immersive_met")
                    if key in flag
                }
        if presentation:
            trace["s3_presentation_observations"] = presentation
    elif stage_id == "S6":
        c = stage.get("creator_s6")
        b = stage.get("benchmark_s6")
        module_checks: dict[str, dict[str, Any]] = {}
        for role, flag in (("creator", c), ("benchmark", b)):
            if isinstance(flag, dict):
                module_checks[role] = {
                    key: flag.get(key)
                    for key in ("price_anchor_met", "urgency_evidence_met", "gift_stack_met", "guarantee_clear_met")
                    if key in flag
                }
        if module_checks:
            trace["s6_module_observations"] = module_checks
        effect_summary_dependency: dict[str, Any] = {}
        for role, flag in (("creator", c), ("benchmark", b)):
            if not isinstance(flag, dict) or str(flag.get("module_type") or "") != "D":
                continue
            dependency = flag.get("computed_depends_on_valid_s4")
            if dependency not in {True, False}:
                dependency = flag.get("depends_on_valid_s4")
            effect_summary_dependency[role] = dependency
        if effect_summary_dependency:
            trace["s6_effect_summary_dependency"] = effect_summary_dependency
    return trace


def _s4_effect_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S4 效果因果 flag：效果要可见，也要可信地由产品造成；只有结果没过程不能直接高分。"""
    c = stage.get("creator_s4")
    b = stage.get("benchmark_s4")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None

    def explicit_quality_keys(flag: dict[str, Any]) -> bool:
        return flag.get("visual_difference_observed") in {True, False} or flag.get("module_constraints_met") in {True, False}

    def quality_met(flag: dict[str, Any]) -> bool:
        if not explicit_quality_keys(flag):
            return True
        return flag.get("visual_difference_observed") is True and flag.get("module_constraints_met") is True

    def side_exec(flag: dict[str, Any]) -> float:
        salience = str(flag.get("effect_salience") or "none")
        if flag.get("effect_visible") is False or salience == "none":
            return 0.0
        if flag.get("tamper_or_cut_risk") is True:
            return 0.5
        if flag.get("requires_close_inspection") is True or salience == "subtle":
            return 0.5
        if flag.get("effect_type") == "aesthetic_display":
            return 1.0 if flag.get("effect_proposition_matched") is True else 0.5
        if flag.get("result_only_without_process") is True:
            if flag.get("effect_attribution_supported") is True and flag.get("effect_proposition_matched") is True:
                return 1.0
            return 0.5
        if flag.get("process_linked_effect") is True and flag.get("effect_attribution_supported") is True:
            if (
                flag.get("effect_type") in {"process_visualization", "quantified_test"}
                and salience == "strong"
                and flag.get("effect_proposition_matched") is True
                and flag.get("closeup_or_focus_met") is True
                and flag.get("effect_maximized") is True
                and quality_met(flag)
            ):
                return 2.0
            if (
                salience == "strong"
                and flag.get("effect_proposition_matched") is True
                and flag.get("comparison_control_met") is True
                and flag.get("closeup_or_focus_met") is True
                and flag.get("effect_maximized") is True
                and quality_met(flag)
            ):
                return 2.0
            if salience in {"clear", "strong"} and flag.get("effect_proposition_matched") is True:
                return 1.0
            return 0.5
        return 1.0 if flag.get("effect_attribution_supported") is True and flag.get("effect_proposition_matched") is True else 0.5

    return {"creator_exec": side_exec(c), "bench_exec": side_exec(b)}


def _s5_trust_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S5 信任放大 flag：硬信任可到 2，软信任封顶 1，口播孤证封顶 0.5。"""
    c = stage.get("creator_s5")
    b = stage.get("benchmark_s5")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None
    valid_bases = {"authority", "traceable_data", "independent_user", "social_consensus", "process_transparency"}

    def side_exec(flag: dict[str, Any]) -> float:
        if flag.get("exists") is False:
            return 0.0
        trust_type = str(flag.get("trust_evidence_type") or "unknown")
        trust_basis = str(flag.get("trust_basis") or "unknown")
        if trust_type in {"none", "unknown"}:
            return 0.0
        if trust_basis not in valid_bases:
            return 0.0
        if flag.get("independent_trust_purpose") is not True or flag.get("duplicates_other_stage") is True:
            return 0.0
        if flag.get("risky_or_unsupported") is True:
            return 0.5
        if flag.get("product_relevance_met") is not True:
            return 0.5
        if flag.get("voice_only") is True:
            return 0.5
        if trust_type == "soft":
            return 1.0
        if flag.get("trust_source_credible") is True and flag.get("trust_claim_specific") is True:
            return 2.0 if flag.get("trust_source_visible") is True else 1.0
        if flag.get("trust_source_credible") is True or flag.get("trust_claim_specific") is True:
            return 1.0
        return 0.5

    return {"creator_exec": side_exec(c), "bench_exec": side_exec(b)}


def _s5_has_any_trust(stage: dict[str, Any]) -> bool:
    """S5 flag 已输出时，软信任也算设计了信任环节，避免硬背书闸误杀。"""
    for key in ("creator_s5", "benchmark_s5"):
        flag = stage.get(key)
        if not isinstance(flag, dict):
            continue
        if (
            flag.get("exists") is not False
            and str(flag.get("trust_evidence_type") or "unknown") not in {"none", "unknown"}
            and str(flag.get("trust_basis") or "unknown") in {
                "authority", "traceable_data", "independent_user", "social_consensus", "process_transparency",
            }
            and flag.get("independent_trust_purpose") is True
            and flag.get("duplicates_other_stage") is not True
        ):
            return True
    return False


def _s5_absence_is_explicit(flag: dict[str, Any]) -> bool:
    """只允许 repair 已确认的 absence 触发 S5 ceiling。

    直接调用 derive 的旧结果没有私有状态时，仍兼容明确的 ``trust_basis=none``
    旗标；经过新 repair 的 unknown 状态则优先于这个兼容回退，绝不触发 ceiling。
    """
    status = flag.get("_s5_source_status")
    if status is not None:
        return status == "explicit_absent"
    return (
        flag.get("exists") is False
        and flag.get("independent_trust_purpose") is False
        and flag.get("duplicates_other_stage") is False
        and str(flag.get("trust_basis") or "") in {"none", "product_claim", "offer_or_spec"}
        and str(flag.get("trust_evidence_type") or "") in {"none", "unknown"}
    )


def _s6_cta_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S6 CTA flag：购买指令和行动路径是主轴，利益/紧迫/保障是转化放大。"""
    c = stage.get("creator_s6")
    b = stage.get("benchmark_s6")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None

    def side_exec(flag: dict[str, Any]) -> float:
        if flag.get("exists") is False:
            return 0.0
        if flag.get("ending_position_met") is not True:
            return 0.0
        direct = flag.get("direct_order_met") is True
        path = flag.get("action_path_clear") is True
        soft_invitation = flag.get("soft_purchase_invitation_met") is True
        fit_value = flag.get("module_fit_met")
        fit = fit_value is True
        amplifier = any(
            flag.get(key) is True
            for key in ("offer_or_incentive_clear", "urgency_met", "product_value_recalled")
        )
        if flag.get("compliance_risk") is True:
            return 0.5 if direct or path or soft_invitation else 0.0
        if not direct and not path:
            # 结尾的"感兴趣/需要的朋友 + 明确优惠"仍是软促单：没有路径，绝对质量仍弱，
            # 但不能与完全没有面向用户购买动作混为一谈。
            if soft_invitation and flag.get("offer_or_incentive_clear") is True:
                return 1.5 if fit else 1.0
            if soft_invitation:
                return 0.5
            return 0.0
        # S6-D 的效果总结素材仍要依赖有效 S4（写入 trace/绝对质量审计），但 CTA 的核心是
        # 结尾购买指令和行动路径。若这两项和利益点已成立，不能仅因模型把表达归成 D 就把 CTA
        # 降为无效；否则会把模块标签误差变成类型层级偏见。
        if fit_value is False:
            return 0.5
        if direct and path and fit and amplifier:
            return 2.0
        if direct and path:
            return 1.0
        return 1.0 if fit and amplifier else 0.5

    return {"creator_exec": side_exec(c), "bench_exec": side_exec(b)}


class SeverityConstraint(NamedTuple):
    """一个只允许收窄 severity 区间的确定性约束。"""

    kind: str
    level: str
    rule: str
    reason: str
    evidence_ids: tuple[str, ...] = ()


def _normalize_model_severity(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SEVERITY_RANK else "medium"


def _flag_evidence_ids(flag: Any) -> tuple[str, ...]:
    if not isinstance(flag, dict):
        return ()
    return tuple(sorted({str(value).strip() for value in flag.get("evidence_ids") or [] if str(value).strip()}))


def _role_evidence_units(result: dict[str, Any] | None, role: str) -> dict[str, dict[str, Any]]:
    understanding = result.get("video_understanding") if isinstance(result, dict) else None
    side = understanding.get(role) if isinstance(understanding, dict) else None
    units = side.get("evidence_units") if isinstance(side, dict) else None
    return {
        str(unit.get("id")): unit
        for unit in units or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }


def _flag_strength(result: dict[str, Any] | None, role: str, flag: Any) -> dict[str, Any]:
    """从 Stage1 evidence_units 汇总 flag 的最弱证据强度。

    不在这里猜测缺失字段。没有引用、引用不存在或 unit 没有 canonical
    evidence_strength 都会被明确记录，不能被当作 absent/false。
    """
    ids = _flag_evidence_ids(flag)
    if not ids:
        return {
            "status": "missing_field",
            "strength": None,
            "evidence_ids": [],
        }

    units = _role_evidence_units(result, role)
    missing_ids = [evidence_id for evidence_id in ids if evidence_id not in units]
    if missing_ids:
        return {
            "status": "missing_field",
            "strength": None,
            "evidence_ids": list(ids),
            "missing_evidence_ids": missing_ids,
        }

    strengths: list[str] = []
    for evidence_id in ids:
        raw_strength = units[evidence_id].get("evidence_strength")
        strength = str(raw_strength or "").strip().lower()
        if not strength:
            return {
                "status": "missing_evidence_strength",
                "strength": None,
                "evidence_ids": list(ids),
            }
        if strength not in EVIDENCE_STRENGTHS:
            return {
                "status": "uncertain_evidence_strength",
                "strength": None,
                "evidence_ids": list(ids),
                "invalid_evidence_strength": strength,
            }
        strengths.append(strength)
    weakest = max(strengths, key=lambda value: EVIDENCE_STRENGTHS.index(value))
    if weakest not in _EXPLICIT_STRENGTHS:
        return {
            "status": "uncertain_evidence_strength",
            "strength": weakest,
            "evidence_ids": list(ids),
        }
    return {"status": "eligible", "strength": weakest, "evidence_ids": list(ids)}


def _pair_flags(stage: dict[str, Any], suffix: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    creator = stage.get(f"creator_{suffix}")
    benchmark = stage.get(f"benchmark_{suffix}")
    return (
        creator if isinstance(creator, dict) else None,
        benchmark if isinstance(benchmark, dict) else None,
    )


def _required_bool_state(flag: dict[str, Any], keys: tuple[str, ...]) -> str:
    values = [flag.get(key) for key in keys]
    if all(value is True or value is False for value in values):
        return "explicit"
    return "uncertain_fact"


def _has_required_evidence(creator: dict[str, Any], benchmark: dict[str, Any]) -> bool:
    return bool(_flag_evidence_ids(creator) and _flag_evidence_ids(benchmark))


def resolve_severity(
    model_severity: str,
    floors: tuple[SeverityConstraint, ...] = (),
    ceilings: tuple[SeverityConstraint, ...] = (),
) -> dict[str, Any]:
    """唯一 severity resolver；聚合规则是 max(floor) / min(ceiling)。"""
    model = _normalize_model_severity(model_severity)
    ordered_floors = tuple(sorted(floors, key=lambda item: (item.level, item.rule, item.reason)))
    ordered_ceilings = tuple(sorted(ceilings, key=lambda item: (item.level, item.rule, item.reason)))
    floor_rank = max((SEVERITY_RANK[item.level] for item in ordered_floors), default=0)
    ceiling_rank = min((SEVERITY_RANK[item.level] for item in ordered_ceilings), default=len(SEVERITIES) - 1)
    constraints = tuple(sorted((*ordered_floors, *ordered_ceilings), key=lambda item: (item.kind, item.level, item.rule, item.reason)))
    if floor_rank > ceiling_rank:
        return {
            "severity": model,
            "status": "conflict",
            "model_severity": model,
            "floor": SEVERITIES[floor_rank],
            "ceiling": SEVERITIES[ceiling_rank],
            "constraints": constraints,
            "phase_c_candidate": True,
        }
    resolved_rank = max(floor_rank, min(SEVERITY_RANK[model], ceiling_rank))
    return {
        "severity": SEVERITIES[resolved_rank],
        "status": "constrained" if constraints else "model_preserved",
        "model_severity": model,
        "floor": SEVERITIES[floor_rank] if ordered_floors else None,
        "ceiling": SEVERITIES[ceiling_rank] if ordered_ceilings else None,
        "constraints": constraints,
        "phase_c_candidate": False,
    }


def _constraint_dict(item: SeverityConstraint) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "level": item.level,
        "rule": item.rule,
        "reason": item.reason,
        "evidence_ids": list(item.evidence_ids),
    }


def _stage_strength_gate(
    result: dict[str, Any] | None,
    creator_role: str,
    creator: dict[str, Any],
    benchmark_role: str,
    benchmark: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    creator_state = _flag_strength(result, creator_role, creator)
    benchmark_state = _flag_strength(result, benchmark_role, benchmark)
    evidence_ids = sorted(set(creator_state.get("evidence_ids", [])) | set(benchmark_state.get("evidence_ids", [])))
    if creator_state.get("status") != "eligible" or benchmark_state.get("status") != "eligible":
        status = creator_state.get("status") if creator_state.get("status") != "eligible" else benchmark_state.get("status")
        return str(status), evidence_ids, {"creator": creator_state, "benchmark": benchmark_state}
    if creator_state.get("strength") not in _EXPLICIT_STRENGTHS or benchmark_state.get("strength") not in _EXPLICIT_STRENGTHS:
        return "insufficient_strength", evidence_ids, {"creator": creator_state, "benchmark": benchmark_state}
    return "eligible", evidence_ids, {"creator": creator_state, "benchmark": benchmark_state}


def _constraint_evaluation(rule: str, status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"rule": rule, "status": status, "reason": reason, **extra}


def _s3_basic_process(flag: dict[str, Any]) -> bool:
    return all(
        flag.get(key) is True
        for key in (
            "usage_process_visible",
            "core_selling_point_visible",
            "action_proof_met",
            "action_target_contact_met",
            "action_application_change_visible",
            "critical_action_continuity_met",
        )
    )


def _s3_basic_process_state(flag: dict[str, Any]) -> str:
    keys = (
        "usage_process_visible",
        "core_selling_point_visible",
        "action_proof_met",
        "action_target_contact_met",
        "action_application_change_visible",
        "critical_action_continuity_met",
    )
    return _required_bool_state(flag, keys)


def _s4_credible_effect(flag: dict[str, Any]) -> bool:
    salience = str(flag.get("effect_salience") or "")
    return (
        flag.get("effect_visible") is True
        and salience in {"clear", "strong"}
        and flag.get("effect_proposition_matched") is True
        and flag.get("effect_attribution_supported") is True
        and flag.get("requires_close_inspection") is False
        and flag.get("tamper_or_cut_risk") is False
    )


def _s4_credible_effect_state(flag: dict[str, Any]) -> str:
    keys = (
        "effect_visible",
        "effect_proposition_matched",
        "effect_attribution_supported",
        "requires_close_inspection",
        "tamper_or_cut_risk",
    )
    return _required_bool_state(flag, keys)


def _derive_one(
    stage_id: str,
    stage: dict[str, Any],
    weights: dict[str, float] | None = None,
    painpoints: list[str] | None = None,
    shake: dict[str, bool] | None = None,
    endorsement: dict[str, _Endorsement] | None = None,
    allow_legacy_text_fallback: bool = False,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据离散事实收集约束，再交给 resolver；旧参数仅保留调用兼容性。"""
    del weights, painpoints, shake, allow_legacy_text_fallback
    model = _normalize_model_severity(stage.get("model_severity") or stage.get("severity"))
    facts = facts if isinstance(facts, dict) else {}
    floors: list[SeverityConstraint] = []
    ceilings: list[SeverityConstraint] = []
    evaluations: list[dict[str, Any]] = []

    def skip(rule: str, status: str, reason: str, **extra: Any) -> None:
        evaluations.append(_constraint_evaluation(rule, status, reason, **extra))

    def add(kind: str, level: str, rule: str, reason: str, evidence_ids: tuple[str, ...] | list[str] = ()) -> None:
        constraint = SeverityConstraint(kind, level, rule, reason, tuple(sorted(set(evidence_ids))))
        (floors if kind == "floor" else ceilings).append(constraint)
        evaluations.append(_constraint_evaluation(rule, "triggered", reason, kind=kind, level=level, evidence_ids=list(constraint.evidence_ids)))

    s1_state = (stage.get("_postprocess_state") or {}).get(_S1_REPAIR_STATE_KEY)
    s1_ready = s1_state == _S1_REPAIR_STATE_VALUE

    if stage_id == "S1":
        rule = "S1_hook_exists_floor"
        creator, benchmark = _pair_flags(stage, "hook")
        if not s1_ready:
            skip(rule, "precondition_missing", "S1 hook facts 未经过 repair_s1_hook_boundaries。")
        elif creator is None or benchmark is None:
            skip(rule, "missing_field", "S1 hook flag 缺失。")
        elif _required_bool_state(creator, ("exists",)) != "explicit" or _required_bool_state(benchmark, ("exists",)) != "explicit":
            skip(rule, "uncertain_fact", "S1 Hook exists 不是明确 true/false。")
        elif not _has_required_evidence(creator, benchmark):
            skip(rule, "missing_field", "S1 Hook 缺少双方 evidence_ids。")
        elif benchmark.get("exists") is True and creator.get("exists") is False:
            add("floor", "large", rule, "标杆有 Hook、达人明确没有 Hook。", (*_flag_evidence_ids(creator), *_flag_evidence_ids(benchmark)))
        else:
            skip(rule, "predicate_not_met", "双方 Hook 存在性未形成结构性缺口。")

        for rule, predicate, reason in (
            (
                "S1_landing_floor",
                lambda c, b: c.get("landing_met") is False and b.get("landing_met") is True,
                "标杆 landing 成立、达人 landing 明确不成立。",
            ),
            (
                "S1_proposition_anchor_floor",
                lambda c, b: c.get("anchors_proposition") is False and b.get("anchors_proposition") is True,
                "标杆 Hook 锚定本品命题、达人明确未锚定。",
            ),
        ):
            creator, benchmark = _pair_flags(stage, "hook")
            if not s1_ready:
                skip(rule, "precondition_missing", "S1 hook facts 未经过 repair_s1_hook_boundaries。")
                continue
            if creator is None or benchmark is None:
                skip(rule, "missing_field", "S1 hook flag 缺失。")
                continue
            key = "landing_met" if rule == "S1_landing_floor" else "anchors_proposition"
            if _required_bool_state(creator, (key,)) != "explicit" or _required_bool_state(benchmark, (key,)) != "explicit":
                skip(rule, "uncertain_fact", f"S1 {key} 不是明确事实。")
                continue
            if not predicate(creator, benchmark):
                skip(rule, "predicate_not_met", "S1 比较型下限条件未满足。")
                continue
            status, ids, detail = _stage_strength_gate(facts, "creator", creator, "benchmark", benchmark)
            if status != "eligible":
                skip(rule, status, "S1 比较型下限需要 direct/explicit evidence_strength。", evidence=detail)
                continue
            add("floor", "medium", rule, reason, ids)

    if stage_id == "S2":
        rule = "S2_contract_floor"
        creator, benchmark = _pair_flags(stage, "s2")
        keys = ("handoff_met", "product_identity_clear", "product_role_clear")
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S2 contract flag 缺失。")
        elif creator.get("merged_with_s3") is True:
            skip(rule, "predicate_not_met", "S2 已与 S3 合并，不重复处罚独立 S2。")
        elif _required_bool_state(creator, (*keys, "merged_with_s3")) != "explicit" or _required_bool_state(benchmark, (*keys,)) != "explicit":
            skip(rule, "uncertain_fact", "S2 契约字段未全部明确。")
        elif not _has_required_evidence(creator, benchmark):
            skip(rule, "missing_field", "S2 契约缺少双方 evidence_ids。")
        elif all(benchmark.get(key) is True for key in keys) and any(creator.get(key) is False for key in keys):
            status, ids, detail = _stage_strength_gate(facts, "creator", creator, "benchmark", benchmark)
            if status == "eligible":
                add("floor", "medium", rule, "标杆完成 S2 承接契约、达人明确缺少关键契约。", ids)
            else:
                skip(rule, status, "S2 比较型下限需要 direct/explicit evidence_strength。", evidence=detail)
        else:
            skip(rule, "predicate_not_met", "S2 未形成标杆完整、达人缺失的契约断层。")

    if stage_id == "S3":
        creator, benchmark = _pair_flags(stage, "s3")
        rule = "S3_real_usage_floor"
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S3 usage flag 缺失。")
        elif _s3_basic_process_state(benchmark) != "explicit" or _s3_basic_process_state(creator) != "explicit":
            skip(rule, "uncertain_fact", "S3 核心使用事实不完整。")
        elif not _has_required_evidence(creator, benchmark):
            skip(rule, "missing_field", "S3 核心使用断层缺少双方 evidence_ids。")
        elif _s3_complete_real_usage(benchmark) and _s3_explicitly_missing_real_usage(creator):
            add("floor", "large", rule, "标杆完成可复核真实使用、达人明确缺少真实使用。", (*_flag_evidence_ids(creator), *_flag_evidence_ids(benchmark)))
        else:
            skip(rule, "predicate_not_met", "S3 未形成完整使用过程断层。")

        rule = "S3_thin_presentation_floor"
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S3 usage flag 缺失。")
        elif _s3_basic_process_state(creator) != "explicit" or _s3_basic_process_state(benchmark) != "explicit":
            skip(rule, "uncertain_fact", "S3 薄呈现规则的核心事实不完整。")
        elif benchmark.get("process_framing_met") is not True or creator.get("process_framing_met") is not False:
            skip(rule, "predicate_not_met", "S3 未形成标杆做厚、达人单薄的明确差异。")
        else:
            status, ids, detail = _stage_strength_gate(facts, "creator", creator, "benchmark", benchmark)
            if status == "eligible":
                add("floor", "medium", rule, "双方都有基础使用过程，但标杆明确做厚、达人呈现单薄。", ids)
            else:
                skip(rule, status, "S3 薄呈现下限需要 direct/explicit evidence_strength。", evidence=detail)

    if stage_id == "S4":
        creator, benchmark = _pair_flags(stage, "s4")
        rule = "S4_visible_effect_floor"
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S4 effect flag 缺失。")
        elif _s4_credible_effect_state(benchmark) != "explicit" or _s4_credible_effect_state(creator) != "explicit":
            skip(rule, "uncertain_fact", "S4 可见效果事实不完整。")
        elif not _has_required_evidence(creator, benchmark):
            skip(rule, "missing_field", "S4 效果断层缺少双方 evidence_ids。")
        elif _s4_strong_visible_effect(benchmark) and (
            _s4_explicitly_missing_visible_effect(creator)
            or (creator.get("result_only_without_process") is True and creator.get("process_linked_effect") is False)
        ):
            add("floor", "large", rule, "标杆完成强而可见的效果证明、达人明确缺少可复核效果。", (*_flag_evidence_ids(creator), *_flag_evidence_ids(benchmark)))
        else:
            skip(rule, "predicate_not_met", "S4 未形成明确效果证明断层。")

        rule = "S4_thin_effect_floor"
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S4 effect flag 缺失。")
        elif _s4_credible_effect_state(benchmark) != "explicit" or _s4_credible_effect_state(creator) != "explicit":
            skip(rule, "uncertain_fact", "S4 薄效果规则的事实不完整。")
        elif not _s4_credible_effect(creator) or not _s4_credible_effect(benchmark):
            skip(rule, "predicate_not_met", "双方没有同时形成可信效果，薄效果规则不触发。")
        elif not (str(benchmark.get("effect_salience") or "") == "strong" and benchmark.get("effect_maximized") is True):
            skip(rule, "predicate_not_met", "标杆没有明确做强和最大化效果。")
        elif str(creator.get("effect_salience") or "") == "strong" and creator.get("effect_maximized") is True:
            skip(rule, "predicate_not_met", "达人效果同样做强，不构成薄效果差距。")
        else:
            status, ids, detail = _stage_strength_gate(facts, "creator", creator, "benchmark", benchmark)
            if status == "eligible":
                add("floor", "medium", rule, "双方都有可信效果，但标杆效果更显著、更聚焦。", ids)
            else:
                skip(rule, status, "S4 薄效果下限需要 direct/explicit evidence_strength。", evidence=detail)

    if stage_id == "S5":
        rule = "S5_no_trust_ceiling"
        creator, benchmark = _pair_flags(stage, "s5")
        b_endorsement = (endorsement or {}).get("benchmark") or _NO_ENDORSEMENT
        c_endorsement = (endorsement or {}).get("creator") or _NO_ENDORSEMENT
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S5 trust flag 缺失。")
        elif not b_endorsement.available or not c_endorsement.available:
            skip(rule, "missing_field", "S5 Stage1 背书观察字段缺失。")
        elif _required_bool_state(creator, ("exists", "independent_trust_purpose", "duplicates_other_stage")) != "explicit" or _required_bool_state(benchmark, ("exists", "independent_trust_purpose", "duplicates_other_stage")) != "explicit":
            skip(rule, "uncertain_fact", "S5 信任事实不完整，不能把 unknown 当作无背书。")
        elif b_endorsement.verbal or b_endorsement.visual or c_endorsement.verbal or c_endorsement.visual:
            skip(rule, "predicate_not_met", "至少一侧存在明确硬背书观察。")
        elif not _s5_absence_is_explicit(creator) or not _s5_absence_is_explicit(benchmark):
            skip(rule, "uncertain_fact", "S5 来源 absence 未被明确确认，不能把 unknown 当作无背书。")
        elif _s5_has_any_trust(stage):
            skip(rule, "predicate_not_met", "至少一侧存在明确软/结构化信任材料。")
        elif creator.get("exists") is False and benchmark.get("exists") is False:
            add("ceiling", "medium", rule, "双方明确没有信任放大材料，模型不能把该阶段判为 large。", (*_flag_evidence_ids(creator), *_flag_evidence_ids(benchmark)))
        else:
            skip(rule, "predicate_not_met", "双方没有同时明确声明 S5 不存在。")

    if stage_id == "S6":
        rule = "S6_creator_cta_ceiling"
        creator, benchmark = _pair_flags(stage, "s6")
        if creator is None or benchmark is None:
            skip(rule, "missing_field", "S6 CTA flag 缺失。")
        elif _required_bool_state(creator, ("direct_order_met", "action_path_clear")) != "explicit" or _required_bool_state(benchmark, ("exists",)) != "explicit":
            skip(rule, "uncertain_fact", "S6 CTA 事实不完整。")
        elif not _has_required_evidence(creator, benchmark):
            skip(rule, "missing_field", "S6 CTA 安全封顶缺少双方 evidence_ids。")
        elif benchmark.get("exists") is False and (creator.get("direct_order_met") is True or creator.get("action_path_clear") is True):
            add("ceiling", "small", rule, "达人有明确购买路径、标杆没有独立 CTA，不因缺少促销放大话术制造差距。", (*_flag_evidence_ids(creator), *_flag_evidence_ids(benchmark)))
        else:
            skip(rule, "predicate_not_met", "S6 未形成达人明确优于标杆 CTA 的安全封顶条件。")

    resolved = resolve_severity(model, tuple(floors), tuple(ceilings))
    constraint_reason = "；".join(item.reason for item in resolved["constraints"]) if resolved["constraints"] else "无确定性约束，保留模型 severity。"
    trace: dict[str, Any] = {
        "status": resolved["status"],
        "severity": resolved["severity"],
        "model_severity": model,
        "resolver": "floor_ceiling_v1",
        "floor": resolved.get("floor"),
        "ceiling": resolved.get("ceiling"),
        "constraints": [_constraint_dict(item) for item in resolved["constraints"]],
        "constraint_evaluations": evaluations,
        "phase_c_candidate": resolved["phase_c_candidate"],
        "reason": constraint_reason,
    }
    if resolved["status"] == "conflict":
        trace["reason"] = "floor 与 ceiling 冲突，保留模型 severity，交 Phase C 复核。"
        trace["conflict"] = {"floor": resolved["floor"], "ceiling": resolved["ceiling"]}

    # 执行分仍可作为离线审计信息，但明确不再进入 severity resolver。
    observed = None
    helper = {
        "S1": _s1_hook_exec,
        "S2": _s2_contract_exec,
        "S3": _s3_usage_exec,
        "S4": _s4_effect_exec,
        "S5": _s5_trust_exec,
        "S6": _s6_cta_exec,
    }.get(stage_id)
    if helper is not None:
        try:
            observed = helper(stage)
        except Exception:
            observed = None
    if not isinstance(observed, dict):
        creator_exec = stage.get("creator_execution")
        benchmark_exec = stage.get("benchmark_execution")
        if isinstance(creator_exec, (int, float)) and not isinstance(creator_exec, bool) and isinstance(benchmark_exec, (int, float)) and not isinstance(benchmark_exec, bool):
            observed = {"creator_exec": float(creator_exec), "bench_exec": float(benchmark_exec)}
    if isinstance(observed, dict):
        creator_observed = observed.get("creator_exec")
        benchmark_observed = observed.get("bench_exec")
        if has_multimodal_assessment(stage):
            creator_observed = multimodal_execution(stage_id, stage, "creator", creator_observed)
            benchmark_observed = multimodal_execution(stage_id, stage, "benchmark", benchmark_observed)
        trace["execution_observation"] = {
            "creator": creator_observed,
            "benchmark": benchmark_observed,
            "source": "diagnostic_only",
        }
        trace["derived_creator_execution"] = creator_observed
        trace["derived_benchmark_execution"] = benchmark_observed
    if has_multimodal_assessment(stage):
        trace["multimodal_integration"] = {
            "channel_requirement": channel_requirement_for(stage_id),
            "sides": {
                role: {
                    "dominant_channel": (stage.get(f"{role}_multimodal") or {}).get("dominant_channel"),
                    "cross_channel_relation": (stage.get(f"{role}_multimodal") or {}).get("cross_channel_relation"),
                    "integrated_effect": (stage.get(f"{role}_multimodal") or {}).get("integrated_effect"),
                    "compensation_applied": (stage.get(f"{role}_multimodal") or {}).get("compensation_applied"),
                }
                for role in ("creator", "benchmark")
                if isinstance(stage.get(f"{role}_multimodal"), dict)
            },
        }
    return _attach_pending_flag_trace(stage_id, stage, trace)


def critical_severity_stages(result: dict[str, Any]) -> list[str]:
    """返回 resolver 检出的 floor/ceiling 冲突阶段，供现有 Phase C 预算复用。"""
    out: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        trace = stage.get("severity_derivation") or {}
        if trace.get("phase_c_candidate") is True:
            match = _STAGE_RE.match(str(stage.get("stage") or ""))
            if match:
                out.append(match.group(1))
    return out


def derive_severity_from_facts(result: dict[str, Any], analysis: dict[str, Any] | None = None) -> None:
    """为每个阶段收集确定性约束并通过唯一 resolver 写入最终 severity。"""
    del analysis
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return
    endorsement = ({side: _side_endorsement(result, side) for side in ("creator", "benchmark")}
                   if any("S5" in str(s.get("stage") or "") for s in stages if isinstance(s, dict)) else {})
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        match = _STAGE_RE.match(str(stage.get("stage") or ""))
        if not match:
            continue
        model = _normalize_model_severity(stage.get("model_severity") or stage.get("severity"))
        try:
            trace = _derive_one(
                match.group(1),
                stage,
                endorsement=endorsement,
                facts=result,
            )
        except Exception as exc:
            trace = {
                "status": "error",
                "severity": model,
                "model_severity": model,
                "resolver": "floor_ceiling_v1",
                "phase_c_candidate": False,
                "reason": f"约束解析异常已降级，保留模型 severity：{exc}",
            }
        stage["severity"] = _normalize_model_severity(trace.get("severity") or model)
        stage["severity_derivation"] = trace
