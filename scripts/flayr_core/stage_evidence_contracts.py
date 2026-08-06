"""Canonical Stage1 evidence contracts shared by extraction and evaluation.

Stage1 records observations first and only then qualifies them for a stage.  The
registry in this module is deliberately declarative: prompt text, normalization,
coverage gates, and offline audits all consume the same stage vocabulary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .artifacts import (
    get_analysis_frame_entries,
    parse_time_range_seconds,
    resolve_artifact_path,
)
from .transcript import current_transcript_segments_path, current_transcript_words_path


STAGE_EVIDENCE_CONTRACT_VERSION = 3
STAGE_EVIDENCE_SNAPSHOT_VERSION = 1
STAGE_EVIDENCE_GATE_VERSION = 1
STAGE1_ACQUISITION_VERSION = 3
STAGE1_COVERAGE_AUDIT_VERSION = 1
STAGE_EVIDENCE_STATES = ("present", "absent", "unknown", "conflict")
STAGE_EVIDENCE_COVERAGE_STATES = ("complete", "partial", "unknown")
STAGE_EVIDENCE_STRENGTHS = ("direct", "explicit", "inferred", "absent")
STAGE1_ACQUISITION_STATUSES = ("complete", "partial", "failed", "unknown")
STAGE1_ACQUISITION_CHANNEL_STATUSES = ("ready", "degraded", "failed", "unknown")
STAGE1_ACQUISITION_COVERAGE_STATES = ("full", "sampled", "partial", "none", "unknown")
STAGE1_STAGE_COVERAGE_STATUSES = ("observed", "complete", "partial", "unknown")
STAGE1_COVERAGE_AUDIT_RUN_STATUSES = ("completed", "partial", "failed", "unknown")
STAGE1_COVERAGE_AUDIT_STATUSES = ("found", "clear", "conflict", "unknown")
STAGE1_COVERAGE_AUDIT_INDEPENDENCE = "separate_request_same_model"
STAGE_EVIDENCE_BINDING_STATUSES = ("supported", "missing", "unknown", "conflict")
STAGE_EVIDENCE_LINK_RELATIONS = ("primary", "supporting", "contradicting")
STAGE_EVIDENCE_LINK_CONFIDENCES = ("high", "medium", "low", "unknown")
STAGE_EVIDENCE_LINK_SOURCES = ("model", "compatibility")
VISUAL_INPUT_TIMESTAMP_TOLERANCE_SECONDS = 0.05
STAGE_EVIDENCE_GATE_STATUSES = (
    "grounded",
    "blocked",
    "not_applicable",
    "not_comparable",
    "legacy",
)

# These are the normalized Stage1 observations and qualification projections.
# ``stage1_recovery`` and the evidence-set metadata are pipeline bookkeeping;
# they are intentionally excluded because the pipeline may update that
# bookkeeping before the final lock is written.
STAGE1_IMMUTABLE_FIELDS = (
    "product_identity",
    "content_summary",
    "communication_strategy",
    "temporal_evidence_mode",
    "selling_point_observations",
    "variant_decision_rule",
    "attention_scan_audit",
    "attention_competitors",
    "gate_observation_status",
    "evidence_units",
    "evidence_checklist",
    "structure_event_checks",
    "stage_evidence_contract_version",
    "stage_evidence_checks",
    "evidence_budget_exceeded",
    "stage1_acquisition",
    "stage1_coverage_audit",
)

# Stage1 records observations. These fields belong to Judgment, Resolution, or
# Report and must fail closed when they appear in an active fact response.
STAGE1_FORBIDDEN_FIELDS = frozenset(
    {
        "severity",
        "model_severity",
        "gap",
        "gap_summary",
        "comparison",
        "comparison_status",
        "comparison_contract",
        "comparison_eligibility",
        "commercial_priority",
        "commercial_priorities",
        "commercial_priority_summary",
        "recommendations",
        "improvements",
        "suggestions",
        "purchase_impact",
        "business_impact",
        "final_conclusion",
        "stage_analysis",
        "stage_evidence_links",
        "linking_reason",
    }
)

# These fields are provenance owned by the local pipeline. A model-shaped
# Stage1 response must not author, replay, or silently replace them. The
# finalizer may restore a trusted copy after the Stage2 response is isolated
# from the locked Stage1 object.
STAGE1_PIPELINE_OWNED_FIELDS = frozenset(
    {
        "stage1_acquisition",
        "stage1_coverage_audit",
        "stage1_recovery",
        "evidence_set_version",
        "evidence_set_sha256",
        "evidence_set_status",
        "evidence_set_source",
    }
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
        "own_positive": "目标接触、真实动作和应用变化均能由可追踪画面观察。",
        "not_own_negative": "只有拿持、口播或静态产品展示，没有真实使用动作。",
        "previous_stage_confusion": "产品引出和解决方案说明本身不构成真实使用。",
        "next_stage_confusion": "结果差异或功效声称不能替代使用过程证据。",
    },
    "S4": {
        "own_positive": "可见结果差异和本品操作之间存在可追踪的对象与时间连接。",
        "not_own_negative": "只有功效声称，没有可见结果差异。",
        "previous_stage_confusion": "使用动作本身属于 S3，不能把动作完成当成效果已证明。",
        "next_stage_confusion": "认证、评论或来源可信度属于 S5，不是效果本身。",
    },
    "S5": {
        "own_positive": "来源主体、依据和产品相关性均有可定位事实。",
        "not_own_negative": "只有产品自述或泛泛‘网上很火/很多人推荐’，没有独立来源。",
        "previous_stage_confusion": "效果是否可见属于 S4，不因结果画面存在就构成信任来源。",
        "next_stage_confusion": "价格、优惠或行动指令属于 S6，不构成来源可信度。",
    },
    "S6": {
        "own_positive": "有明确面向观众的行动指令和可执行路径。",
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


def stage1_forbidden_field_issues(value: Any) -> list[str]:
    """Return paths where a Stage1 response attempts to emit downstream judgment.

    The check is intentionally recursive: a model must not evade ownership by
    nesting ``severity`` or ``comparison`` inside an otherwise valid object.
    Text values are never inspected, so ordinary observations containing those
    words remain valid.
    """
    issues: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text in STAGE1_FORBIDDEN_FIELDS:
                    issues.append(child_path)
                walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(value, "")
    return issues


def stage1_pipeline_owned_field_issues(value: Any) -> list[str]:
    """Return paths where an untrusted response authors pipeline metadata."""
    issues: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if key_text in STAGE1_PIPELINE_OWNED_FIELDS:
                    issues.append(child_path)
                walk(child, child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(value, "")
    return issues


def stage_boundary_contract_issues() -> list[str]:
    """Validate that every registered stage declares all four boundary tests."""
    required = {"own_positive", "not_own_negative", "previous_stage_confusion", "next_stage_confusion"}
    issues: list[str] = []
    for code in stage_codes():
        declared = STAGE_BOUNDARY_TESTS.get(code)
        if not isinstance(declared, dict) or set(declared) != required:
            issues.append(f"{code}:boundary_tests_incomplete")
            continue
        if any(not str(value).strip() for value in declared.values()):
            issues.append(f"{code}:boundary_tests_empty")
    return issues


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _finite_nonnegative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _snap_to_known_visual_timestamp(value: Any, known_timestamps: list[float]) -> float | None:
    """Accept only a timestamp that maps to an actual frame in the request manifest."""
    timestamp = _finite_nonnegative(value)
    if timestamp is None or not known_timestamps:
        return None
    nearest = min(known_timestamps, key=lambda item: abs(item - timestamp))
    if abs(nearest - timestamp) > VISUAL_INPUT_TIMESTAMP_TOLERANCE_SECONDS:
        return None
    return nearest


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _acquisition_channel(
    status: Any,
    *,
    coverage: Any = "unknown",
    count: Any = 0,
    boundary_precision: Any = "unknown",
    reason: Any = "",
) -> dict[str, Any]:
    normalized_status = str(status or "unknown").strip().lower()
    if normalized_status not in STAGE1_ACQUISITION_CHANNEL_STATUSES:
        normalized_status = "unknown"
    normalized_coverage = str(coverage or "unknown").strip().lower()
    if normalized_coverage not in STAGE1_ACQUISITION_COVERAGE_STATES:
        normalized_coverage = "unknown"
    try:
        normalized_count = max(0, int(count or 0))
    except (TypeError, ValueError):
        normalized_count = 0
    return {
        "status": normalized_status,
        "coverage": normalized_coverage,
        "count": normalized_count,
        "boundary_precision": str(boundary_precision or "unknown").strip().lower(),
        "reason": str(reason or "").strip(),
    }


def normalize_stage1_acquisition(value: Any) -> dict[str, Any]:
    """Normalize code-owned acquisition provenance without accepting conclusions.

    This structure is written by the pipeline from preprocessing metadata.  It
    is intentionally a capability/coverage record, not a model assertion about
    what the video means.
    """
    if not isinstance(value, dict):
        return {}
    channels = value.get("channels") if isinstance(value.get("channels"), dict) else {}
    normalized_channels = {
        channel: _acquisition_channel(channels.get(channel, {}).get("status") if isinstance(channels.get(channel), dict) else None,
                                      coverage=channels.get(channel, {}).get("coverage") if isinstance(channels.get(channel), dict) else None,
                                      count=channels.get(channel, {}).get("count") if isinstance(channels.get(channel), dict) else 0,
                                      boundary_precision=channels.get(channel, {}).get("boundary_precision") if isinstance(channels.get(channel), dict) else None,
                                      reason=channels.get(channel, {}).get("reason") if isinstance(channels.get(channel), dict) else None)
        for channel in ("visual", "voiceover", "subtitle", "audio")
    }
    stage_coverage = value.get("stage_coverage") if isinstance(value.get("stage_coverage"), dict) else {}
    normalized_stage_coverage = {
        stage: {
            "count": _nonnegative_int(stage_coverage.get(stage, {}).get("count", 0))
            if isinstance(stage_coverage.get(stage), dict)
            else 0,
            "status": (
                str(stage_coverage.get(stage, {}).get("status") or "unknown").strip().lower()
                if isinstance(stage_coverage.get(stage), dict)
                else "unknown"
            )
            if (
                isinstance(stage_coverage.get(stage), dict)
                and str(stage_coverage.get(stage, {}).get("status") or "unknown").strip().lower()
                in STAGE1_STAGE_COVERAGE_STATUSES
            )
            else "unknown"
        }
        for stage in stage_codes()
    }
    visual_input_timestamps: list[float] = []
    for item in value.get("visual_input_timestamps") or []:
        timestamp = _finite_nonnegative(item)
        if timestamp is not None and timestamp not in visual_input_timestamps:
            visual_input_timestamps.append(timestamp)
    visual_input_timestamps.sort()
    raw_status = str(value.get("status") or "unknown").strip().lower()
    status = raw_status if raw_status in STAGE1_ACQUISITION_STATUSES else "unknown"
    return {
        "version": value.get("version") if value.get("version") == STAGE1_ACQUISITION_VERSION else None,
        "source": str(value.get("source") or "").strip().lower(),
        "status": status,
        "input_mode": str(value.get("input_mode") or "unknown").strip().lower(),
        "speech_mode": str(value.get("speech_mode") or "unknown").strip().lower(),
        "duration_seconds": _finite_nonnegative(value.get("duration_seconds")),
        "channels": normalized_channels,
        "visual_input_timestamps": visual_input_timestamps,
        "stage_coverage": normalized_stage_coverage,
        "errors": [
            str(item).strip()
            for item in (value.get("errors") if isinstance(value.get("errors"), list) else [])
            if str(item).strip()
        ],
    }


def normalize_stage1_coverage_audit(value: Any, valid_ids: set[str] | None = None) -> dict[str, Any]:
    """Normalize the independent semantic-coverage pass owned by the pipeline.

    The model may propose candidate IDs and slot observations, but the active
    result stores only the normalized, code-owned audit record.  This record is
    intentionally separate from ``stage_evidence_checks``: a primary pass can
    say ``absent`` while the audit pass finds a candidate, and that disagreement
    must remain visible instead of being silently overwritten.
    """
    if not isinstance(value, dict):
        return {}
    valid_ids = valid_ids if isinstance(valid_ids, set) else set()
    raw_stages = value.get("stages")
    if not isinstance(raw_stages, dict):
        raw_stages = value.get("stage_audits") if isinstance(value.get("stage_audits"), dict) else {}
    normalized_stages: dict[str, dict[str, Any]] = {}
    for contract in STAGE_EVIDENCE_CONTRACTS:
        raw = raw_stages.get(contract.code) if isinstance(raw_stages, dict) else None
        if not isinstance(raw, dict):
            normalized_stages[contract.code] = {
                "status": "unknown",
                "coverage": "unknown",
                "evidence_ids": [],
                "invalid_evidence_ids": [],
                "observed_signals": [],
                "missing_signals": [],
                "reason": "覆盖审计未返回该阶段。",
            }
            continue
        raw_status = str(raw.get("status") or raw.get("audit_status") or "unknown").strip().lower()
        status = raw_status if raw_status in STAGE1_COVERAGE_AUDIT_STATUSES else "unknown"
        raw_coverage = str(raw.get("coverage") or "unknown").strip().lower()
        coverage = raw_coverage if raw_coverage in STAGE_EVIDENCE_COVERAGE_STATES else "unknown"
        evidence_ids: list[str] = []
        invalid_ids: list[str] = []
        for item in raw.get("evidence_ids") or raw.get("candidate_evidence_ids") or []:
            token = str(item or "").strip()
            if not token:
                continue
            if token in valid_ids:
                if token not in evidence_ids:
                    evidence_ids.append(token)
            elif token not in invalid_ids:
                invalid_ids.append(token)
        observed = [
            token for token in _clean_tokens(raw.get("observed_signals"))
            if token in contract.allowed_signals
        ]
        missing = [
            token for token in _clean_tokens(raw.get("missing_signals"))
            if token in contract.allowed_signals
        ]
        signal_bindings, invalid_signal_bindings = _normalize_signal_bindings(
            raw.get("signal_bindings"),
            contract,
            valid_ids,
        )
        normalized_stages[contract.code] = {
            "status": status,
            "coverage": coverage,
            "evidence_ids": evidence_ids,
            "invalid_evidence_ids": invalid_ids,
            "observed_signals": observed,
            "missing_signals": missing,
            "signal_bindings": signal_bindings,
            "invalid_signal_bindings": invalid_signal_bindings,
            "reason": str(raw.get("reason") or "").strip(),
        }
    raw_run_status = str(value.get("status") or "unknown").strip().lower()
    run_status = raw_run_status if raw_run_status in STAGE1_COVERAGE_AUDIT_RUN_STATUSES else "unknown"
    return {
        "version": value.get("version") if value.get("version") == STAGE1_COVERAGE_AUDIT_VERSION else None,
        "source": str(value.get("source") or "").strip().lower(),
        "status": run_status,
        "independence": str(value.get("independence") or "unknown").strip().lower(),
        "stages": normalized_stages,
        "errors": [
            str(item).strip()
            for item in (value.get("errors") if isinstance(value.get("errors"), list) else [])
            if str(item).strip()
        ],
    }


def build_stage1_acquisition_manifest(
    analysis: Any,
    role: str,
    *,
    native_video: bool = False,
    visual_input_count: int = 0,
    visual_input_timestamps: list[Any] | None = None,
    audio_input_available: bool | None = None,
) -> dict[str, Any]:
    """Build code-owned input coverage from preprocessing artifacts.

    The function never infers a semantic fact.  It only records whether the
    input channel that could support a fact was actually available to the
    extractor.  Missing or ambiguous preprocessing therefore becomes an
    explicit downstream block instead of a model-generated ``absent``.
    """
    videos = analysis.get("videos") if isinstance(analysis, dict) else {}
    info = videos.get(role) if isinstance(videos, dict) and isinstance(videos.get(role), dict) else {}
    evidence = info.get("video_evidence") if isinstance(info.get("video_evidence"), dict) else {}
    duration = _finite_nonnegative(info.get("duration_seconds"))
    # Use the same canonical manifest as the actual payload builder, then
    # require every referenced frame to exist. A timestamp in a stale manifest
    # is not visual coverage and must not make a stage look observed.
    frames = get_analysis_frame_entries(info)
    usable_frames = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        if resolve_artifact_path(info, frame.get("path"), require_file=True) is None:
            continue
        usable_frames.append(frame)
    frames = usable_frames
    valid_timed_frames = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        timestamp = _finite_nonnegative(frame.get("timestamp_seconds"))
        if timestamp is not None and (duration is None or timestamp <= duration + 0.05):
            valid_timed_frames.append(timestamp)
    valid_timed_frames.sort()
    frame_errors = [
        str(item).strip()
        for item in [*(info.get("errors") or []), *(evidence.get("errors") or [])]
        if str(item).strip() and any(token in str(item).lower() for token in ("frame", "duration", "ffmpeg", "selection"))
    ]
    if visual_input_timestamps is None:
        # Compatibility callers that only provide a preprocessing manifest do
        # not know the exact request payload. The live pipeline passes the
        # selected timestamps explicitly below.
        request_timestamps = list(valid_timed_frames)
    else:
        # A caller must not be able to manufacture coverage by supplying an
        # arbitrary timestamp. Only timestamps that resolve to an actual,
        # readable canonical frame can qualify a sampled visual fact.
        request_timestamps = sorted(
            {
                timestamp
                for item in visual_input_timestamps
                if (timestamp := _snap_to_known_visual_timestamp(item, valid_timed_frames)) is not None
            }
        )
    visual_ready = bool(native_video or (duration is not None and request_timestamps))
    if native_video:
        visual_reason = "本次请求由代码确认包含原生视频。"
        visual_coverage = "full"
        visual_count = max(len(request_timestamps), int(visual_input_count or 0))
    elif visual_ready:
        # A finite, valid frame manifest proves that the extractor received
        # sampled visual observations. It does not prove that every moment of
        # the video was observed, so it must never authorize a negative claim
        # such as "no usage" or "no CTA". Only native video is full visual
        # coverage for this contract.
        visual_reason = "本次请求使用代码生成的 canonical analysis frame manifest；这是离散采样，不是全时段观察。"
        visual_coverage = "sampled"
        visual_count = len(request_timestamps)
    else:
        visual_reason = "缺少可验证的时长或带时间边界的视觉输入。"
        visual_coverage = "none"
        visual_count = len(valid_timed_frames)
    visual_channel = _acquisition_channel(
        "ready" if visual_ready else "failed" if frames or frame_errors else "unknown",
        coverage=visual_coverage,
        count=visual_count,
        boundary_precision=("continuous" if native_video else "frame" if request_timestamps else "unknown"),
        reason=visual_reason,
    )

    transcription_status = str(info.get("transcription_status") or "unknown").strip().lower()
    segment_ready = transcription_status == "completed" and current_transcript_segments_path(info) is not None
    words_ready = current_transcript_words_path(info) is not None
    windowed_transcript_ready = (
        resolve_artifact_path(
            info,
            evidence.get("transcript_windowed_path"),
            require_file=True,
        )
        is not None
    )
    # A words index without the derived consumption view is not enough to
    # prove what the model actually received.  Keep the channel available for
    # semantic audit, but downgrade its boundary precision until the
    # window-safe artifact exists.
    word_window_ready = words_ready and windowed_transcript_ready
    voiceover_channel = _acquisition_channel(
        "ready" if segment_ready else "failed" if transcription_status in {"failed", "placeholder"} else "unknown",
        coverage="full" if segment_ready else "none",
        count=1 if segment_ready else 0,
        boundary_precision="word" if word_window_ready else "segment" if segment_ready else "unknown",
        reason=(
            "在线 ASR 已完成并提供分段时间戳。"
            + (
                "已提供词级边界和窗口安全转写。"
                if word_window_ready
                else "只有分段边界或缺少窗口安全转写，精确窗口归属受限。"
            )
            if segment_ready
            else f"transcription_status={transcription_status}。"
        ),
    )

    subtitle_status = str(info.get("subtitle_track_status") or "unknown").strip().lower()
    subtitle_segments = evidence.get("subtitle_segment_count") or info.get("subtitle_segment_count") or 0
    subtitle_ready = subtitle_status in {"ready", "empty"}
    subtitle_channel = _acquisition_channel(
        "ready" if subtitle_ready else "failed" if subtitle_status in {"failed", "error", "unreadable"} else "unknown",
        coverage="full" if subtitle_ready else "unknown",
        count=subtitle_segments,
        boundary_precision="frame" if subtitle_ready else "unknown",
        reason=f"subtitle_track_status={subtitle_status}。",
    )

    audio_ready = resolve_artifact_path(info, info.get("audio_path"), require_file=True) is not None
    if audio_input_available is None:
        # Compatibility callers may not know the exact request payload.  The
        # production pipeline passes this explicitly; this fallback keeps
        # older offline manifest builders truthful to their local context.
        audio_input_available = audio_ready
    audio_observable = audio_ready and bool(audio_input_available)
    audio_channel = _acquisition_channel(
        "ready" if audio_observable else "unknown" if audio_ready else "unknown",
        coverage="full" if audio_observable else "none",
        count=1 if audio_observable else 0,
        boundary_precision="continuous" if audio_observable else "unknown",
        reason=(
            "本地音轨已产出且实际随 Stage1 请求提供。"
            if audio_observable
            else "本地音轨存在，但未确认随 Stage1 请求提供。"
            if audio_ready
            else "缺少本地音轨。"
        ),
    )

    speech_mode = info.get("speech_mode") if isinstance(info.get("speech_mode"), dict) else {}
    mode = str(speech_mode.get("mode") or "unknown").strip().lower()
    # If an audio track exists but transcription did not complete, the absence
    # of a spoken signal is not observable. Keep the spine unknown so a model
    # cannot turn an ASR failure into an explicit negative claim.
    if audio_ready and transcription_status not in {"completed"}:
        mode = "unknown"
    required_channels = ["visual"]
    if mode == "spoken":
        required_channels.append("voiceover")
    elif mode == "subtitle_driven":
        required_channels.append("subtitle")
    channels = {
        "visual": visual_channel,
        "voiceover": voiceover_channel,
        "subtitle": subtitle_channel,
        "audio": audio_channel,
    }
    missing_required = [channel for channel in required_channels if channels[channel]["status"] != "ready"]
    required_channels_full = all(channels[channel]["coverage"] == "full" for channel in required_channels)
    if not visual_ready:
        overall_status = "failed" if frames or frame_errors else "unknown"
    elif missing_required or not required_channels_full:
        overall_status = "partial"
    else:
        overall_status = "complete"

    # Stage boundaries are functional contracts, not fixed time slices. Before
    # Stage1 qualification there is no code-owned semantic boundary to count
    # against, so sampled inputs remain diagnostic-only here. Positive claims
    # are checked below against their own evidence time ranges and the exact
    # request timestamps; native video is known to cover the full timeline.
    if native_video and duration is not None:
        stage_coverage = {
            stage: {"count": 1, "status": "observed"}
            for stage in stage_codes()
        }
    else:
        stage_coverage = {
            stage: {"count": 0, "status": "unknown"}
            for stage in stage_codes()
        }

    return normalize_stage1_acquisition(
        {
            "version": STAGE1_ACQUISITION_VERSION,
            "source": "pipeline",
            "status": overall_status,
            "input_mode": "native_video" if native_video else "canonical_frames",
            "speech_mode": mode,
            "duration_seconds": duration,
            "channels": channels,
            "stage_coverage": stage_coverage,
            "visual_input_timestamps": request_timestamps,
            "errors": frame_errors + missing_required,
        }
    )


def stage_evidence_snapshot(side: Any) -> dict[str, Any]:
    """Build the immutable Stage1 evidence payload used for hashing.

    All normalized Stage1 observations and qualification projections are
    included. Stage2 links are intentionally excluded: links explain how
    judgment consumes evidence and may be revised without changing the
    underlying observation set.
    """
    if not isinstance(side, dict):
        return {
            "snapshot_version": STAGE_EVIDENCE_SNAPSHOT_VERSION,
            "contract_version": None,
            "fields": {field: None for field in STAGE1_IMMUTABLE_FIELDS},
        }
    return {
        "snapshot_version": STAGE_EVIDENCE_SNAPSHOT_VERSION,
        "fields": {
            field: copy.deepcopy(side.get(field))
            for field in STAGE1_IMMUTABLE_FIELDS
        },
    }


def stage_evidence_sha256(side: Any) -> str:
    """Return the stable digest for a Stage1 evidence set."""
    return hashlib.sha256(_canonical_json(stage_evidence_snapshot(side)).encode("utf-8")).hexdigest()


def freeze_stage_evidence(side: Any) -> dict[str, Any]:
    """Stamp a normalized Stage1 side with its code-owned immutable digest."""
    if not isinstance(side, dict):
        raise ValueError("Stage1 evidence side must be an object before it can be frozen.")
    side["evidence_set_version"] = STAGE_EVIDENCE_SNAPSHOT_VERSION
    side["evidence_set_sha256"] = stage_evidence_sha256(side)
    side["evidence_set_status"] = "frozen"
    side["evidence_set_source"] = "pipeline"
    return side


def stage_evidence_snapshot_issues(
    side: Any,
    *,
    expected_sha256: str | None = None,
    require_snapshot: bool = True,
) -> list[str]:
    """Return missing or changed immutable evidence metadata without mutation."""
    if not isinstance(side, dict):
        return ["side_not_object"]
    snapshot_values = (
        side.get("evidence_set_version"),
        str(side.get("evidence_set_sha256") or "").strip(),
        str(side.get("evidence_set_status") or "").strip().lower(),
    )
    if not require_snapshot and not any(value not in (None, "") for value in snapshot_values):
        return []
    issues: list[str] = []
    if side.get("evidence_set_version") != STAGE_EVIDENCE_SNAPSHOT_VERSION:
        issues.append("missing_or_old_evidence_set_version")
    if side.get("evidence_set_status") != "frozen":
        issues.append("evidence_set_not_frozen")
    stored = str(side.get("evidence_set_sha256") or "").strip()
    if not stored:
        issues.append("missing_evidence_set_sha256")
    actual = stage_evidence_sha256(side)
    if stored and stored != actual:
        issues.append("evidence_set_sha256_mismatch")
    if expected_sha256 and actual != expected_sha256:
        issues.append("evidence_set_changed_after_lock")
    return issues


def stage_evidence_immutability_issues(
    value: Any,
    expected_hashes: dict[str, str] | None = None,
    *,
    require_snapshot: bool = True,
) -> list[str]:
    """Check both roles in a full facts/result object."""
    if not isinstance(value, dict):
        return ["video_understanding_not_object"]
    sides = value.get("video_understanding") if isinstance(value.get("video_understanding"), dict) else value
    issues: list[str] = []
    for role in ("benchmark", "creator"):
        side = sides.get(role) if isinstance(sides, dict) and isinstance(sides.get(role), dict) else None
        if side is None:
            continue
        expected = (expected_hashes or {}).get(role)
        role_issues = stage_evidence_snapshot_issues(
            side,
            expected_sha256=expected,
            require_snapshot=require_snapshot,
        )
        issues.extend(f"{role}:{issue}" for issue in role_issues)
    return issues


def normalize_stage_evidence_links(value: Any, stage_analysis: Any) -> list[dict[str, Any]]:
    """Normalize the Stage2-to-Stage1 link layer.

    Older valid results only carried ``*_evidence_ids``.  They receive an
    explicit compatibility link with ``confidence=unknown`` rather than being
    silently treated as if the model supplied a reason. New prompts can emit
    the same structure with a model-provided linking reason.
    """
    links: list[dict[str, Any]] = []
    raw_items = value if isinstance(value, list) else []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        links.append(
            {
                "stage_id": normalize_stage_code(raw.get("stage_id") or raw.get("stage")) or str(raw.get("stage_id") or "").strip(),
                "role": str(raw.get("role") or "").strip().lower(),
                "evidence_id": str(raw.get("evidence_id") or raw.get("id") or "").strip(),
                "relation": str(raw.get("relation") or "").strip().lower(),
                "linking_reason": str(raw.get("linking_reason") or "").strip(),
                "confidence": str(raw.get("confidence") or "").strip().lower(),
                "source": str(raw.get("source") or "model").strip().lower(),
            }
        )
    if links:
        return links

    # Compatibility migration for old model outputs. This is deliberately
    # visible in ``source`` and does not claim that the model supplied a reason.
    for index, stage in enumerate(stage_analysis if isinstance(stage_analysis, list) else [], start=1):
        if not isinstance(stage, dict):
            continue
        stage_id = normalize_stage_code(stage.get("stage")) or f"S{index}"
        for role in ("creator", "benchmark"):
            references = [str(item).strip() for item in stage.get(f"{role}_evidence_ids") or [] if str(item).strip()]
            for ref_index, evidence_id in enumerate(dict.fromkeys(references)):
                links.append(
                    {
                        "stage_id": stage_id,
                        "role": role,
                        "evidence_id": evidence_id,
                        "relation": "primary" if ref_index == 0 else "supporting",
                        "linking_reason": (
                            f"由 {role}_evidence_ids 兼容迁移；旧结果未提供独立的 linking_reason。"
                        ),
                        "confidence": "unknown",
                        "source": "compatibility",
                    }
                )
    return links


def reconcile_stage_evidence_links(value: Any) -> None:
    """Keep mutable stage links aligned with the final stage reference lists.

    Resolution may remove an unsupported reference, but it may not create a
    new evidence unit. Existing link metadata is retained by tuple identity;
    any newly exposed reference receives an explicit compatibility link so the
    final artifact remains structurally auditable.
    """
    if not isinstance(value, dict):
        return
    stages = value.get("stage_analysis")
    if not isinstance(stages, list):
        return
    existing = {
        (
            str(item.get("stage_id") or "").strip().upper(),
            str(item.get("role") or "").strip().lower(),
            str(item.get("evidence_id") or "").strip(),
        ): item
        for item in value.get("stage_evidence_links") or []
        if isinstance(item, dict)
    }
    value["stage_evidence_links"] = normalize_stage_evidence_links([], stages)
    rebuilt: list[dict[str, Any]] = []
    for item in value["stage_evidence_links"]:
        key = (
            str(item.get("stage_id") or "").strip().upper(),
            str(item.get("role") or "").strip().lower(),
            str(item.get("evidence_id") or "").strip(),
        )
        prior = existing.get(key)
        rebuilt.append(copy.deepcopy(prior) if isinstance(prior, dict) else item)
    value["stage_evidence_links"] = rebuilt


def _nested_stage_reference_ids(stage: dict[str, Any], role: str) -> set[str]:
    """Collect nested evidence IDs for one stage/role without treating them as facts."""
    prefix = f"{role}_"
    top_level_key = f"{role}_evidence_ids"
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key)
                if key_text == "evidence_ids" or key_text.endswith("_evidence_ids"):
                    if isinstance(nested, list):
                        found.update(str(item).strip() for item in nested if str(item).strip())
                    elif isinstance(nested, dict):
                        walk(nested)
                    continue
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for key, value in stage.items():
        key_text = str(key)
        if key_text.startswith(prefix) and key_text != top_level_key:
            walk(value)
    return found


def stage_evidence_link_issues(result: Any) -> list[str]:
    """Validate explicit links against Stage1 IDs and final stage references."""
    if not isinstance(result, dict):
        return ["result_not_object"]
    stages = result.get("stage_analysis")
    links = result.get("stage_evidence_links")
    if not isinstance(stages, list):
        return ["stage_analysis_not_list"]
    if not isinstance(links, list):
        return ["stage_evidence_links_missing"]

    understanding = result.get("video_understanding")
    active_roles = {
        role
        for role in ("creator", "benchmark")
        if isinstance(understanding, dict)
        and isinstance(understanding.get(role), dict)
        and understanding[role].get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
    }

    expected_refs: set[tuple[str, str, str]] = set()
    nested_reference_issues: list[str] = []
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            continue
        stage_id = normalize_stage_code(stage.get("stage")) or f"S{index}"
        for role in ("creator", "benchmark"):
            for evidence_id in stage.get(f"{role}_evidence_ids") or []:
                token = str(evidence_id or "").strip()
                if token:
                    expected_refs.add((stage_id, role, token))
            nested_ids = _nested_stage_reference_ids(stage, role)
            top_level_ids = {
                str(value).strip()
                for value in stage.get(f"{role}_evidence_ids") or []
                if str(value).strip()
            }
            for evidence_id in sorted(nested_ids - top_level_ids):
                nested_reference_issues.append(
                    f"{stage_id}:{role}:nested_reference_missing_from_stage_list:{evidence_id}"
                )

    units_by_role = {}
    understanding = result.get("video_understanding")
    for role in ("creator", "benchmark"):
        side = understanding.get(role) if isinstance(understanding, dict) else {}
        units_by_role[role] = _evidence_units_by_id(side)

    issues: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    primary_owners: dict[tuple[str, str], str] = {}
    for index, link in enumerate(links):
        if not isinstance(link, dict):
            issues.append(f"link[{index}]:not_object")
            continue
        stage_id = normalize_stage_code(link.get("stage_id"))
        role = str(link.get("role") or "").strip().lower()
        evidence_id = str(link.get("evidence_id") or "").strip()
        relation = str(link.get("relation") or "").strip().lower()
        confidence = str(link.get("confidence") or "").strip().lower()
        source = str(link.get("source") or "").strip().lower()
        reason = str(link.get("linking_reason") or "").strip()
        key = (stage_id or str(link.get("stage_id") or ""), role, evidence_id)
        if stage_id is None:
            issues.append(f"link[{index}]:invalid_stage_id")
        if role not in {"creator", "benchmark"}:
            issues.append(f"link[{index}]:invalid_role")
        if not evidence_id:
            issues.append(f"link[{index}]:missing_evidence_id")
        if relation not in STAGE_EVIDENCE_LINK_RELATIONS:
            issues.append(f"link[{index}]:invalid_relation")
        if confidence not in STAGE_EVIDENCE_LINK_CONFIDENCES:
            issues.append(f"link[{index}]:invalid_confidence")
        if source not in STAGE_EVIDENCE_LINK_SOURCES:
            issues.append(f"link[{index}]:invalid_source")
        if not reason:
            issues.append(f"link[{index}]:missing_linking_reason")
        if role in units_by_role and evidence_id not in units_by_role[role]:
            issues.append(f"link[{index}]:unknown_evidence_id:{evidence_id}")
        if (
            stage_id
            and role in active_roles
            and evidence_id
            and evidence_id in units_by_role.get(role, {})
            and evidence_id not in qualified_stage_evidence_ids(
                understanding.get(role),
                stage_id,
            )
        ):
            # A link is a consumer-side reference.  In the active contract it
            # must pass the same stage-specific qualification gate as the
            # downstream analysis view; merely existing in the immutable
            # Stage1 set is not enough.
            issues.append(
                f"link[{index}]:unqualified_evidence_id:{stage_id}:{role}:{evidence_id}"
            )
        if key in seen:
            issues.append(f"link[{index}]:duplicate_link")
        seen.add(key)
        if role in active_roles and relation == "primary" and evidence_id:
            owner_key = (role, evidence_id)
            prior_stage = primary_owners.get(owner_key)
            if prior_stage and prior_stage != stage_id:
                issues.append(
                    f"link[{index}]:primary_ownership_conflict:{role}:{evidence_id}:{prior_stage}:{stage_id}"
                )
            else:
                primary_owners[owner_key] = stage_id or ""
        if stage_id and (stage_id, role, evidence_id) not in expected_refs:
            issues.append(f"link[{index}]:not_in_stage_reference")

    linked_refs = {
        (normalize_stage_code(item.get("stage_id")) or str(item.get("stage_id") or ""),
         str(item.get("role") or "").strip().lower(),
         str(item.get("evidence_id") or "").strip())
        for item in links
        if isinstance(item, dict)
    }
    for missing in sorted(expected_refs - linked_refs):
        issues.append("missing_link:" + ":".join(missing))
    return [*nested_reference_issues, *issues]


def stage_evidence_gate(
    result: Any,
    stage: Any,
    *,
    comparison_status: Any = None,
) -> dict[str, Any]:
    """Return the code-owned handoff state from Stage1 into stage judgment.

    This is deliberately a policy boundary, not another model judgment.  A
    stage may be judged only when both sides have a closed Stage1 state
    (``present`` or a complete, explicit ``absent``).  ``unknown`` and
    ``conflict`` always block a grounded comparison; they must never be
    reinterpreted as absence.  Legacy facts remain visible for compatibility
    but are marked separately so evaluations cannot silently mix generations.
    """
    code = normalize_stage_code(stage) or str(stage or "").strip().upper()[:2]
    understanding = result.get("video_understanding") if isinstance(result, dict) else {}
    sides = understanding if isinstance(understanding, dict) else {}
    role_states: dict[str, dict[str, Any]] = {}
    for role in ("creator", "benchmark"):
        side = sides.get(role) if isinstance(sides.get(role), dict) else {}
        readiness = stage_evidence_readiness(side, code)
        role_states[role] = {
            "status": readiness,
            "evidence_ids": sorted(qualified_stage_evidence_ids(side, code))
            if readiness == "present"
            else [],
            "evidence_set_sha256": str(side.get("evidence_set_sha256") or "").strip(),
            "diagnostics": stage_evidence_diagnostics(side, code),
        }

    statuses = {item["status"] for item in role_states.values()}
    snapshot_invalid_roles = {
        role
        for role in ("creator", "benchmark")
        if isinstance(sides.get(role), dict)
        and sides[role].get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
        and stage_evidence_snapshot_issues(sides[role], require_snapshot=True)
    }
    normalized_comparison = str(comparison_status or "").strip().lower()
    if "conflict" in statuses:
        gate_status = "blocked"
        reason_code = "conflicting_stage_evidence"
        reason = "Stage1 阶段资格存在冲突，后续不能自行选择其中一套事实。"
    elif snapshot_invalid_roles:
        gate_status = "blocked"
        reason_code = "evidence_snapshot_invalid"
        reason = "Stage1 事实集未通过冻结摘要校验，不能把未锁定事实交给后续判断。"
    elif "unknown" in statuses:
        blocked_roles = [role for role, item in role_states.items() if item["status"] == "unknown"]
        budget_roles = [
            role
            for role in blocked_roles
            if isinstance(sides.get(role), dict) and sides[role].get("evidence_budget_exceeded") is True
        ]
        gate_status = "blocked"
        reason_code = "evidence_budget_exceeded" if budget_roles else "evidence_collection_incomplete"
        reason = (
            "Stage1 证据采集预算未闭合，不能把部分采集当成完整判断。"
            if budget_roles
            else "Stage1 未形成完整、可追溯的阶段证据，不能把未知当成缺失。"
        )
    elif "legacy" in statuses:
        gate_status = "legacy"
        reason_code = "legacy_stage1_contract"
        reason = "至少一侧使用旧版 Stage1 事实合同，保留历史结果但不把它计入新合同的 grounded 结论。"
    elif normalized_comparison in {"not_applicable", "not_directly_comparable", "not_comparable"}:
        gate_status = "not_applicable" if normalized_comparison == "not_applicable" else "not_comparable"
        reason_code = "comparison_scope_closed"
        reason = "比较合同已明确该阶段不适用或不可比。"
    else:
        gate_status = "grounded"
        reason_code = "qualified_stage1_evidence"
        reason = "双方阶段证据均已完成资格化，后续结论可以引用对应证据。"

    return {
        "version": STAGE_EVIDENCE_GATE_VERSION,
        "stage": code,
        "status": gate_status,
        "analysis_allowed": gate_status == "grounded",
        "reason_code": reason_code,
        "reason": reason,
        "comparison_status": normalized_comparison or "direct",
        "creator": role_states["creator"],
        "benchmark": role_states["benchmark"],
        "source": "code",
    }


def materialize_stage_evidence_gates(result: Any) -> None:
    """Attach the same Stage1-to-judgment gate to every S1-S6 result stage."""
    if not isinstance(result, dict):
        return
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            continue
        code = normalize_stage_code(stage.get("stage")) or f"S{index}"
        gate = stage_evidence_gate(
            result,
            code,
            comparison_status=stage.get("comparison_status"),
        )
        stage["stage_evidence_gate"] = gate
        if gate["status"] == "blocked":
            stage["analysis_status"] = "evidence_blocked"
            stage["analysis_reason"] = gate["reason"]
        elif gate["status"] == "legacy":
            stage["analysis_status"] = "legacy_evidence_contract"
            stage["analysis_reason"] = gate["reason"]
        elif gate["status"] in {"not_applicable", "not_comparable"}:
            stage["analysis_status"] = gate["status"]
        else:
            stage["analysis_status"] = "grounded"
            stage.pop("analysis_reason", None)


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
        signal_bindings, invalid_signal_bindings = _normalize_signal_bindings(
            raw.get("signal_bindings"),
            contract,
            valid_ids,
        )
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
            "signal_bindings": signal_bindings,
            "invalid_signal_bindings": invalid_signal_bindings,
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
                "signal_bindings": {},
                "invalid_signal_bindings": [],
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


def _normalize_signal_bindings(
    value: Any,
    contract: StageEvidenceContract,
    valid_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Normalize signal-to-evidence bindings without inventing support.

    A stage-level evidence list is not sufficient to prove every required
    signal.  Each supported signal therefore carries its own evidence IDs. A
    malformed binding becomes ``unknown`` and is retained as an audit issue;
    it is never silently converted into a valid binding.
    """
    raw_bindings = value if isinstance(value, dict) else {}
    bindings: dict[str, dict[str, Any]] = {}
    invalid_signals: list[str] = []
    for raw_signal, raw_binding in raw_bindings.items():
        signal = str(raw_signal or "").strip()
        if signal not in contract.allowed_signals:
            if signal and signal not in invalid_signals:
                invalid_signals.append(signal)
            continue
        if not isinstance(raw_binding, dict):
            raw_binding = {}
        raw_status = str(raw_binding.get("status") or "unknown").strip().lower()
        status = raw_status if raw_status in STAGE_EVIDENCE_BINDING_STATUSES else "unknown"
        evidence_ids: list[str] = []
        invalid_ids: list[str] = []
        raw_ids = raw_binding.get("evidence_ids")
        if isinstance(raw_ids, list):
            for raw_id in raw_ids:
                evidence_id = str(raw_id or "").strip()
                if not evidence_id:
                    continue
                if evidence_id in valid_ids and evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
                elif evidence_id not in invalid_ids:
                    invalid_ids.append(evidence_id)
        bindings[signal] = {
            "status": status,
            "evidence_ids": evidence_ids,
            "invalid_evidence_ids": invalid_ids,
            "reason": str(raw_binding.get("reason") or "").strip(),
        }
    return bindings, invalid_signals


