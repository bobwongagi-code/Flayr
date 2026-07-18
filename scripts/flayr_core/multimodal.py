"""S1-S6 跨模态综合评估合同与确定性执行分融合。"""

from __future__ import annotations

import json
from typing import Any


MULTIMODAL_CHANNELS = ("visual", "speech", "text", "sound_rhythm")
MULTIMODAL_IMPACTS = {
    "strong_positive", "positive", "neutral", "negative", "strong_negative", "absent", "unknown",
}
MULTIMODAL_RELATIONS = {
    "reinforcing", "complementary", "neutral", "conflicting", "distracting", "unknown",
}
MULTIMODAL_EFFECTS = {"strong", "effective", "weak", "missing", "unknown"}
MULTIMODAL_DOMINANT_CHANNELS = {*MULTIMODAL_CHANNELS, "none", "unknown"}

MULTIMODAL_CHANNEL_REQUIREMENTS = {
    "S1": {
        "level": "any_channel_sufficient",
        "required_signal": "stay_motivation",
        "policy": "任一渠道都可主导留人；强画面可补偿弱或缺失口播。弱渠道只有造成困惑、冲突或抢注意力时才扣减。",
    },
    "S2": {
        "level": "any_channel_sufficient",
        "required_signal": "product_identity_and_role",
        "policy": "画面、口播或字幕可承担产品身份与角色说明；但最终仍须自然承接 S1，并让用户知道产品是什么、为什么出现。",
    },
    "S3": {
        "level": "required_evidence_with_amplification",
        "required_signal": "visible_usage_process",
        "policy": "真实使用过程与关键动作可见是硬条件；口播、字幕、声音和节奏只能增强步骤理解、专业性和卖点证明，不能替代使用演示。",
    },
    "S4": {
        "level": "required_evidence_with_amplification",
        "required_signal": "visible_effect",
        "policy": "可见效果是硬条件；口播、字幕、声音和节奏只能放大已经看得见的效果，不能把不可见效果说成成立。",
    },
    "S5": {
        "level": "source_grounded",
        "required_signal": "credible_source",
        "policy": "可信来源和与本品相关的信任主张是硬条件；来源存在后可由清晰展示和解释增强，通用氛围不能替代或凭空放大来源。",
    },
    "S6": {
        "level": "required_evidence_with_amplification",
        "required_signal": "explicit_purchase_action",
        "policy": "购买邀请或行动指令是硬条件；口播、字幕、价格/赠品画面和声音节奏可组合放大，但氛围不能替代购买动作。",
    },
}

MULTIMODAL_STAGE_POLICIES = {
    stage_id: str(requirement["policy"])
    for stage_id, requirement in MULTIMODAL_CHANNEL_REQUIREMENTS.items()
}

MULTIMODAL_PROMPT_CONTRACT = (
    "每个 S1-S6 stage 必须分别输出 creator_multimodal 与 benchmark_multimodal。先读取该侧阶段内全部画面、"
    "口播语义、屏幕文字、BGM/音效/语气/剪辑节奏，再判断它们组合后的净效果；禁止按最弱渠道一票否决，"
    "也禁止把四个渠道等权相加。channel_impacts 的枚举：strong_positive=该渠道直接承担阶段核心任务；"
    "positive=明确增强；neutral=存在但不 materially 改变结果；negative=制造理解成本或轻度干扰；"
    "strong_negative=与主信号冲突或明显抢走注意力；absent=没有该渠道；unknown=证据不足。"
    "缺失渠道不是负面渠道；弱但中性的口播不能拖垮强视觉，含糊或冲突口播则可形成 negative。"
    "cross_channel_relation 只能是 reinforcing/complementary/neutral/conflicting/distracting/unknown；"
    "integrated_effect 只能是 strong/effective/weak/missing/unknown，回答多渠道组合后该阶段核心任务是否完成。"
    "compensation_applied 只在一个强正向渠道实际弥补另一个弱、缺失或轻度负向渠道时为 true；"
    "integration_reason 必须点明主导渠道、补偿或冲突关系及最终净效果，并引用 channel_evidence_ids 中的事实。"
)


