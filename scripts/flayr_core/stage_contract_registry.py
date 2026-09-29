"""Canonical stage contract registry shared by evidence producers and consumers.

This module owns only the declarative stage vocabulary and its prompt-facing
projection.  Runtime evidence normalization and qualification remain in
``stage_evidence_contracts`` so the existing module stays the public facade.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

S3_FACT_PROMPT = (
    "S3 判断功能与使用表达：具体讲解或真实演示均可参与，不要求纯口播视频必须实际使用。"
    "讲解须说清产品功能与需求、适用对象/场景、条件或步骤的具体联系；品名、泛泛好用、裸功效声称不够。"
    "functional_explanation_met 仅在有可引用口播/字幕证据时为 true；exists 表示具体讲解或实际使用存在。"
    "usage_evidence_state 仍只记录实际使用，讲解不得把 none 改成 partial/complete；没有实际动作时视觉动作字段保持 false。"
)
S3_EXPRESSION_PROMPT = S3_FACT_PROMPT + (
    "S3 比较功能与使用表达的整体效果。双方都是口播时，比较具体性、相关性、清晰度与组织，不按篇幅或卖点数量自动定档。"
    "讲解与演示并存或跨形式比较时，双方讲解相当不代表整体相当；还须判断演示是否清楚增加了对产品功能、用法、适用情境或操作过程的理解。"
    "没有有效理解增益不加档；有证据支持的增益按其实际作用判断差距，不能因“不自动高一档”而忽略。"
    "演示形式或动作的存在本身不自动更优，也不自动触发 medium/large。整体表达有效且基本相当可为 none，双方都没有合格讲解或使用才是不涉及；证据不足仍保留 uncertain。"
    "建议先改善当前拍法的讲解，再可选提出真实使用镜头及其帮助理解的理由；不要求补齐六阶段，也不承诺销量。"
    "S4 效果证明与 S5 独立来源规则不变，功能讲解不代替效果证明或背书。"
)

# These definitions keep the registry from becoming a list of names whose
# meaning drifts between prompt authors, parsers, and reviewers.  They describe
# what may be recorded as an observation, not how much the observation is worth.
STAGE_SIGNAL_DEFINITIONS: dict[str, str] = {
    "stop_trigger": "开头可直接看到或听到的痛点、变化、冲突、承诺或问题触发点",
    "cold_audience_relevance": "不依赖品牌背景，陌生观众仅凭开头即可理解为何与自己可能有关",
    "product_or_problem_anchor": "画面、字幕或口播中可定位的产品/问题主体",
    "visual_salience": "画面中可直接观察到的显著变化、特写、动作或对比",
    "promise_or_contrast": "开头明确出现的承诺、反差或待验证悬念",
    "product_identity": "当前视频实际可见、可读或可听到的产品身份线索",
    "problem_to_product_bridge": "从前述问题/需求到该产品出现之间可观察的承接关系",
    "role_or_reason_clarity": "产品承担什么角色或为何在此时出现的直接线索",
    "benefit_link": "产品与目标利益之间被明确连接的事实",
    "natural_handoff": "从前一段问题/触发到产品出现的时间或表达承接",
    "target_contact": "产品与目标对象发生接触或作用的可见画面",
    "functional_explanation": "口播或字幕具体说明产品功能如何对应需求、适用对象/场景、条件或使用步骤；仅报品名、泛泛好用或裸功效声称不算",
    "real_action": "可追踪的真实操作动作，而非只拿着、说着或静态展示",
    "application_change": "操作对象、覆盖区域或应用状态发生的可见变化",
    "continuity": "动作关键步骤在连续或足够相邻的证据中可追踪",
    "selling_point_in_action": "某个产品卖点在实际操作过程中被具体呈现",
    "usage_context": "使用对象、场景或限制条件可被直接定位",
    "multi_scene_logic": "多个使用场景之间有可观察的关系，而非随意拼接",
    "result_difference": "操作前后、控制对象或结果状态的可见差异",
    "result_presentation": "把使用后的目标对象或结果状态作为效果结果展示，即使差异本身不够明显",
    "effect_attribution": "结果与本产品操作之间存在可追踪的事实连接",
    "before_after_or_control": "前后状态或对照对象被实际呈现",
    "proof_salience": "效果证明区域足够清晰、占据可观察画面",
    "process_link": "效果结果与前面的具体操作之间的时间/对象连接",
    "close_detail": "关键结果区域以足够细节被展示",
    "reference_measure": "可识别的尺寸、数量、时间或其他对照参照",
    "source_identity": "独立于产品、品牌和当前达人的信任来源主体可被识别",
    "source_basis": "独立来源的出处、报告、认证、第三方用户原话或过程信息实际出现",
    "product_relevance": "该来源明确与当前产品或同一购买判断有关",
    "source_specificity": "来源不是泛泛的‘很多人说’，而有可定位对象或群体",
    "traceability": "来源可以回溯到画面、口播、字幕或具体出处",
    "independent_origin": "来源并非仅由产品/达人自述构成",
    "explicit_action": "直接面向观众的购买、点击、咨询或其他行动指令；明确购买条件的激励（如买就送/下单送）也属于促成购买的行动线索，普通赠品罗列不算",
    "purchase_path": "可执行的购物车、链接、店铺、私信或其他购买路径",
    "offer_or_value": "与行动相关的价格、优惠、权益或明确利益点；只有明确绑定购买条件时才可支持行动线索，普通赠品清单不算",
    "urgency": "明确的时效、限量、截止或现在行动理由",
    "ending_position": "行动指令出现在结尾或可识别的收束位置",
    "cta_recall": "行动指令能够召回前面已展示的产品价值或理由",
}

STAGE_DISQUALIFIER_DEFINITIONS: dict[str, str] = {
    "generic_greeting_only": "只有问候或泛泛开场，没有可识别触发点",
    "late_context_only": "关键上下文在开头窗口之外才出现，不能倒推为开头事实",
    "product_only_without_bridge": "只展示产品身份，没有与前述问题的承接",
    "mouth_only_or_static": "只有口播、拿持或静态展示，没有真实使用动作",
    "product_only_without_target_contact": "产品出现但没有与目标对象接触",
    "generic_product_statement_only": "只有品名、泛泛评价或裸功效声称，没有具体功能或用法解释，也没有实际使用展示",
    "staged_or_fake_action": "动作无法证明真实作用于目标对象或明显是摆拍替代",
    "claim_only_without_result": "只有功效/效果声称，没有可见结果",
    "unrelated_risk_or_warning": "画面显示的是无关风险、警示或其他对象变化",
    "result_only_without_process": "只有结果画面，没有可追踪的产品操作过程",
    "product_claim_only": "只有产品自述或卖点声称，没有独立来源",
    "offer_only": "只有价格、优惠或赠品，不构成信任来源",
    "unattributed_social_claim": "只有‘网上很火/很多人推荐’等不可定位的社会性说法",
    "generic_praise_only": "只有推荐、好用或喜欢等泛泛评价，没有行动指令",
    "benefit_only_without_action": "只回顾产品利益，没有面向观众的可执行行动",
}

# Every stage is tested against the same four boundary questions: own positive,
# non-own negative, previous-stage confusion, and next-stage confusion.  This
# is deliberately declarative so a new stage cannot be added without stating
# where its meaning starts and ends.
STAGE_BOUNDARY_TESTS: dict[str, dict[str, str]] = {
    "S1": {
        "own_positive": "开头已有可理解的痛点、变化、冲突、承诺或问题触发点。",
        "not_own_negative": "只有问候或泛泛开场，不能构成可识别触发点。",
        "previous_stage_confusion": "S1 没有前置功能阶段；不能把后续完整上下文倒灌进开头。",
        "next_stage_confusion": "产品身份或解决方案承接本身属于 S2，不应单独充当 S1 Hook。",
    },
    "S2": {
        "own_positive": "产品身份和它回应前面问题的关系在该窗口内可观察。",
        "not_own_negative": "只展示产品身份，没有问题到产品的承接。",
        "previous_stage_confusion": "S1 只证明吸引注意，不足以证明产品已经被自然引出。",
        "next_stage_confusion": "真实操作动作和使用对象接触属于 S3，不因产品出现就算 S2 完成。",
    },
    "S3": {
        "own_positive": "可追踪的真实使用，或有证据支持的具体功能/用法解释，均可参与 S3。",
        "not_own_negative": "只有拿持、品名、泛泛好用或裸功效声称，没有具体解释或真实使用。",
        "previous_stage_confusion": "只把产品引出来属于 S2；具体解释用途、适用情境或用法才进入 S3。",
        "next_stage_confusion": "功能解释不等于效果证明；口播不得填成已发生的使用动作或 S4 结果。",
    },
    "S4": {
        "own_positive": "可见结果差异和本品操作之间存在可追踪的对象与时间连接。",
        "not_own_negative": "只有功效声称，没有可见结果差异。",
        "previous_stage_confusion": "使用动作本身属于 S3，不能把动作完成当成效果已证明。",
        "next_stage_confusion": "认证、评论或来源可信度属于 S5，不是效果本身。",
    },
    "S5": {
        "own_positive": "独立来源主体、依据和产品相关性均有可定位事实。",
        "not_own_negative": "只有产品、品牌或当前达人自述，或泛泛‘网上很火/很多人推荐’，没有独立来源。",
        "previous_stage_confusion": "效果是否可见属于 S4，不因结果画面存在就构成信任来源。",
        "next_stage_confusion": "价格、优惠或行动指令属于 S6，不构成来源可信度。",
    },
    "S6": {
        "own_positive": "至少有明确面向观众的行动指令（含购买条件激励）或可执行购买路径。",
        "not_own_negative": "只有推荐、好用或产品价值回顾，没有可执行行动。",
        "previous_stage_confusion": "信任来源属于 S5，不能替代购买行动。",
        "next_stage_confusion": "S6 没有后续功能阶段；不得把泛泛收尾当成 CTA。",
    },
}


@dataclass(frozen=True)
class StageEvidenceContract:
    code: str
    label: str
    required_signals: tuple[str, ...]
    optional_signals: tuple[str, ...]
    channel_policy: str
    non_substitutable_channels: tuple[str, ...]
    disqualifiers: tuple[str, ...]
    scan_instruction: str
    required_signal_mode: str = "all"
    participation_signals: tuple[str, ...] = ()
    partial_compatible_disqualifiers: tuple[str, ...] = ()

    @property
    def allowed_signals(self) -> tuple[str, ...]:
        return self.required_signals + self.optional_signals

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "stage": self.code,
            "label": self.label,
            "required_signals": list(self.required_signals),
            "required_signal_mode": self.required_signal_mode,
            "participation_signals": list(self.participation_signals or self.required_signals),
            "optional_signals": list(self.optional_signals),
            "signal_definitions": {
                signal: STAGE_SIGNAL_DEFINITIONS.get(signal, "只记录该信号的直接观察，不做强弱评价。")
                for signal in self.allowed_signals
            },
            "channel_policy": self.channel_policy,
            "non_substitutable_channels": list(self.non_substitutable_channels),
            "disqualifiers": list(self.disqualifiers),
            "partial_compatible_disqualifiers": list(self.partial_compatible_disqualifiers),
            "disqualifier_definitions": {
                disqualifier: STAGE_DISQUALIFIER_DEFINITIONS.get(disqualifier, "只记录直接观察到的排除条件。")
                for disqualifier in self.disqualifiers
            },
            "scan_instruction": self.scan_instruction,
            "boundary_tests": copy.deepcopy(STAGE_BOUNDARY_TESTS.get(self.code, {})),
        }


STAGE_EVIDENCE_CONTRACTS: tuple[StageEvidenceContract, ...] = (
    StageEvidenceContract(
        "S1",
        "钩子",
        ("stop_trigger", "cold_audience_relevance"),
        ("product_or_problem_anchor", "visual_salience", "promise_or_contrast"),
        "visual_or_voiceover",
        (),
        ("generic_greeting_only", "late_context_only"),
        "检查开头是否给陌生观众一个可理解、值得继续看的触发点，不用完整看完视频倒推。",
        participation_signals=("stop_trigger", "cold_audience_relevance"),
    ),
    StageEvidenceContract(
        "S2",
        "产品引出",
        ("product_identity", "problem_to_product_bridge"),
        ("role_or_reason_clarity", "benefit_link", "natural_handoff"),
        "visual_or_voiceover",
        (),
        ("product_only_without_bridge",),
        "检查产品身份和它为什么能回应前面问题之间是否有事实上的承接。",
        participation_signals=("product_identity", "problem_to_product_bridge"),
        partial_compatible_disqualifiers=("product_only_without_bridge",),
    ),
    StageEvidenceContract(
        "S3",
        "功能与使用表达",
        ("target_contact", "real_action", "application_change", "functional_explanation"),
        ("continuity", "selling_point_in_action", "usage_context", "multi_scene_logic"),
        "visual_or_voiceover",
        (),
        ("generic_product_statement_only", "staged_or_fake_action"),
        "真实使用的三个信号须同时满足，或有具体 functional_explanation；讲解不冒充实际动作。比较解释的具体性、相关性和清晰度，不按字数、形式或卖点数量自动定档。",
        required_signal_mode="usage_or_explanation",
        participation_signals=("target_contact", "real_action", "functional_explanation"),
    ),
    StageEvidenceContract(
        "S4",
        "效果呈现",
        ("result_difference", "effect_attribution"),
        (
            "result_presentation",
            "before_after_or_control",
            "proof_salience",
            "process_link",
            "close_detail",
            "reference_measure",
        ),
        "visual_required",
        ("visual",),
        ("claim_only_without_result", "unrelated_risk_or_warning", "result_only_without_process"),
        "检查结果或差异是否真的可见、是否与本品动作有可追踪关系；风险提示或泛泛卖点不能替代效果证据。",
        participation_signals=("result_presentation", "result_difference"),
        partial_compatible_disqualifiers=("result_only_without_process",),
    ),
    StageEvidenceContract(
        "S5",
        "信任放大",
        ("source_identity", "source_basis", "product_relevance", "independent_origin"),
        ("source_specificity", "traceability"),
        "visual_or_voiceover",
        (),
        ("product_claim_only", "offer_only", "unattributed_social_claim"),
        "检查是否有可识别、与产品相关且独立于品牌和当前达人的信任来源；达人自己的使用经历、演示、旧工具对比、价格和优惠都不能单独构成独立信任来源。",
        participation_signals=("source_identity", "source_basis", "product_relevance", "independent_origin"),
    ),
    StageEvidenceContract(
        "S6",
        "促单",
        ("explicit_action", "purchase_path"),
        ("offer_or_value", "urgency", "ending_position", "cta_recall"),
        "visual_or_voiceover",
        (),
        ("generic_praise_only", "benefit_only_without_action"),
        "检查是否至少存在面向观众的行动指令（含明确购买条件激励）或可执行购买路径；普通赠品清单、只有推荐或产品价值回顾不算 CTA。",
        "any",
        ("explicit_action", "purchase_path"),
    ),
)

_CONTRACT_BY_STAGE = {item.code: item for item in STAGE_EVIDENCE_CONTRACTS}


def stage_codes() -> tuple[str, ...]:
    return tuple(item.code for item in STAGE_EVIDENCE_CONTRACTS)


def stage_evidence_contract(stage: Any) -> StageEvidenceContract | None:
    code = str(stage or "").strip().upper()[:2]
    return _CONTRACT_BY_STAGE.get(code)


def required_stage_signals_satisfied(
    contract: StageEvidenceContract,
    observed_signals: set[str] | list[str] | tuple[str, ...],
) -> bool:
    """Apply the canonical all-of/any-of requirement for one stage."""
    observed = set(observed_signals)
    required = set(contract.required_signals)
    if contract.required_signal_mode == "usage_or_explanation":
        return "functional_explanation" in observed or {"target_contact", "real_action", "application_change"}.issubset(observed)
    if contract.required_signal_mode == "any":
        return bool(required.intersection(observed))
    return required.issubset(observed)


def stage_evidence_contract_prompt(stages: list[Any] | tuple[Any, ...] | set[Any] | None = None) -> str:
    """Return the prompt-facing contract for all or only selected stages."""
    import json

    selected = {
        code
        for code in (
            normalize_stage_code(value)
            for value in (stages if stages is not None else stage_codes())
        )
        if code is not None
    }
    return json.dumps(
        [item.as_prompt_dict() for item in STAGE_EVIDENCE_CONTRACTS if item.code in selected],
        ensure_ascii=False,
        indent=2,
    )


def _clean_tokens(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        token = str(item or "").strip()
        if token and token not in output:
            output.append(token)
    return output


def normalize_stage_code(value: Any) -> str | None:
    code = str(value or "").strip().upper()[:2]
    return code if code in _CONTRACT_BY_STAGE else None