def merge_stage_signal_bindings(*values: Any) -> dict[str, dict[str, Any]]:
    """Merge binding evidence without allowing a later pass to erase facts."""
    merged: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        for signal, raw in value.items():
            if not isinstance(raw, dict):
                continue
            current = merged.setdefault(
                str(signal),
                {
                    "status": "unknown",
                    "evidence_ids": [],
                    "invalid_evidence_ids": [],
                    "reason": "",
                },
            )
            for field in ("evidence_ids", "invalid_evidence_ids"):
                for evidence_id in raw.get(field) or []:
                    token = str(evidence_id or "").strip()
                    if token and token not in current[field]:
                        current[field].append(token)
            statuses = {
                str(current.get("status") or "unknown").strip().lower(),
                str(raw.get("status") or "unknown").strip().lower(),
            }
            if "conflict" in statuses:
                current["status"] = "conflict"
            elif len(statuses - {"unknown"}) > 1:
                # Independent passes disagree about the same signal.  Keep
                # the disagreement explicit so it cannot be mistaken for an
                # ordinary missing observation downstream.
                current["status"] = "conflict"
            elif "supported" in statuses and current.get("evidence_ids"):
                current["status"] = "supported"
            elif "missing" in statuses and statuses <= {"missing", "unknown"}:
                current["status"] = "missing"
            else:
                current["status"] = "unknown"
            reason = str(raw.get("reason") or "").strip()
            if reason and reason not in str(current.get("reason") or ""):
                current["reason"] = "；".join(
                    item for item in (str(current.get("reason") or "").strip(), reason) if item
                )
    return merged


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


