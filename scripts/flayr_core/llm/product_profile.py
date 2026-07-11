"""产品地基与短视频视觉证明合同的归一化。"""

from __future__ import annotations

import re
from typing import Any


_PROOF_MODES = {
    "instant_visual", "process_result", "sensory_proxy", "aesthetic_value", "social_reaction",
    "long_term_record", "trust_substituted", "low_decision_light_proof",
}
_EFFECT_REQUIRES_PROCESS = {"true", "false", "partial"}
_PROOF_SIGNAL_TYPES = {
    "state_change", "process_event", "sensory_response", "aesthetic_appeal", "social_response",
    "long_term_record", "trust_evidence", "light_proof",
}
_PROOF_PLAN_RANKS = {"low": 1, "medium": 2, "high": 3}
_PROOF_PLAN_STAGES = {"S2", "S3", "S4", "S5"}
_PROOF_PLAN_SOURCES = {"model_category_default", "operator_priority", "curated_priority"}
_DIRECT_VISUAL_PROOF_MODES = {"instant_visual", "process_result"}
_SIGNAL_TYPES_BY_MODE = {
    "instant_visual": {"state_change"},
    "process_result": {"state_change", "process_event"},
    "sensory_proxy": {"sensory_response"},
    "aesthetic_value": {"aesthetic_appeal"},
    "social_reaction": {"social_response"},
    "long_term_record": {"long_term_record"},
    "trust_substituted": {"trust_evidence"},
    "low_decision_light_proof": {"light_proof"},
}
_CAPTURE_CONDITION_RE = re.compile(r"(?:同一?光|同机位|同距离|同构图|特写|近景|微距|高清|拍摄|镜头|构图|光线)")
_OBSERVABLE_SIGNAL_RE = re.compile(
    r"(?:变化|差异|状态|反光|纹理|污渍|颜色|色泽|水珠|残留|覆盖|泡沫|体积|速度|反应|记录|认证|评论|评分|前后|vs|→|->)",
    re.I,
)
_COMPOUND_PRIMARY_RE = re.compile(r"[、，,；;+]|(?:与|及|和|且|并|同时)")
_COMPOUND_DIMENSION_RE = re.compile(r"[、，,；;+]|(?:与|及|同时)")


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def normalize_proof_mode(value: Any) -> str:
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return mode if mode in _PROOF_MODES else "instant_visual"


def normalize_effect_requires_process(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"yes", "1", "是"}:
        return "true"
    if token in {"no", "0", "否"}:
        return "false"
    return token if token in _EFFECT_REQUIRES_PROCESS else "partial"


def normalize_category_profile(value: Any) -> dict[str, Any] | None:
    """品类画像归一（4d）：模型只报事实与世界知识，权重政策在代码（postprocess/derive.py）。"""
    if not isinstance(value, dict):
        return None
    painpoints = [str(p).strip() for p in value.get("painpoints") or [] if str(p).strip()][:10]
    return {
        "category_name": str(value.get("category_name") or "").strip(),
        "price_tier": normalize_choice(value.get("price_tier"), {"low", "mid", "high"}, "mid"),
        # 来源占位（model_fallback）：postprocess 若发现运营档位会改写为 operator 并填 price
        "price_tier_source": "model_fallback",
        "price": "",
        "decision_threshold": normalize_choice(value.get("decision_threshold"), {"impulse", "considered"}, "considered"),
        "drive_type": normalize_choice(value.get("drive_type"), {"emotional", "functional", "mixed"}, "functional"),
        "painpoints": painpoints,
    }