def multimodal_output_example() -> dict[str, Any]:
    return {
        "channel_impacts": {
            "visual": "strong_positive",
            "speech": "neutral",
            "text": "positive",
            "sound_rhythm": "neutral",
        },
        "channel_evidence_ids": {
            "visual": ["C1"],
            "speech": ["C1"],
            "text": ["C1"],
            "sound_rhythm": ["C1"],
        },
        "dominant_channel": "visual",
        "cross_channel_relation": "complementary",
        "integrated_effect": "strong",
        "compensation_applied": True,
        "integration_reason": "强视觉结果直接完成阶段任务，普通口播没有制造冲突，字幕提供补充说明。",
    }


def render_multimodal_prompt_contract() -> str:
    """主分析、Repair 与 Phase C 共用的唯一多模态综合合同文本。"""
    return "\n".join(
        [
            "## S1-S6 跨模态综合合同",
            MULTIMODAL_PROMPT_CONTRACT,
            "各阶段渠道可替代性等级、必要信号与补偿边界：",
            json.dumps(MULTIMODAL_CHANNEL_REQUIREMENTS, ensure_ascii=False, indent=2),
            "每侧输出结构示例：",
            json.dumps(multimodal_output_example(), ensure_ascii=False, indent=2),
        ]
    )


_EFFECT_SCORE = {"missing": 0.0, "weak": 0.5, "effective": 1.0, "strong": 2.0}


def multimodal_execution(stage_id: str, stage: dict[str, Any], role: str, base_exec: float | None) -> float | None:
    """把综合净效果放进各阶段硬约束内；旧结果缺字段时保留原执行分。"""
    assessment = stage.get(f"{role}_multimodal")
    if not isinstance(assessment, dict):
        return base_exec
    effect = str(assessment.get("integrated_effect") or "unknown")
    effect_score = _EFFECT_SCORE.get(effect)
    if effect_score is None:
        return base_exec

    # S1 的核心就是综合留人净效果。开头阶段始终存在，不再用四维结构件命中数或
    # landing 二元值覆盖强视觉/强口播对其他弱渠道的合理补偿。
    if stage_id == "S1":
        return effect_score

    if base_exec is None:
        return None
    cap = float(base_exec)

    # S3 在真实动作闭环和核心卖点证明都成立时，专业口播、字幕与声音组织可以把
    # “基础演示”提升为出色演示；它们仍不能补偿缺失的使用过程。
    if stage_id == "S3":
        flag = stage.get(f"{role}_s3")
        if isinstance(flag, dict):
            missing = flag.get("missing_selling_points")
            no_missing = not isinstance(missing, list) or not any(str(item).strip() for item in missing)
            hard_process_met = (
                (flag.get("usage_process_visible") is True or flag.get("real_usage_met") is True)
                and flag.get("core_selling_point_visible") is True
                and flag.get("action_proof_met") is True
                and flag.get("action_target_contact_met") is True
                and flag.get("action_application_change_visible") is True
                and flag.get("critical_action_continuity_met") is True
                and no_missing
            )
            if hard_process_met:
                cap = 2.0

    # 其他阶段的既有执行分已经编码各自硬条件。综合层可以暴露冲突、干扰和净弱化，
    # 但不能越过产品身份、可见效果、可信来源或购买动作等硬约束。
    return min(cap, effect_score)


def channel_requirement_for(stage_id: str) -> dict[str, str]:
    """返回阶段渠道可替代性合同，供 derive trace、QA 和评估器共用。"""
    requirement = MULTIMODAL_CHANNEL_REQUIREMENTS.get(stage_id)
    return dict(requirement) if isinstance(requirement, dict) else {}


def has_multimodal_assessment(stage: dict[str, Any]) -> bool:
    return any(isinstance(stage.get(f"{role}_multimodal"), dict) for role in ("creator", "benchmark"))