def _budget_recovery_allows_qualification(side: Any, stage_code: str) -> bool:
    """Keep a budget-truncated extraction out of every downstream evidence view.

    The recovery metadata is written by the pipeline, not accepted from the
    model response.  Keeping this guard below the public qualification helper
    prevents a new consumer from accidentally treating a partial extraction as
    complete merely because its stage check looks valid.
    """
    if not isinstance(side, dict) or side.get("evidence_budget_exceeded") is not True:
        return True
    recovery = side.get("stage1_recovery")
    if not isinstance(recovery, dict) or recovery.get("source") != "pipeline":
        return False
    if recovery.get("status") not in {
        "applied",
        "applied_with_unresolved",
        "coverage_audited",
        "coverage_audited_with_unresolved",
    }:
        return False
    unresolved = {
        str(value).strip().upper()[:2]
        for value in recovery.get("unresolved_stages") or []
        if str(value).strip()
    }
    return stage_code not in unresolved


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
        "invalid_signal_bindings",
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
    signal_bindings = check.get("signal_bindings") if isinstance(check.get("signal_bindings"), dict) else {}
    contradictory_signals = sorted(observed & missing)
    if contradictory_signals:
        issues.append(f"{contract.code}:signal_both_observed_and_missing:{','.join(contradictory_signals)}")

    # Validate every binding, including bindings for optional signals and
    # negative/unknown stages.  Checking only ``observed`` would let an
    # invalid or unsupported binding hide inside an ``absent`` or ``unknown``
    # projection without blocking qualification.
    for signal, binding in signal_bindings.items():
        if not isinstance(binding, dict):
            issues.append(f"{contract.code}:invalid_signal_binding:{signal}")
            continue
        binding_ids = [
            str(value).strip()
            for value in binding.get("evidence_ids") or []
            if str(value).strip()
        ]
        if binding.get("invalid_evidence_ids"):
            issues.append(f"{contract.code}:signal_binding_invalid_evidence:{signal}")
        outside_stage = sorted(set(binding_ids) - set(evidence_ids))
        if outside_stage:
            issues.append(
                f"{contract.code}:signal_binding_outside_stage_evidence:{signal}:{','.join(outside_stage)}"
            )
        if binding.get("status") == "supported" and not binding_ids:
            issues.append(f"{contract.code}:supported_signal_without_evidence:{signal}")
        elif binding.get("status") != "supported" and binding_ids:
            issues.append(f"{contract.code}:unsupported_signal_with_evidence:{signal}")

    # Every observed signal must point to the exact atomic facts that support
    # it.  The stage-level evidence_ids list remains a compatibility index, but
    # it is no longer sufficient for qualification on its own.
    for signal in sorted(observed):
        binding = signal_bindings.get(signal)
        if not isinstance(binding, dict) or binding.get("status") != "supported":
            issues.append(f"{contract.code}:observed_signal_without_supported_binding:{signal}")

    if status == "present":
        if coverage != "complete":
            issues.append(f"{contract.code}:present_without_complete_coverage")
        if not evidence_ids:
            issues.append(f"{contract.code}:present_without_evidence")
        missing_required = sorted(required - observed)
        if missing_required:
            issues.append(f"{contract.code}:present_missing_required_signals:{','.join(missing_required)}")
        missing_bindings: list[str] = []
        for signal in sorted(required):
            binding = signal_bindings.get(signal)
            if not isinstance(binding, dict) or binding.get("status") != "supported":
                missing_bindings.append(signal)
        if missing_bindings:
            issues.append(
                f"{contract.code}:present_missing_required_signal_bindings:{','.join(missing_bindings)}"
            )
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
        supported_bindings = sorted(
            signal
            for signal, binding in signal_bindings.items()
            if isinstance(binding, dict)
            and binding.get("status") == "supported"
        )
        if coverage != "complete" or evidence_ids or supported_bindings or not required.issubset(missing):
            issues.append(f"{contract.code}:absence_without_complete_coverage")
    elif status in {"unknown", "conflict"}:
        bound_ids = [
            value
            for binding in signal_bindings.values()
            if isinstance(binding, dict)
            for value in binding.get("evidence_ids") or []
            if str(value).strip()
        ]
        if evidence_ids or bound_ids:
            issues.append(f"{contract.code}:{status}_with_evidence")
    else:
        issues.append(f"{contract.code}:invalid_status")

    return issues