def normalize_product_profile(value: Any) -> dict[str, Any] | None:
    """产品商业 DNA 归一：判分前模型先立的"本品视觉命题"尺子。

    visual_proof_points.primary 是 S4 执行分主锚；core_visual_proposition 只做旧结果回退。
    模型只报产品事实 + 品类世界知识；后续运营/DNA 库可经 postprocess 覆盖（同 price_tier 降级链）。
    """
    if not isinstance(value, dict):
        return None
    multipliers = [str(m).strip() for m in value.get("trust_multipliers") or [] if str(m).strip()][:6]
    dimensions = [str(d).strip() for d in value.get("visual_diff_dimensions") or [] if str(d).strip()][:3]
    selling_points = [str(s).strip() for s in value.get("core_selling_points") or [] if str(s).strip()][:6]
    proof_plan = normalize_short_video_proof_plan(value.get("short_video_proof_plan"))
    proof_contract = normalize_proof_contract(value.get("proof_contract"))
    if proof_plan is not None and proof_contract is not None and proof_contract["valid"]:
        plan_error = validate_proof_contract_against_plan(proof_contract, proof_plan)
        if plan_error:
            proof_contract["valid"] = False
            proof_contract["validation_reason"] = plan_error
    visual_proof_points = normalize_visual_proof_points(value.get("visual_proof_points"))
    proof_mode = normalize_proof_mode(value.get("proof_mode"))
    if proof_contract is not None:
        proof_mode = proof_contract["mode"]
        if proof_contract["valid"] and proof_mode in _DIRECT_VISUAL_PROOF_MODES:
            visual_proof_points = _contract_primary_visual_proof(proof_contract, visual_proof_points)
        elif proof_contract["valid"]:
            # 非直接视觉证明不允许伪造 S4-A~F 的 before/after 主锚；其证据由对应模式消费。
            visual_proof_points = [point for point in visual_proof_points if point["priority"] != "primary"]
        else:
            # 合同未通过时不允许旧字段绕过门禁继续充当 S4 强主锚。
            visual_proof_points = []
    return {
        # 可视化分叉：no（香水/保健品等效果拍不出）时 S4 视觉审计失效，判断权重应转 S5/达人可信度
        "visualizable": normalize_choice(value.get("visualizable"), {"yes", "no"}, "yes"),
        "physical_task": str(value.get("physical_task") or "").strip(),
        # S1 钩子命题：本品最有拦截力的点（模型推，运营可经降级链覆盖）
        "hook_proposition": str(value.get("hook_proposition") or "").strip(),
        # S3 主轴：本品核心卖点（使用过程要演示传递的对象，模型推/运营可供给）
        "core_selling_points": selling_points,
        # S3 场景层：本品典型使用场景（卖点演示的舞台，判场景适配/丰富/连贯的基准）
        "usage_context": str(value.get("usage_context") or "").strip(),
        "core_visual_proposition": str(value.get("core_visual_proposition") or "").strip(),
        "visual_proof_points": visual_proof_points,
        # 产品可以有多个商业卖点。该计划只负责选出 S4 的单一可测视觉锚点，并记录其余卖点应在哪一阶段传递。
        "short_video_proof_plan": proof_plan,
        # proof_contract 是 Step-0 的权威产品证明合同；旧字段保留，只供历史结果降级。
        "proof_contract": proof_contract,
        # 证明模式：S4 如何证明价值。视觉即时效果只是其中一种，低价颜值品/长周期品/感官品要避免硬套 before-after。
        "proof_mode": proof_mode,
        "effect_requires_process": normalize_effect_requires_process(value.get("effect_requires_process")),
        # before/after 应变化的视觉维度（S4 核验对比只看这些；未来 CV 检测层的维度钩子）
        "visual_diff_dimensions": dimensions,
        "trust_multipliers": multipliers,
        "shooting_requirement": str(value.get("shooting_requirement") or "").strip(),
        # 来源占位（model_inferred）：postprocess 命中 DNA 库或运营供给时改写为 library/operator
        "dna_source": "model_inferred",
        "confidence": normalize_choice(value.get("confidence"), {"high", "low"}, "high"),
    }


