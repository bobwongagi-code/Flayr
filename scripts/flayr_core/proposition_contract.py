"""产品命题注册表与 S1-S6 阶段合同。

合同只把 Step-0 地基和人工钩子命题变成稳定、可引用的 ID，不参与 severity。
模型和 postprocess 共同消费同一份合同，避免各阶段重新解释一套产品卖点。
"""

from __future__ import annotations

import re
from typing import Any


CONTRACT_VERSION = "1.0"


def _as_texts(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    out: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _text_key(value: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def build_product_proposition_contract(
    foundation: dict[str, Any] | None,
    brand_proposition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从产品地基生成稳定命题 ID、阶段可引用范围和证明关系。"""
    foundation = foundation if isinstance(foundation, dict) else {}
    brand = brand_proposition if isinstance(brand_proposition, dict) else {}
    profile = foundation.get("product_profile") if isinstance(foundation.get("product_profile"), dict) else {}
    category = foundation.get("category_profile") if isinstance(foundation.get("category_profile"), dict) else {}

    propositions: list[dict[str, Any]] = []
    trust_evidence: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    claim_index: dict[tuple[str, str], dict[str, Any]] = {}

    def add_claim(kind: str, text: Any, source: str, **metadata: Any) -> str:
        clean = str(text or "").strip()
        key = (kind, _text_key(clean))
        if not clean or not key[1]:
            return ""
        existing = claim_index.get(key)
        if existing is not None:
            for name, value in metadata.items():
                if value not in (None, "", []):
                    existing[name] = value
            return str(existing["id"])
        counters[kind] = counters.get(kind, 0) + 1
        item = {
            "id": f"{kind}.{counters[kind]}",
            "kind": kind,
            "text": clean,
            "source": source,
            **{name: value for name, value in metadata.items() if value not in (None, "", [])},
        }
        propositions.append(item)
        claim_index[key] = item
        return str(item["id"])

    hook_values = _as_texts(brand.get("propositions"))
    hook_source = "brand_proposition.propositions"
    if not hook_values:
        hook_values = _as_texts(profile.get("hook_proposition")) + _as_texts(profile.get("physical_task"))
        hook_source = "product_profile"
    for text in hook_values:
        add_claim("hook", text, hook_source)

    pain_values = _as_texts(brand.get("painpoints"))
    pain_source = "brand_proposition.painpoints"
    if not pain_values:
        pain_values = _as_texts(category.get("painpoints"))
        pain_source = "category_profile.painpoints"
    for text in pain_values:
        add_claim("pain", text, pain_source)

    add_claim("role", profile.get("physical_task"), "product_profile.physical_task")
    for text in _as_texts(profile.get("core_selling_points"), limit=8):
        add_claim("selling", text, "product_profile.core_selling_points")

    proof_plan = profile.get("short_video_proof_plan") if isinstance(profile.get("short_video_proof_plan"), dict) else {}
    selected_candidate_id = str(proof_plan.get("s4_anchor_candidate_id") or "").strip()
    selected_selling_id = ""
    for candidate in proof_plan.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        claim_id = add_claim(
            "selling",
            candidate.get("selling_point"),
            "product_profile.short_video_proof_plan",
            delivery_stage=str(candidate.get("delivery_stage") or "").strip().upper(),
            candidate_id=str(candidate.get("id") or "").strip(),
        )
        if str(candidate.get("id") or "").strip() == selected_candidate_id:
            selected_selling_id = claim_id

    selling_claims = [item for item in propositions if item["kind"] == "selling"]

    def related_selling_ids(values: Any) -> list[str]:
        related: list[str] = []
        for value in _as_texts(values, limit=8):
            value_key = _text_key(value)
            for item in selling_claims:
                item_key = _text_key(item["text"])
                if value_key and item_key and (value_key in item_key or item_key in value_key):
                    if item["id"] not in related:
                        related.append(str(item["id"]))
        return related

    proof_points = profile.get("visual_proof_points") if isinstance(profile.get("visual_proof_points"), list) else []
    for point in proof_points:
        if not isinstance(point, dict):
            continue
        related = related_selling_ids(point.get("related_selling_points"))
        if point.get("priority") == "primary" and selected_selling_id and selected_selling_id not in related:
            related.append(selected_selling_id)
        add_claim(
            "proof",
            point.get("proof_target") or point.get("visual_standard"),
            "product_profile.visual_proof_points",
            priority=str(point.get("priority") or "secondary"),
            related_ids=related,
        )
    proof_contract_present = isinstance(profile.get("proof_contract"), dict)
    proof_plan_present = isinstance(profile.get("short_video_proof_plan"), dict)
    if (
        not any(item["kind"] == "proof" for item in propositions)
        and not proof_contract_present
        and not proof_plan_present
    ):
        add_claim(
            "proof",
            profile.get("core_visual_proposition"),
            "product_profile.core_visual_proposition",
            priority="fallback",
            related_ids=[selected_selling_id] if selected_selling_id else [],
        )

    for index, text in enumerate(_as_texts(profile.get("trust_multipliers"), limit=8), start=1):
        trust_evidence.append(
            {
                "id": f"trust.{index}",
                "text": text,
                "source": "product_profile.trust_multipliers",
            }
        )

    ids_by_kind = {
        kind: [str(item["id"]) for item in propositions if item["kind"] == kind]
        for kind in ("hook", "pain", "role", "selling", "proof")
    }
    all_claim_ids = [str(item["id"]) for item in propositions]
    s4_selling_ids = [
        str(item["id"])
        for item in propositions
        if item["kind"] == "selling" and item.get("delivery_stage") == "S4"
    ]

    stages = {
        "S1": {
            "relation": "用留人机制锚定本品命题或痛点",
            "allowed_ids": [*ids_by_kind["hook"], *ids_by_kind["pain"]],
        },
        "S2": {
            "relation": "承接 S1，并引用后续将被演示/证明的卖点，让产品成为答案；引用不等于在 S2 完成证明",
            "allowed_ids": [
                *ids_by_kind["hook"],
                *ids_by_kind["pain"],
                *ids_by_kind["role"],
                *ids_by_kind["selling"],
            ],
        },
        "S3": {
            "relation": "在真实使用动作中演示本品核心卖点",
            "allowed_ids": ids_by_kind["selling"],
        },
        "S4": {
            "relation": "呈现并归因本品选定效果或证明信号",
            "allowed_ids": list(dict.fromkeys([*ids_by_kind["proof"], *s4_selling_ids])),
        },
        "S5": {
            "relation": "用独立信任材料支持具体产品主张；信任材料本身不是产品命题",
            "allowed_ids": all_claim_ids,
            "trust_evidence_ids": [item["id"] for item in trust_evidence],
        },
        "S6": {
            "relation": "在结尾 CTA 中召回前文已建立的产品价值",
            "allowed_ids": all_claim_ids,
        },
    }
    return {
        "version": CONTRACT_VERSION,
        "propositions": propositions,
        "trust_evidence": trust_evidence,
        "stages": stages,
    }


def stage_allowed_ids(contract: dict[str, Any] | None, stage_id: str) -> set[str]:
    """返回某阶段可引用的命题 ID；非法或缺失合同返回空集合。"""
    if not isinstance(contract, dict):
        return set()
    stages = contract.get("stages") if isinstance(contract.get("stages"), dict) else {}
    stage = stages.get(stage_id) if isinstance(stages.get(stage_id), dict) else {}
    return {str(item) for item in stage.get("allowed_ids") or [] if str(item).strip()}