def stage1_acquisition_issues(side: Any, stage: Any) -> list[str]:
    """Validate the code-owned input coverage needed by one stage.

    A model saying ``absent`` is not enough to prove a negative.  For a
    negative observation, the channels that could contain the stage signal
    must have been collected; for a positive observation, every channel used by
    its references must have been available.  This rule is shared by all six
    stages and deliberately contains no product-specific thresholds.
    """
    code = normalize_stage_code(stage)
    contract = stage_evidence_contract(code)
    if contract is None:
        return [f"{stage}:invalid_stage"]
    if not isinstance(side, dict):
        return [f"{code}:acquisition_side_missing"]
    checks = stage_evidence_check_map(side)
    check = checks.get(code or "")
    if not isinstance(check, dict) or check.get("status") not in {"present", "absent"}:
        return []
    manifest = normalize_stage1_acquisition(side.get("stage1_acquisition"))
    if manifest.get("version") != STAGE1_ACQUISITION_VERSION:
        return [f"{code}:acquisition_manifest_missing_or_old"]
    if manifest.get("source") != "pipeline":
        return [f"{code}:acquisition_manifest_not_code_owned"]
    channels = manifest.get("channels") if isinstance(manifest.get("channels"), dict) else {}
    issues: list[str] = []

    required_channels: set[str] = set()
    if check.get("status") == "absent":
        # A negative claim requires the visual track and, when the pipeline
        # identified a spoken/subtitle spine, the corresponding text track too.
        required_channels.add("visual")
        mode = str(manifest.get("speech_mode") or "unknown").strip().lower()
        if mode == "spoken":
            required_channels.add("voiceover")
        elif mode == "subtitle_driven":
            required_channels.add("subtitle")
        elif mode not in {"visual_driven", "music_driven"}:
            return [f"{code}:acquisition_spine_unknown_for_negative_claim"]
    else:
        units_by_id = _evidence_units_by_id(side)
        referenced = [
            units_by_id[evidence_id]
            for evidence_id in check.get("evidence_ids") or []
            if evidence_id in units_by_id
        ]
        for channel in ("visual", "voiceover", "subtitle", "audio"):
            if any(_unit_has_channel(unit, channel) for unit in referenced):
                required_channels.add(channel)
        if contract.channel_policy == "visual_required":
            required_channels.add("visual")

        # A syntactically valid visual fact is not proof that the extractor
        # received a frame covering that fact. Native video covers the whole
        # timeline. For sampled inputs, use the actual request timestamps and
        # the unit's own time range; no fixed S1-S6 time slice is assumed.
        visual_units = [unit for unit in referenced if _unit_has_channel(unit, "visual")]
        if visual_units:
            visual_info = channels.get("visual") if isinstance(channels.get("visual"), dict) else {}
            if manifest.get("input_mode") != "native_video" and visual_info.get("coverage") != "full":
                request_timestamps = manifest.get("visual_input_timestamps")
                if not isinstance(request_timestamps, list) or not request_timestamps:
                    issues.append(f"{code}:acquisition_visual_input_unobserved")
                else:
                    duration = manifest.get("duration_seconds")
                    for unit in visual_units:
                        parsed = parse_time_range_seconds(unit.get("time_range"), duration)
                        if parsed is None:
                            issues.append(f"{code}:acquisition_visual_evidence_time_invalid")
                            continue
                        start, end = parsed
                        if not any(start <= timestamp <= end for timestamp in request_timestamps):
                            issues.append(f"{code}:acquisition_visual_input_outside_evidence_range")

    for channel in sorted(required_channels):
        channel_info = channels.get(channel) if isinstance(channels.get(channel), dict) else {}
        if channel_info.get("status") != "ready":
            issues.append(f"{code}:acquisition_channel_unavailable:{channel}")
        elif check.get("status") == "absent" and channel_info.get("coverage") != "full":
            # A sampled frame set can support a positive, directly observed
            # fact, but it cannot prove that a negative fact never occurred
            # between samples. This is deliberately generic for S1-S6; the
            # stage registry decides which channels are non-substitutable.
            issues.append(f"{code}:acquisition_channel_coverage_incomplete:{channel}")
        if channel == "voiceover" and channel_info.get("boundary_precision") != "word":
            # A coarse ASR segment may overlap a stage while containing speech
            # from several stages. It can remain an audit artifact, but it
            # cannot qualify either a positive or a negative stage claim.
            issues.append(f"{code}:acquisition_channel_boundary_imprecise:voiceover")
    return issues