def normalize_proof_contract(value: Any) -> dict[str, Any] | None:
    """归一 Step-0 产品证明合同，校验字段职责而不猜某个品的正确卖点。"""
    if not isinstance(value, dict):
        return None
    raw_mode = str(value.get("mode") or "").strip().lower().replace("-", "_").replace(" ", "_")
    mode = normalize_proof_mode(raw_mode)
    signal_type = str(value.get("signal_type") or "").strip().lower().replace("-", "_").replace(" ", "_")
    observable_signal = str(value.get("observable_signal") or "").strip()
    observable_dimension = str(value.get("observable_dimension") or "").strip()
    if not observable_dimension:
        observable_dimension = _infer_observable_dimension(observable_signal)
    contract = {
        "anchor_candidate_id": str(value.get("anchor_candidate_id") or "").strip(),
        "mode": mode,
        "consumer_outcome": str(value.get("consumer_outcome") or "").strip(),
        "signal_type": signal_type,
        "observable_dimension": observable_dimension,
        "observable_signal": observable_signal,
        "before_state": str(value.get("before_state") or "").strip(),
        "after_state": str(value.get("after_state") or "").strip(),
        "proof_condition": str(value.get("proof_condition") or "").strip(),
        "valid": False,
        "validation_reason": "",
    }
    if raw_mode not in _PROOF_MODES:
        contract["validation_reason"] = "缺少或非法 proof_contract.mode"
        return contract
    if not contract["consumer_outcome"]:
        contract["validation_reason"] = "缺少 consumer_outcome"
        return contract
    # consumer_outcome 是面向消费者的自然语言结果，允许同一结果里的谓语连接。
    # 单一主证明的硬边界落在 observable_signal：只能用一个可复核维度衡量，
    # 不能因为文案里出现“并/且”就把语义仍单一的结果误判为多个卖点。
    if signal_type not in _PROOF_SIGNAL_TYPES:
        contract["validation_reason"] = "signal_type 不在允许枚举中"
        return contract
    if signal_type not in _SIGNAL_TYPES_BY_MODE[mode]:
        contract["validation_reason"] = "proof_mode 与 signal_type 不匹配"
        return contract
    if not contract["observable_signal"]:
        contract["validation_reason"] = "缺少 observable_signal"
        return contract
    if not contract["observable_dimension"]:
        contract["validation_reason"] = "缺少 observable_dimension"
        return contract
    if _COMPOUND_DIMENSION_RE.search(contract["observable_dimension"]):
        contract["validation_reason"] = "observable_dimension 必须只保留一个可观察维度"
        return contract
    if (
        _CAPTURE_CONDITION_RE.search(contract["observable_signal"])
        and not _OBSERVABLE_SIGNAL_RE.search(contract["observable_signal"])
    ):
        contract["validation_reason"] = "observable_signal 只描述拍摄条件，不是可观察信号"
        return contract
    if not contract["proof_condition"]:
        contract["validation_reason"] = "缺少 proof_condition"
        return contract
    if mode in _DIRECT_VISUAL_PROOF_MODES:
        if not contract["before_state"] or not contract["after_state"]:
            contract["validation_reason"] = "直接视觉证明必须给出 before_state 与 after_state"
            return contract
        if contract["before_state"] == contract["after_state"]:
            contract["validation_reason"] = "before_state 与 after_state 不得相同"
            return contract
    contract["valid"] = True
    return contract


def _infer_observable_dimension(observable_signal: str) -> str:
    """兼容旧合同：从“维度从/由 before 变为 after”中提取单一维度名。"""
    text = str(observable_signal or "").strip()
    if not text:
        return ""
    for marker in ("从", "由", "：", ":", "→", "->"):
        index = text.find(marker)
        if index > 0:
            return text[:index].strip()
    return text


