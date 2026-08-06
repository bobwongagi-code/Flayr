"""Canonical Stage1 evidence contracts shared by extraction and evaluation.

Stage1 records observations first and only then qualifies them for a stage.  The
registry in this module is deliberately declarative: prompt text, normalization,
coverage gates, and offline audits all consume the same stage vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


STAGE_EVIDENCE_CONTRACT_VERSION = 1
STAGE_EVIDENCE_STATES = ("present", "absent", "unknown", "conflict")
STAGE_EVIDENCE_COVERAGE_STATES = ("complete", "partial", "unknown")
STAGE_EVIDENCE_STRENGTHS = ("direct", "explicit", "inferred", "absent")

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
    "real_action": "可追踪的真实操作动作，而非只拿着、说着或静态展示",
    "application_change": "操作对象、覆盖区域或应用状态发生的可见变化",
    "continuity": "动作关键步骤在连续或足够相邻的证据中可追踪",
    "selling_point_in_action": "某个产品卖点在实际操作过程中被具体呈现",
    "usage_context": "使用对象、场景或限制条件可被直接定位",
    "multi_scene_logic": "多个使用场景之间有可观察的关系，而非随意拼接",
    "result_difference": "操作前后、控制对象或结果状态的可见差异",
    "effect_attribution": "结果与本产品操作之间存在可追踪的事实连接",
    "before_after_or_control": "前后状态或对照对象被实际呈现",
    "proof_salience": "效果证明区域足够清晰、占据可观察画面",
    "process_link": "效果结果与前面的具体操作之间的时间/对象连接",
    "close_detail": "关键结果区域以足够细节被展示",
    "reference_measure": "可识别的尺寸、数量、时间或其他对照参照",
    "source_identity": "信任来源的主体可被识别",
    "source_basis": "来源依据、出处、报告、认证、用户原话或过程信息实际出现",
    "product_relevance": "该来源明确与当前产品或同一购买判断有关",
    "source_specificity": "来源不是泛泛的‘很多人说’，而有可定位对象或群体",
    "traceability": "来源可以回溯到画面、口播、字幕或具体出处",
    "independent_origin": "来源并非仅由产品/达人自述构成",
    "explicit_action": "直接面向观众的购买、点击、咨询或其他行动指令",
    "purchase_path": "可执行的购物车、链接、店铺、私信或其他购买路径",
    "offer_or_value": "与行动相关的价格、优惠、权益或明确利益点",
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

    @property
    def allowed_signals(self) -> tuple[str, ...]:
        return self.required_signals + self.optional_signals

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "stage": self.code,
            "label": self.label,
            "required_signals": list(self.required_signals),
            "optional_signals": list(self.optional_signals),
            "signal_definitions": {
                signal: STAGE_SIGNAL_DEFINITIONS.get(signal, "只记录该信号的直接观察，不做强弱评价。")
                for signal in self.allowed_signals
            },
            "channel_policy": self.channel_policy,
            "non_substitutable_channels": list(self.non_substitutable_channels),
            "disqualifiers": list(self.disqualifiers),
            "disqualifier_definitions": {
                disqualifier: STAGE_DISQUALIFIER_DEFINITIONS.get(disqualifier, "只记录直接观察到的排除条件。")
                for disqualifier in self.disqualifiers
            },
            "scan_instruction": self.scan_instruction,
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
    ),
    StageEvidenceContract(
        "S3",
        "使用过程",
        ("target_contact", "real_action", "application_change"),
        ("continuity", "selling_point_in_action", "usage_context", "multi_scene_logic"),
        "visual_required",
        ("visual",),
        ("mouth_only_or_static", "product_only_without_target_contact", "staged_or_fake_action"),
        "检查产品是否真实作用于目标对象、动作是否发生、应用前后或状态变化是否可追踪；口播不能替代视觉使用证明。",
    ),
    StageEvidenceContract(
        "S4",
        "效果呈现",
        ("result_difference", "effect_attribution"),
        ("before_after_or_control", "proof_salience", "process_link", "close_detail", "reference_measure"),
        "visual_required",
        ("visual",),
        ("claim_only_without_result", "unrelated_risk_or_warning", "result_only_without_process"),
        "检查结果或差异是否真的可见、是否与本品动作有可追踪关系；风险提示或泛泛卖点不能替代效果证据。",
    ),
    StageEvidenceContract(
        "S5",
        "信任放大",
        ("source_identity", "source_basis", "product_relevance"),
        ("source_specificity", "traceability", "independent_origin"),
        "visual_or_voiceover",
        (),
        ("product_claim_only", "offer_only", "unattributed_social_claim"),
        "检查是否有可识别、与产品相关的信任来源或来源说明；产品自述、价格和优惠单独不算独立信任来源。",
    ),
    StageEvidenceContract(
        "S6",
        "促单",
        ("explicit_action", "purchase_path"),
        ("offer_or_value", "urgency", "ending_position", "cta_recall"),
        "visual_or_voiceover",
        (),
        ("generic_praise_only", "benefit_only_without_action"),
        "检查是否有面向观众的明确行动和可执行购买路径；只有推荐或产品价值回顾不算完整 CTA。",
    ),
)

_CONTRACT_BY_STAGE = {item.code: item for item in STAGE_EVIDENCE_CONTRACTS}


def stage_codes() -> tuple[str, ...]:
    return tuple(item.code for item in STAGE_EVIDENCE_CONTRACTS)


def stage_evidence_contract(stage: Any) -> StageEvidenceContract | None:
    code = str(stage or "").strip().upper()[:2]
    return _CONTRACT_BY_STAGE.get(code)


def stage_evidence_contract_prompt() -> str:
    """Return the only prompt-facing copy of the six stage contracts."""
    import json

    return json.dumps(
        [item.as_prompt_dict() for item in STAGE_EVIDENCE_CONTRACTS],
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


def normalize_stage_evidence_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    return state if state in STAGE_EVIDENCE_STATES else "unknown"


def normalize_stage_evidence_coverage(value: Any) -> str:
    coverage = str(value or "").strip().lower()
    return coverage if coverage in STAGE_EVIDENCE_COVERAGE_STATES else "unknown"


def normalize_stage_evidence_checks(value: Any, valid_ids: set[str]) -> list[dict[str, Any]]:
    """Normalize one check per stage; omitted checks remain unknown, never absent."""
    raw_items: list[Any]
    if isinstance(value, dict):
        raw_items = []
        for key, item in value.items():
            if isinstance(item, dict):
                raw_items.append({"stage": key, **item})
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []

    by_stage: dict[str, dict[str, Any]] = {}
    duplicate_stages: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        stage = normalize_stage_code(raw.get("stage") or raw.get("stage_code"))
        contract = stage_evidence_contract(stage)
        if contract is None:
            continue
        if stage in by_stage:
            duplicate_stages.add(stage)
        evidence_ids = []
        invalid_evidence_ids = []
        for evidence_id in raw.get("evidence_ids") or []:
            token = str(evidence_id or "").strip()
            if token in valid_ids and token not in evidence_ids:
                evidence_ids.append(token)
            elif token and token not in invalid_evidence_ids:
                invalid_evidence_ids.append(token)
        raw_observed = _clean_tokens(raw.get("observed_signals"))
        raw_missing = _clean_tokens(raw.get("missing_signals"))
        observed = [token for token in raw_observed if token in contract.allowed_signals]
        missing = [token for token in raw_missing if token in contract.allowed_signals]
        invalid_observed_signals = [token for token in raw_observed if token not in contract.allowed_signals]
        invalid_missing_signals = [token for token in raw_missing if token not in contract.allowed_signals]
        observed_disqualifiers = [
            token
            for token in _clean_tokens(raw.get("observed_disqualifiers") or raw.get("disqualifiers"))
            if token in contract.disqualifiers
        ]
        invalid_observed_disqualifiers = [
            token
            for token in _clean_tokens(raw.get("observed_disqualifiers") or raw.get("disqualifiers"))
            if token not in contract.disqualifiers
        ]
        strength = str(raw.get("evidence_strength") or "").strip().lower()
        if strength not in STAGE_EVIDENCE_STRENGTHS:
            strength = None
        raw_coverage = str(raw.get("coverage") or "").strip().lower()
        coverage = raw_coverage if raw_coverage in STAGE_EVIDENCE_COVERAGE_STATES else "unknown"
        status = normalize_stage_evidence_state(raw.get("status") or raw.get("state"))
        by_stage[stage] = {
            "stage": stage,
            "status": status,
            "coverage": coverage,
            "evidence_ids": evidence_ids,
            "invalid_evidence_ids": invalid_evidence_ids,
            "observed_signals": observed,
            "missing_signals": missing,
            "invalid_observed_signals": invalid_observed_signals,
            "invalid_missing_signals": invalid_missing_signals,
            "observed_disqualifiers": observed_disqualifiers,
            "invalid_observed_disqualifiers": invalid_observed_disqualifiers,
            "evidence_strength": strength,
            "reason": str(raw.get("reason") or "").strip(),
        }

    normalized: list[dict[str, Any]] = []
    for contract in STAGE_EVIDENCE_CONTRACTS:
        item = by_stage.get(
            contract.code,
            {
                "stage": contract.code,
                "status": "unknown",
                "coverage": "unknown",
                "evidence_ids": [],
                "invalid_evidence_ids": [],
                "observed_signals": [],
                "missing_signals": [],
                "invalid_observed_signals": [],
                "invalid_missing_signals": [],
                "observed_disqualifiers": [],
                "invalid_observed_disqualifiers": [],
                "evidence_strength": None,
                "reason": "未提供该阶段的独立证据资格判断。",
            },
        )
        if contract.code in duplicate_stages:
            item = {
                **item,
                "status": "conflict",
                "coverage": "unknown",
                "evidence_ids": [],
                "reason": "输入中同一阶段出现多个资格判断，需定向复核。",
            }
        normalized.append(item)
    return normalized


def stage_evidence_check_map(side: Any) -> dict[str, dict[str, Any]]:
    checks = side.get("stage_evidence_checks") if isinstance(side, dict) else []
    return {
        str(item.get("stage")): item
        for item in checks or []
        if isinstance(item, dict) and str(item.get("stage") or "") in _CONTRACT_BY_STAGE
    }


def _evidence_units_by_id(side: Any) -> dict[str, dict[str, Any]]:
    units = side.get("evidence_units") if isinstance(side, dict) else []
    return {
        str(item.get("id")): item
        for item in units or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _duplicate_evidence_ids(side: Any) -> list[str]:
    units = side.get("evidence_units") if isinstance(side, dict) else []
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in units or []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("id") or "").strip()
        if evidence_id in seen and evidence_id not in duplicates:
            duplicates.append(evidence_id)
        seen.add(evidence_id)
    return duplicates


def _unit_has_channel(unit: dict[str, Any], channel: str) -> bool:
    if channel == "visual":
        return bool(str(unit.get("visual_fact") or "").strip())
    if channel == "voiceover":
        return bool(str(unit.get("voiceover") or unit.get("voiceover_zh") or "").strip())
    if channel == "subtitle":
        return bool(str(unit.get("subtitle_fact") or "").strip())
    if channel == "audio":
        value = str(unit.get("audio_fact") or "").strip().lower()
        return bool(value and value not in {"无", "none", "unknown", "未评估"})
    return False


def _unit_strengths(units_by_id: dict[str, dict[str, Any]], evidence_ids: list[str]) -> list[str | None]:
    return [
        str(units_by_id[evidence_id].get("evidence_strength") or "").strip().lower() or None
        if evidence_id in units_by_id
        else None
        for evidence_id in evidence_ids
    ]


def _stage_check_issues(
    contract: StageEvidenceContract,
    check: Any,
    units_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Validate one stage projection against the locked Stage1 observations.

    The check is a qualification projection, not a second evidence source.  In
    particular, its ``evidence_strength`` value is diagnostic only; eligibility
    is derived from the referenced evidence units below.
    """
    if not isinstance(check, dict):
        return [f"{contract.code}:stage_check_missing"]

    status = check.get("status")
    coverage = check.get("coverage")
    evidence_ids = [str(value) for value in check.get("evidence_ids") or [] if str(value).strip()]
    issues: list[str] = []
    for field in (
        "invalid_evidence_ids",
        "invalid_observed_signals",
        "invalid_missing_signals",
        "invalid_observed_disqualifiers",
    ):
        invalid = [str(value) for value in check.get(field) or [] if str(value).strip()]
        if invalid:
            issues.append(f"{contract.code}:{field}:{','.join(invalid)}")
    missing_ids = [value for value in evidence_ids if value not in units_by_id]
    if missing_ids:
        issues.append(f"{contract.code}:unknown_evidence_id:{','.join(missing_ids)}")

    observed = set(check.get("observed_signals") or [])
    missing = set(check.get("missing_signals") or [])
    required = set(contract.required_signals)
    observed_disqualifiers = set(check.get("observed_disqualifiers") or [])
    contradictory_signals = sorted(observed & missing)
    if contradictory_signals:
        issues.append(f"{contract.code}:signal_both_observed_and_missing:{','.join(contradictory_signals)}")

    if status == "present":
        if coverage != "complete":
            issues.append(f"{contract.code}:present_without_complete_coverage")
        if not evidence_ids:
            issues.append(f"{contract.code}:present_without_evidence")
        missing_required = sorted(required - observed)
        if missing_required:
            issues.append(f"{contract.code}:present_missing_required_signals:{','.join(missing_required)}")
        if observed_disqualifiers:
            issues.append(
                f"{contract.code}:present_with_disqualifiers:{','.join(sorted(observed_disqualifiers))}"
            )
        strengths = _unit_strengths(units_by_id, evidence_ids)
        if any(strength is None for strength in strengths):
            issues.append(f"{contract.code}:present_without_evidence_strength")
        elif any(strength not in {"direct", "explicit"} for strength in strengths):
            issues.append(f"{contract.code}:present_without_explicit_strength")
        referenced_units = [units_by_id[evidence_id] for evidence_id in evidence_ids if evidence_id in units_by_id]
        if contract.channel_policy == "visual_required":
            if not any(_unit_has_channel(unit, "visual") for unit in referenced_units):
                issues.append(f"{contract.code}:required_visual_channel_missing")
        elif contract.channel_policy == "visual_or_voiceover":
            if not any(
                _unit_has_channel(unit, "visual")
                or _unit_has_channel(unit, "subtitle")
                or _unit_has_channel(unit, "voiceover")
                for unit in referenced_units
            ):
                issues.append(f"{contract.code}:required_observation_channel_missing")
    elif status == "absent":
        if coverage != "complete" or evidence_ids or not required.issubset(missing):
            issues.append(f"{contract.code}:absence_without_complete_coverage")
    elif status in {"unknown", "conflict"}:
        if evidence_ids:
            issues.append(f"{contract.code}:{status}_with_evidence")
    else:
        issues.append(f"{contract.code}:invalid_status")

    return issues