def stage1_coverage_audit_issues(side: Any, stage: Any | None = None) -> list[str]:
    """Validate the independent semantic-coverage pass for one or all stages.

    ``stage_evidence_checks`` is a primary model projection.  It cannot prove
    that the model did not miss a required fact.  An active Stage1 result must
    therefore carry a completed, pipeline-owned coverage audit before any
    stage can be considered grounded.  This function is a runtime gate, not a
    structural contract error: an unavailable audit blocks the affected stage
    and does not invalidate otherwise well-formed facts for every other stage.
    """
    if not isinstance(side, dict):
        return ["coverage_audit_side_missing"]
    if side.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        return []
    audit = normalize_stage1_coverage_audit(
        side.get("stage1_coverage_audit"),
        set(_evidence_units_by_id(side)),
    )
    codes = [normalize_stage_code(stage)] if stage is not None else list(stage_codes())
    issues: list[str] = []
    if audit.get("version") != STAGE1_COVERAGE_AUDIT_VERSION:
        return [f"{code}:coverage_audit_missing_or_old" for code in codes if code]
    if audit.get("source") != "pipeline":
        return [f"{code}:coverage_audit_not_code_owned" for code in codes if code]
    if audit.get("independence") != STAGE1_COVERAGE_AUDIT_INDEPENDENCE:
        return [f"{code}:coverage_audit_independence_unverified" for code in codes if code]
    if audit.get("status") != "completed":
        return [f"{code}:coverage_audit_not_completed" for code in codes if code]
    stages = audit.get("stages") if isinstance(audit.get("stages"), dict) else {}
    checks = stage_evidence_check_map(side)
    for code in codes:
        if not code:
            continue
        item = stages.get(code)
        if not isinstance(item, dict):
            issues.append(f"{code}:coverage_audit_stage_missing")
            continue
        status = item.get("status")
        coverage = item.get("coverage")
        if status not in STAGE1_COVERAGE_AUDIT_STATUSES:
            issues.append(f"{code}:coverage_audit_invalid_status")
            continue
        if status in {"unknown", "conflict"} or coverage != "complete":
            issues.append(f"{code}:coverage_audit_scope_incomplete")
        check = checks.get(code)
        primary_status = check.get("status") if isinstance(check, dict) else "unknown"
        if primary_status == "present" and status != "found":
            issues.append(f"{code}:coverage_audit_disagrees_with_present")
        elif primary_status == "absent" and status != "clear":
            issues.append(f"{code}:coverage_audit_disagrees_with_absent")
    return issues