def normalize_short_video_proof_plan(value: Any) -> dict[str, Any] | None:
    """归一短视频证明计划。

    这个计划不替产品决定唯一商业卖点；它要求模型先列全候选，再按可视展示空间、功能中心性、理解成本
    选出一个 S4 测量锚点。代码仅验证计划内部排序、阶段归属和合同引用，不猜某个品该选什么。
    """
    if not isinstance(value, dict):
        return None
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    invalid_reason = ""
    for raw in value.get("candidates") or []:
        if not isinstance(raw, dict):
            invalid_reason = "candidates 含非法项"
            continue
        candidate_id = str(raw.get("id") or "").strip()
        selling_point = str(raw.get("selling_point") or "").strip()
        stage = str(raw.get("delivery_stage") or "").strip().upper()
        proof_mode = normalize_proof_mode(raw.get("proof_mode")) if stage == "S4" else ""
        candidate = {
            "id": candidate_id,
            "selling_point": selling_point,
            "visual_space": normalize_choice(raw.get("visual_space"), set(_PROOF_PLAN_RANKS), ""),
            "functional_centrality": normalize_choice(raw.get("functional_centrality"), set(_PROOF_PLAN_RANKS), ""),
            "comprehension_cost": normalize_choice(raw.get("comprehension_cost"), set(_PROOF_PLAN_RANKS), ""),
            "delivery_stage": stage,
            "proof_mode": proof_mode,
            "reason": str(raw.get("reason") or "").strip(),
        }
        if not candidate_id or candidate_id in seen_ids:
            invalid_reason = invalid_reason or "candidate id 缺失或重复"
        elif not selling_point:
            invalid_reason = invalid_reason or "candidate 缺少 selling_point"
        elif not all(candidate[key] in _PROOF_PLAN_RANKS for key in ("visual_space", "functional_centrality", "comprehension_cost")):
            invalid_reason = invalid_reason or "candidate 排序维度必须是 low|medium|high"
        elif stage not in _PROOF_PLAN_STAGES:
            invalid_reason = invalid_reason or "candidate delivery_stage 必须是 S2|S3|S4|S5"
        elif stage == "S4" and candidate["proof_mode"] not in _PROOF_MODES:
            invalid_reason = invalid_reason or "S4 candidate 缺少合法 proof_mode"
        else:
            candidates.append(candidate)
            seen_ids.add(candidate_id)
        if len(candidates) >= 6:
            break

    anchor_id = str(value.get("s4_anchor_candidate_id") or "").strip()
    source = normalize_choice(value.get("selection_source"), _PROOF_PLAN_SOURCES, "model_category_default")
    confidence = normalize_choice(value.get("anchor_confidence"), {"high", "low"}, "low")
    plan = {
        "candidates": candidates,
        "s4_anchor_candidate_id": anchor_id,
        "selection_source": source,
        "anchor_confidence": confidence,
        "valid": False,
        "validation_reason": invalid_reason,
    }
    if invalid_reason:
        return plan
    if not candidates:
        plan["validation_reason"] = "至少需要一个卖点 candidate"
        return plan
    s4_candidates = [candidate for candidate in candidates if candidate["delivery_stage"] == "S4"]
    if not s4_candidates:
        if anchor_id:
            plan["validation_reason"] = "无 S4 candidate 时不得指定 s4_anchor_candidate_id"
            return plan
        plan["valid"] = True
        return plan
    selected = next((candidate for candidate in s4_candidates if candidate["id"] == anchor_id), None)
    if selected is None:
        plan["validation_reason"] = "s4_anchor_candidate_id 必须指向一个 S4 candidate"
        return plan
    selected_rank = _proof_plan_rank(selected)
    if any(_proof_plan_rank(candidate) > selected_rank for candidate in s4_candidates):
        plan["validation_reason"] = "S4 anchor 未按可视展示空间、功能中心性、理解成本选择最高候选"
        return plan
    plan["valid"] = True
    return plan


def _proof_plan_rank(candidate: dict[str, Any]) -> tuple[int, int, int]:
    """S4 候选按视觉展示空间优先，其次产品主要功能，最后才看理解成本。"""
    return (
        _PROOF_PLAN_RANKS.get(str(candidate.get("visual_space") or ""), 0),
        _PROOF_PLAN_RANKS.get(str(candidate.get("functional_centrality") or ""), 0),
        -_PROOF_PLAN_RANKS.get(str(candidate.get("comprehension_cost") or ""), 0),
    )


