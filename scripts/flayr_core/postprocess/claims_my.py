"""flayr_core.postprocess.claims_my：马来西亚（MY）市场认证主张专项。

⚠️ 仅适用于 MY 市场。本模块所有函数都围绕 KKM / KKMA / kelulusan / "认证" 这些
   马来西亚卫生部审批与一般认证关键词处理：
     - S5 负责第三方认证的信任资格；同一已资格化事实可按实际功能被其他阶段引用
     - 删除阶段文本中未被 evidence_unit 支持的认证 / 评论 / 证书表述
     - 对应文案降级或替换为中性表达

未来若要新增其他市场（如 TH / ID / VN）的认证规则，请新增 claims_th.py / claims_id.py 等
平级文件，不要修改本模块；保持每个市场一个文件、规则一目了然。

依赖：仅依赖外部 (json + re)，与 postprocess 包内其他模块完全解耦。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..stage_ownership import CERTIFICATION_PATTERN, contains_certification
from ..stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    qualified_stage_evidence_ids,
    stage_evidence_readiness,
)


def reconcile_certification_ownership(result: dict[str, Any]) -> None:
    """Compatibility hook; stage ownership no longer migrates or deletes facts."""
    del result


def discard_unreferenced_certification_claims(result: dict[str, Any]) -> None:
    """阶段文本提到认证或评论但其引用的 evidence_unit 未承载该信息时，删除该主张。"""
    understanding = result.get("video_understanding", {})
    proof_pattern = (
        rf"(?:{CERTIFICATION_PATTERN.pattern}|用户评论|用户评价|用户反馈|晒单|用户证言|"
        r"testimoni|testimonial)"
    )
    for stage_index, stage in enumerate(result.get("stage_analysis", []), start=1):
        stage_source_parts = []
        stage_evidence_blocked = False
        for role in ("benchmark", "creator"):
            units = understanding.get(role, {}).get("evidence_units", [])
            references = {str(value) for value in stage.get(f"{role}_evidence_ids", [])}
            side = understanding.get(role, {}) if isinstance(understanding, dict) else {}
            if side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
                stage_code = f"S{stage_index}"
                # Unknown/conflict means the observation is not available for
                # this repair.  Do not turn an uncollected stage into an
                # unsupported-claim/explicit-absence conclusion.
                if stage_evidence_readiness(side, stage_code) not in {"present", "absent"}:
                    stage_evidence_blocked = True
                    continue
                references &= qualified_stage_evidence_ids(side, stage_code)
            referenced = [unit for unit in units if isinstance(unit, dict) and str(unit.get("id")) in references]
            source_text = json.dumps(referenced, ensure_ascii=False)
            stage_source_parts.append(source_text)
            visual_key = f"{role}_visual_evidence"
            visual_values = [str(item) for item in stage.get(visual_key, [])]
            unsupported_certification = not contains_certification(source_text)
            if unsupported_certification:
                for key in (f"{role}_key_message", f"{role}_summary", f"{role}_quote", f"{role}_quote_zh"):
                    stage[key] = remove_certification_clauses(stage.get(key), key)
            removed = unsupported_certification and any(
                contains_certification(item)
                for item in visual_values
            )
            if removed:
                stage[visual_key] = [
                    item
                    for item in visual_values
                    if not contains_certification(item)
                ]
            unsupported_proof_visual = (
                any(re.search(proof_pattern, item, flags=re.IGNORECASE) for item in visual_values)
                and not re.search(proof_pattern, source_text, flags=re.IGNORECASE)
            )
            if unsupported_proof_visual and referenced:
                fact = str(referenced[0].get("information") or "").strip()
                stage[f"{role}_key_message"] = fact
                stage[f"{role}_summary"] = f"{fact}；当前引用证据未验证额外信任背书。"
                removed = True
            if removed:
                stage[visual_key].append("当前引用画面未验证额外背书信息。")
        if stage_evidence_blocked:
            continue
        if not re.search(proof_pattern, "\n".join(stage_source_parts), flags=re.IGNORECASE):
            scrub_unreferenced_proof_language(stage, proof_pattern)


def scrub_unreferenced_proof_language(stage: dict[str, Any], proof_pattern: str) -> None:
    """阶段没有引用任何认证/评论事实，但文本里依然出现这些字眼时，整体替换为中性表述。"""
    neutral = "该阶段没有形成可独立核验的信任承接，当前结论仅依据画面和口播。"
    for key in ("module_fit_reason", "gap", "creator_summary", "creator_key_message", "benchmark_summary", "benchmark_key_message"):
        value = str(stage.get(key) or "")
        if re.search(proof_pattern, value, flags=re.IGNORECASE):
            stage[key] = neutral
    for key in ("gap_summary", "evidence"):
        values = stage.get(key)
        if not isinstance(values, list):
            continue
        retained = [
            item
            for item in values
            if not re.search(proof_pattern, str(item), flags=re.IGNORECASE)
        ]
        if len(retained) != len(values):
            stage[key] = retained or ["当前引用证据未验证额外背书信息。"]


def remove_certification_clauses(value: Any, key: str) -> str:
    text = str(value or "").strip()
    if not contains_certification(text):
        return text
    clauses = [part.strip(" 。；;") for part in re.split(r"(?<=[。；;.!?])\s*", text) if part.strip(" 。；;")]
    retained = [part for part in clauses if not contains_certification(part)]
    if retained:
        return "。".join(retained) + "。"
    if key == "gap":
        return "达人在该阶段缺少与标杆同等清晰的信息传递和可验证画面支撑。"
    return "该阶段以对应口播与可见画面传递信息。"