def stage_evidence_runtime_issues(side: Any, stage: Any) -> list[str]:
    """Return runtime gate issues without treating them as schema corruption."""
    return [
        *stage1_acquisition_issues(side, stage),
        *stage1_coverage_audit_issues(side, stage),
    ]


def stage_evidence_diagnostics(side: Any, stage: Any) -> dict[str, Any]:
    """Explain why one stage is or is not eligible for downstream analysis.

    The diagnostics are code-owned observability, not another decision layer.
    Stable reason codes let reports and historical audits distinguish a weak
    primary projection from unavailable acquisition or an incomplete semantic
    coverage pass without parsing free-form error strings.
    """
    code = normalize_stage_code(stage) or str(stage or "").strip().upper()
    if not isinstance(side, dict):
        return {
            "status": "unknown",
            "primary_status": "missing",
            "acquisition_status": "unknown",
            "coverage_audit_status": "unknown",
            "reason_codes": ["side_missing"],
            "issues": ["side_missing"],
        }

    active = side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
    if not active:
        return {
            "status": "legacy",
            "primary_status": "legacy",
            "acquisition_status": "legacy",
            "coverage_audit_status": "legacy",
            "reason_codes": ["legacy_contract"],
            "issues": [],
        }

    check = stage_evidence_check_map(side).get(code)
    primary_status = str(check.get("status") or "unknown").strip().lower() if isinstance(check, dict) else "missing"
    manifest = normalize_stage1_acquisition(side.get("stage1_acquisition"))
    audit = normalize_stage1_coverage_audit(
        side.get("stage1_coverage_audit"),
        set(_evidence_units_by_id(side)),
    )
    acquisition_issues = stage1_acquisition_issues(side, code)
    audit_issues = stage1_coverage_audit_issues(side, code)
    snapshot_issues = stage_evidence_snapshot_issues(side, require_snapshot=True)
    primary_issues = []
    contract = stage_evidence_contract(code)
    if contract is not None and isinstance(check, dict):
        primary_issues = _stage_check_issues(contract, check, _evidence_units_by_id(side))
    issues = [*primary_issues, *acquisition_issues, *audit_issues]
    reason_codes: list[str] = []
    if primary_status == "missing" or primary_status == "unknown":
        reason_codes.append("primary_unknown")
    elif primary_status == "conflict":
        reason_codes.append("primary_conflict")
    if primary_issues:
        reason_codes.append("primary_qualification_gate")
    if acquisition_issues:
        reason_codes.append("acquisition_gate")
    if audit_issues:
        reason_codes.append("coverage_audit_gate")
    if snapshot_issues:
        reason_codes.append("snapshot_invalid")
        issues.extend(f"snapshot:{item}" for item in snapshot_issues)
    if not reason_codes:
        reason_codes.append("ready")
    return {
        "status": stage_evidence_readiness(side, code),
        "primary_status": primary_status,
        "acquisition_status": str(manifest.get("status") or "unknown"),
        "coverage_audit_status": str(audit.get("status") or "unknown"),
        "reason_codes": reason_codes,
        "issues": list(dict.fromkeys(str(item) for item in issues if str(item).strip())),
    }