def validate_proof_contract_against_plan(contract: dict[str, Any], plan: dict[str, Any]) -> str:
    """验证合同只消费短视频计划中已选定的 S4 锚点；旧结果没有计划时由调用方兼容。"""
    if plan.get("valid") is not True:
        return str(plan.get("validation_reason") or "short_video_proof_plan 无效")
    anchor_id = str(plan.get("s4_anchor_candidate_id") or "").strip()
    contract_anchor = str(contract.get("anchor_candidate_id") or "").strip()
    direct_visual = contract.get("mode") in _DIRECT_VISUAL_PROOF_MODES
    if not anchor_id:
        if direct_visual:
            return "直接视觉 proof_contract 必须对应 short_video_proof_plan 的 S4 anchor"
        return ""
    selected = next(
        (candidate for candidate in plan.get("candidates") or [] if candidate.get("id") == anchor_id),
        None,
    )
    if not selected:
        return "short_video_proof_plan 找不到 S4 anchor"
    if contract_anchor != anchor_id:
        return "proof_contract.anchor_candidate_id 必须等于 short_video_proof_plan.s4_anchor_candidate_id"
    if selected.get("proof_mode") != contract.get("mode"):
        return "proof_contract.mode 必须与 S4 anchor 的 proof_mode 一致"
    return ""