def qualified_stage_evidence_ids(side: Any, stage: Any, *, allow_inferred: bool = False) -> set[str]:
    """Return IDs qualified by the locked unit facts, never inferred from functions."""
    stage_code = normalize_stage_code(stage) or ""
    check = stage_evidence_check_map(side).get(stage_code)
    if not isinstance(check, dict) or check.get("status") != "present":
        return set()
    contract = stage_evidence_contract(stage_code)
    if contract is None:
        return set()
    units_by_id = _evidence_units_by_id(side)
    if _stage_check_issues(contract, check, units_by_id):
        return set()
    allowed_strengths = {"direct", "explicit"}
    if allow_inferred:
        allowed_strengths.add("inferred")
    return {
        evidence_id
        for evidence_id in (str(value) for value in check.get("evidence_ids") or [])
        if evidence_id in units_by_id
        and str(units_by_id[evidence_id].get("evidence_strength") or "").strip().lower() in allowed_strengths
    }


def stage_evidence_recovery_targets(side: Any) -> list[str]:
    """Stages that need one bounded pre-lock re-observation pass."""
    targets: list[str] = []
    units_by_id = _evidence_units_by_id(side)
    for contract in STAGE_EVIDENCE_CONTRACTS:
        check = stage_evidence_check_map(side).get(contract.code)
        if not isinstance(check, dict):
            targets.append(contract.code)
            continue
        if check.get("status") in {"unknown", "conflict"} or _stage_check_issues(contract, check, units_by_id):
            targets.append(contract.code)
    return targets


def stage_evidence_contract_issues(side: Any, *, require_version: bool = True) -> list[str]:
    """Return structural issues without converting unknown into absence."""
    issues: list[str] = []
    if not isinstance(side, dict):
        return ["side_not_object"]
    if require_version and side.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        issues.append("missing_or_old_contract_version")
    checks = stage_evidence_check_map(side)
    if set(checks) != set(stage_codes()):
        issues.append("stage_checks_incomplete")
    units = _evidence_units_by_id(side)
    duplicate_ids = _duplicate_evidence_ids(side)
    if duplicate_ids:
        issues.append("duplicate_evidence_ids:" + ",".join(duplicate_ids))
    for stage in stage_codes():
        issues.extend(_stage_check_issues(_CONTRACT_BY_STAGE[stage], checks.get(stage), units))
    return issues


def project_functions_from_stage_checks(side: Any) -> dict[str, set[str]]:
    """Compatibility view for old consumers; stage checks remain authoritative."""
    output: dict[str, set[str]] = {}
    for stage, check in stage_evidence_check_map(side).items():
        if check.get("status") == "present":
            for evidence_id in check.get("evidence_ids") or []:
                output.setdefault(str(evidence_id), set()).add(f"{stage}_evidence")
    return output