def qualified_stage_evidence_ids(side: Any, stage: Any, *, allow_inferred: bool = False) -> set[str]:
    """Return IDs qualified by the locked unit facts, never inferred from functions."""
    stage_code = normalize_stage_code(stage) or ""
    if (
        isinstance(side, dict)
        and side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
        and stage_evidence_snapshot_issues(side, require_snapshot=True)
    ):
        # This helper is called directly by many consumers.  Do not rely on a
        # caller having checked stage_evidence_readiness first.
        return set()
    if not _budget_recovery_allows_qualification(side, stage_code):
        return set()
    check = stage_evidence_check_map(side).get(stage_code)
    if not isinstance(check, dict) or check.get("status") != "present":
        return set()
    # Qualification is the only input boundary for Stage2/derive.  Do not
    # expose a syntactically valid model claim when the code-owned acquisition
    # manifest says the underlying channel or stage coverage was unavailable.
    if stage_evidence_runtime_issues(side, stage_code):
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


def qualified_stage_evidence_units(side: Any, stages: list[Any] | set[Any] | None = None) -> list[dict[str, Any]]:
    """Return locked observation records that are qualified for at least one stage.

    This is the safe bridge for derived metrics that do not belong to one
    stage, such as product visibility.  Active contracts use the union of
    stage-qualified IDs; legacy artifacts retain their historical raw path.
    """
    if not isinstance(side, dict):
        return []
    units = [item for item in side.get("evidence_units") or [] if isinstance(item, dict)]
    if side.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        return units
    selected = {
        normalize_stage_code(stage)
        for stage in (stages or stage_codes())
        if normalize_stage_code(stage) in stage_codes()
    }
    allowed: set[str] = set()
    for stage in selected:
        allowed.update(qualified_stage_evidence_ids(side, stage))
    return [unit for unit in units if str(unit.get("id") or "").strip() in allowed]


def stage_evidence_readiness(side: Any, stage: Any) -> str:
    """Return whether a stage may feed deterministic downstream relations.

    ``present`` requires the same locked qualification used by the resolver;
    ``absent`` is a valid complete negative observation. ``unknown`` and
    ``conflict`` remain non-actionable instead of being collapsed into absent.
    Legacy results return ``legacy`` so callers can retain their compatibility
    path without weakening the active contract.
    """
    if not isinstance(side, dict) or side.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        return "legacy"
    if stage_evidence_snapshot_issues(side, require_snapshot=True):
        return "unknown"
    stage_code = normalize_stage_code(stage)
    contract = stage_evidence_contract(stage_code)
    check = stage_evidence_check_map(side).get(stage_code or "")
    if contract is None or not isinstance(check, dict):
        return "unknown"
    if not _budget_recovery_allows_qualification(side, stage_code):
        return "unknown"
    # Preserve an explicit disagreement even when the independent audit is
    # itself incomplete.  Both states remain blocked downstream, but callers
    # must be able to distinguish "the two observations conflict" from "we
    # never completed the coverage scan" for diagnosis and retraining.
    if check.get("status") == "conflict":
        return "conflict"
    if stage_evidence_runtime_issues(side, stage_code):
        return "unknown"
    status = check.get("status")
    issues = _stage_check_issues(contract, check, _evidence_units_by_id(side))
    if status == "present":
        return "present" if not issues and qualified_stage_evidence_ids(side, stage_code) else "unknown"
    if status == "absent":
        return "absent" if not issues else "unknown"
    if status in {"unknown", "conflict"}:
        return status
    return "unknown"


def _filter_evidence_references(value: Any, allowed_ids: set[str]) -> Any:
    """Filter nested evidence references while preserving non-evidence facts."""
    if isinstance(value, dict):
        filtered: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if key_text == "evidence_ids" or key_text.endswith("_evidence_ids"):
                if isinstance(nested, list):
                    filtered[key] = [item for item in nested if str(item).strip() in allowed_ids]
                elif isinstance(nested, dict):
                    filtered[key] = {
                        nested_key: [item for item in nested_value if str(item).strip() in allowed_ids]
                        if isinstance(nested_value, list)
                        else _filter_evidence_references(nested_value, allowed_ids)
                        for nested_key, nested_value in nested.items()
                    }
                else:
                    filtered[key] = nested
                continue
            filtered[key] = _filter_evidence_references(nested, allowed_ids)
        return filtered
    if isinstance(value, list):
        return [_filter_evidence_references(item, allowed_ids) for item in value]
    return value


def _stage_units_for_side(
    side: dict[str, Any],
    qualified_by_stage: dict[str, set[str]],
) -> dict[str, list[dict[str, Any]]]:
    """Return full observation records partitioned by the stage that qualified them."""
    units_by_id = _evidence_units_by_id(side)
    return {
        stage: [
            copy.deepcopy(units_by_id[evidence_id])
            for evidence_id in sorted(ids)
            if evidence_id in units_by_id
        ]
        for stage, ids in qualified_by_stage.items()
    }