def _contract_primary_visual_proof(contract: dict[str, Any], points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """以通过校验的合同生成唯一 primary，避免模型自由文本把卖点或拍摄条件当证明标准。"""
    secondary = [dict(point) for point in points if point.get("priority") != "primary"]
    primary = {
        "priority": "primary",
        "proof_target": contract["consumer_outcome"],
        "visual_standard": f"{contract['before_state']} vs {contract['after_state']}",
        "visual_diff_dimensions": [contract["observable_dimension"]],
        "related_selling_points": [],
    }
    return [primary, *secondary][:4]


def normalize_visual_proof_points(value: Any) -> list[dict[str, Any]]:
    """归一 S4 多视觉证明点；兼容旧 product_profile 无此字段。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        proof_target = str(item.get("proof_target") or item.get("target") or "").strip()
        visual_standard = str(item.get("visual_standard") or item.get("standard") or "").strip()
        if not proof_target and not visual_standard:
            continue
        dims = [str(dim).strip() for dim in item.get("visual_diff_dimensions") or [] if str(dim).strip()][:3]
        related = [str(point).strip() for point in item.get("related_selling_points") or [] if str(point).strip()][:4]
        point = {
            "priority": normalize_choice(item.get("priority"), {"primary", "secondary"}, "secondary"),
            "proof_target": proof_target,
            "visual_standard": visual_standard,
            "visual_diff_dimensions": dims,
            "related_selling_points": related,
        }
        for normalized_point in _split_compound_primary_visual_proof(point):
            out.append(_repair_result_primary_visual_standard(normalized_point))
        if len(out) >= 4:
            break
    if out and not any(point["priority"] == "primary" for point in out):
        out[0]["priority"] = "primary"
    return out[:4]


def _split_compound_primary_visual_proof(point: dict[str, Any]) -> list[dict[str, Any]]:
    """把模型误写成 all-of 的 primary 拆回单一 primary + secondary。

    S4 primary 是消费者最终结果；机制、附加卖点、处置方式不能和最终结果焊成同一个
    all-of 条件，否则任一附加卖点没拍到都会误杀核心效果。
    """
    if point.get("priority") != "primary":
        return [point]
    target = str(point.get("proof_target") or "")
    standard = str(point.get("visual_standard") or "")
    dims = [str(d).strip() for d in point.get("visual_diff_dimensions") or [] if str(d).strip()]
    related = [str(r).strip() for r in point.get("related_selling_points") or [] if str(r).strip()]
    if not _looks_like_compound_primary(target, dims, related):
        return [point]

    head_target, tail_target = _split_first_compound_piece(target)
    head_standard, tail_standard = _split_first_compound_piece(standard)
    primary = dict(point)
    primary["proof_target"] = _clean_proof_phrase(head_target) or target
    primary["visual_standard"] = _clean_proof_phrase(head_standard) or standard
    primary["visual_diff_dimensions"] = dims[:1] or dims
    primary_related = _filter_related_points(
        related,
        " ".join([primary["proof_target"], primary["visual_standard"], " ".join(primary["visual_diff_dimensions"])]),
    )
    primary["related_selling_points"] = primary_related[:4]

    secondary_dims = dims[1:]
    secondary_related = [r for r in related if r not in primary_related]
    secondary_target = _clean_proof_phrase(tail_target)
    secondary_standard = _clean_proof_phrase(tail_standard)
    secondary: dict[str, Any] | None = None
    if secondary_target or secondary_standard or secondary_dims or secondary_related:
        secondary = {
            "priority": "secondary",
            "proof_target": secondary_target or "附加视觉证明",
            "visual_standard": secondary_standard or secondary_target or "补充证明点有效呈现",
            "visual_diff_dimensions": secondary_dims[:3],
            "related_selling_points": secondary_related[:4],
        }
    return [primary, secondary] if secondary else [primary]


def _looks_like_compound_primary(target: str, dims: list[str], related: list[str]) -> bool:
    text = target.strip()
    markers = ("与", "及", "和", "同时", "+", "/", "、", "双重", "多重")
    return any(marker in text for marker in markers) or (len(dims) > 1 and len(related) > 1 and ("双重" in text or "多重" in text))


def _split_first_compound_piece(text: str) -> tuple[str, str]:
    normalized = str(text or "").strip()
    if not normalized:
        return "", ""
    separators = ("；", ";", "，", ",", "并", "且", "同时", "与", "及", "和", "+", "/", "、")
    positions = [(normalized.find(sep), sep) for sep in separators if normalized.find(sep) > 0]
    if not positions:
        return normalized, ""
    index, sep = min(positions, key=lambda item: item[0])
    return normalized[:index], normalized[index + len(sep):]


def _clean_proof_phrase(text: str) -> str:
    cleaned = str(text or "").strip(" ，,；;。")
    for suffix in ("双重验证", "多重验证", "验证", "证明"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.strip(" ，,；;。")


def _filter_related_points(related: list[str], proof_text: str) -> list[str]:
    """只保留与 primary 文字直接相交的卖点，避免拆分后仍被附加卖点污染。"""
    compact = str(proof_text or "").replace(" ", "")
    if not compact:
        return []
    tokens = {compact[i:i + 2] for i in range(max(len(compact) - 1, 0)) if compact[i:i + 2].strip()}
    matched = []
    for item in related:
        item_text = str(item or "")
        if any(token and token in item_text for token in tokens):
            matched.append(item)
    return matched


def _repair_result_primary_visual_standard(point: dict[str, Any]) -> dict[str, Any]:
    """结果型 primary 的 visual_standard 不应被机制触发词替代。"""
    if point.get("priority") != "primary":
        return point
    target = str(point.get("proof_target") or "")
    standard = str(point.get("visual_standard") or "")
    dims = [str(dim).strip() for dim in point.get("visual_diff_dimensions") or [] if str(dim).strip()]
    if not target or not standard or not dims:
        return point
    result_hints = ("效果", "清洁", "洁净", "去污", "美白", "控油", "遮瑕", "修复", "提亮", "补水", "除皱", "除毛", "定妆", "防漏", "防水")
    process_hints = ("入水", "起泡", "释放", "接触", "按压", "打开", "启动", "喷出", "涂抹")
    dimension = dims[0]
    if (
        any(hint in target for hint in result_hints)
        and any(hint in standard for hint in process_hints)
        and ("vs" in dimension.lower() or "→" in dimension or "->" in dimension)
    ):
        repaired = dict(point)
        repaired["visual_standard"] = dimension
        return repaired
    return point