def stage_analysis_stage_context(
    stage: Any,
    video_understanding: Any,
    stage_code_value: Any,
) -> dict[str, Any]:
    """Build a phase-review context without exposing unqualified stage claims.

    The current stage result is not an evidence source.  Phase C needs the
    target window and the qualified IDs for orientation, but it must not see
    stale summaries or nested flags that could make an old conclusion look
    like a newly verified fact.
    """
    if not isinstance(stage, dict):
        return {}
    code = normalize_stage_code(stage_code_value or stage.get("stage")) or ""
    context: dict[str, Any] = {
        "stage": stage.get("stage") or code,
        "time_range": stage.get("time_range"),
        "creator_time_range": stage.get("creator_time_range"),
        "benchmark_time_range": stage.get("benchmark_time_range"),
        "core_question": stage.get("core_question"),
        "analysis_evidence_scope": "qualified_stage_evidence_only",
    }
    sides = video_understanding if isinstance(video_understanding, dict) else {}
    for role in ("creator", "benchmark"):
        side = sides.get(role) if isinstance(sides.get(role), dict) else {}
        readiness = stage_evidence_readiness(side, code)
        allowed = sorted(qualified_stage_evidence_ids(side, code)) if readiness == "present" else []
        context[f"{role}_stage_evidence_readiness"] = readiness
        context[f"{role}_evidence_ids"] = allowed
    return context


# These fields are useful audit observations, but they are not themselves a
# stage qualification.  Exposing them in an active analysis payload would let
# a downstream model infer a stage conclusion from an unqualified summary or
# checklist even after the canonical evidence gate has returned unknown.
_UNQUALIFIED_ANALYSIS_OBSERVATION_FIELDS = (
    "content_summary",
    "communication_strategy",
    "selling_point_observations",
    "variant_decision_rule",
    "attention_scan_audit",
    "attention_competitors",
    "gate_observation_status",
    "evidence_checklist",
    "structure_event_checks",
)

# These are pipeline-owned acquisition/qualification internals. They remain
# in the persisted audit artifact, but must not be exposed to a downstream
# model as a second, unqualified source of stage facts.
_STAGE1_INTERNAL_ANALYSIS_FIELDS = (
    "stage1_acquisition",
    "stage1_coverage_audit",
    "stage1_recovery",
    "evidence_budget_exceeded",
    "evidence_set_version",
    "evidence_set_sha256",
    "evidence_set_status",
    "evidence_set_source",
)


def _stage_analysis_side_view(side: Any, target_stages: set[str] | None) -> dict[str, Any]:
    """Build the only Stage1 view that downstream judgment may consume."""
    if not isinstance(side, dict):
        return {}
    view = copy.deepcopy(side)
    if side.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        view["analysis_evidence_scope"] = "legacy_raw"
        return view

    selected_stages = target_stages or set(stage_codes())
    qualified_by_stage = {
        stage: qualified_stage_evidence_ids(side, stage)
        for stage in selected_stages
        if stage in stage_codes()
    }
    readiness_by_stage = {
        stage: stage_evidence_readiness(side, stage)
        for stage in selected_stages
        if stage in stage_codes()
    }
    allowed_ids = set().union(*qualified_by_stage.values()) if qualified_by_stage else set()
    stage_units = _stage_units_for_side(side, qualified_by_stage)
    view["stage_evidence_units"] = stage_units
    # Keep a small ID/time index for compatibility.  Full observation content
    # is only exposed inside the stage-scoped map above, so a consumer cannot
    # accidentally treat a qualified S4 record as an unscoped S3 fact.
    view["evidence_units"] = [
        {
            "id": unit.get("id"),
            "time_range": unit.get("time_range"),
            "evidence_strength": unit.get("evidence_strength"),
            "qualified_stages": sorted(
                stage for stage, ids in qualified_by_stage.items()
                if str(unit.get("id") or "") in ids
            ),
        }
        for unit in side.get("evidence_units") or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip() in allowed_ids
    ]
    view = _filter_evidence_references(view, allowed_ids)
    for field in _UNQUALIFIED_ANALYSIS_OBSERVATION_FIELDS:
        view.pop(field, None)
    for field in _STAGE1_INTERNAL_ANALYSIS_FIELDS:
        view.pop(field, None)
    projected_checks = []
    for check in view.get("stage_evidence_checks") or []:
        if not isinstance(check, dict):
            continue
        projected = dict(check)
        code = normalize_stage_code(projected.get("stage"))
        readiness = readiness_by_stage.get(code or "")
        if readiness in {"unknown", "conflict"}:
            # The model's signal checklist is only a qualification claim.  If
            # the referenced observations fail the canonical gate, retaining
            # those signal names would let a downstream model infer the stage
            # conclusion without a qualified evidence unit.
            projected.update(
                {
                    "status": readiness,
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "observed_disqualifiers": [],
                    "evidence_strength": None,
                    "reason": "代码资格校验未通过，Stage2 不得将该阶段视为已采到证据。",
                }
            )
        projected_checks.append(projected)
    view["stage_evidence_checks"] = projected_checks
    view["qualified_stage_evidence_ids"] = {
        stage: sorted(ids)
        for stage, ids in qualified_by_stage.items()
    }
    view["stage_evidence_readiness"] = readiness_by_stage
    view["analysis_evidence_scope"] = "qualified_stage_evidence_only"
    view["analysis_evidence_stages"] = sorted(selected_stages)
    return view


def stage_analysis_evidence_view(value: Any, target_stages: list[Any] | set[Any] | None = None) -> Any:
    """Return a non-authoritative, qualification-filtered view for downstream models.

    The persisted Stage1 result remains the audit record.  This separate view
    prevents Stage2/Phase C payloads from using raw units that a stage check did
    not qualify.  Legacy results intentionally retain their old raw view for
    compatibility and are marked so callers can keep the legacy audit path.
    """
    if not isinstance(value, dict):
        return {}
    normalized_targets = {
        normalize_stage_code(stage)
        for stage in (target_stages or [])
        if normalize_stage_code(stage) in stage_codes()
    }
    selected = normalized_targets or None
    roles = [role for role in ("benchmark", "creator") if isinstance(value.get(role), dict)]
    if roles:
        view = copy.deepcopy(value)
        for role in roles:
            view[role] = _stage_analysis_side_view(value[role], selected)
        return view
    if value.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
        return _stage_analysis_side_view(value, selected)
    view = copy.deepcopy(value)
    view["analysis_evidence_scope"] = "legacy_raw"
    return view


def stage_evidence_recovery_targets(
    side: Any,
    *,
    include_budget: bool = True,
    include_coverage_audit: bool = True,
) -> list[str]:
    """Stages that need one bounded pre-lock re-observation pass.

    ``evidence_budget_exceeded`` is a video-wide signal.  It cannot identify a
    single stage safely, so it deliberately opens one bounded pass for the
    complete registry rather than silently locking a potentially incomplete
    observation set.
    """
    if include_budget and isinstance(side, dict) and side.get("evidence_budget_exceeded") is True:
        return list(stage_codes())
    targets: list[str] = []
    units_by_id = _evidence_units_by_id(side)
    for contract in STAGE_EVIDENCE_CONTRACTS:
        check = stage_evidence_check_map(side).get(contract.code)
        if not isinstance(check, dict):
            targets.append(contract.code)
            continue
        if (
            check.get("status") in {"unknown", "conflict"}
            or _stage_check_issues(contract, check, units_by_id)
            or stage1_acquisition_issues(side, contract.code)
            or (include_coverage_audit and stage1_coverage_audit_issues(side, contract.code))
        ):
            targets.append(contract.code)
    return targets


def stage_evidence_contract_issues(side: Any, *, require_version: bool = True) -> list[str]:
    """Return structural issues without converting unknown into absence."""
    issues: list[str] = []
    if not isinstance(side, dict):
        return ["side_not_object"]
    if require_version and side.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        issues.append("missing_or_old_contract_version")
    if side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
        issues.extend(
            "forbidden_stage1_field:" + path
            for path in stage1_forbidden_field_issues(side)
        )
    checks = stage_evidence_check_map(side)
    if set(checks) != set(stage_codes()):
        issues.append("stage_checks_incomplete")
    units = _evidence_units_by_id(side)
    duplicate_ids = _duplicate_evidence_ids(side)
    if duplicate_ids:
        issues.append("duplicate_evidence_ids:" + ",".join(duplicate_ids))
    for stage in stage_codes():
        issues.extend(_stage_check_issues(_CONTRACT_BY_STAGE[stage], checks.get(stage), units))
        # Runtime acquisition and semantic-coverage gaps belong to the
        # per-stage gate.  They must not make a structurally valid result
        # fail as a whole; another stage may still be grounded.
    return issues


def project_functions_from_stage_checks(side: Any) -> dict[str, set[str]]:
    """Compatibility view for old consumers; stage checks remain authoritative."""
    output: dict[str, set[str]] = {}
    for stage, check in stage_evidence_check_map(side).items():
        if check.get("status") == "present":
            for evidence_id in check.get("evidence_ids") or []:
                output.setdefault(str(evidence_id), set()).add(f"{stage}_evidence")
    return output
