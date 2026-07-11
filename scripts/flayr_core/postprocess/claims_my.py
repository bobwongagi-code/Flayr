"""flayr_core.postprocess.claims_my：马来西亚（MY）市场认证主张专项。

⚠️ 仅适用于 MY 市场。本模块所有函数都围绕 KKM / KKMA / kelulusan / "认证" 这些
   马来西亚卫生部审批与一般认证关键词处理：
     - 把第三方认证主张统一归到 S5 信任放大阶段（认证功能是外部背书，按功能归 S5，
       而非按出现位置归 S2；自述功效不算认证）
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


def _is_empty_time_range(text: str) -> bool:
    """时间区间是否为空/全零（如 ""、"0.0s - 0.0s"）；用于判断 S5 是否需借用认证时间。"""
    numbers = re.findall(r"\d+(?:\.\d+)?", str(text or ""))
    return not numbers or all(float(number) == 0 for number in numbers)


def reconcile_certification_ownership(result: dict[str, Any]) -> None:
    """把双方的第三方认证主张统一归到 S5，并从其他阶段移除重复出现。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 5:
        return
    trust = stages[4]
    for role in ("benchmark", "creator"):
        _reconcile_role_certification(result, stages, trust, role)


def _reconcile_role_certification(
    result: dict[str, Any],
    stages: list[dict[str, Any]],
    trust: dict[str, Any],
    role: str,
) -> None:
    """把一侧认证证据迁移为 S5 唯一归属，另一侧使用同一逻辑。"""
    role_label = "标杆" if role == "benchmark" else "达人"
    quote_key = f"{role}_quote"
    quote_zh_key = f"{role}_quote_zh"
    time_key = f"{role}_time_range"
    evidence_key = f"{role}_evidence_ids"
    visual_key = f"{role}_visual_evidence"

    # 认证常与产品引出同框出现，但功能是外部背书。扫描全阶段，不能只读原 S5。
    cert_quote, cert_zh, cert_time = "", "", ""
    for stage in stages:
        candidate = str(stage.get(quote_key) or "")
        if contains_certification(candidate):
            cert_quote = candidate
            cert_zh = str(stage.get(quote_zh_key) or "")
            cert_time = str(stage.get(time_key) or "")
            break
    has_cert_anywhere = bool(cert_quote) or any(
        contains_certification(json.dumps({k: v for k, v in stage.items() if k.startswith(role)}, ensure_ascii=False))
        for stage in stages
    )
    if not has_cert_anywhere:
        return

    understanding = result.get("video_understanding", {}).get(role, {})
    units = understanding.get("evidence_units", []) if isinstance(understanding, dict) else []
    if not isinstance(units, list):
        return
    cert_visual = "口播/字幕提及第三方认证背书；当前关键帧未必可核验认证标记。"
    # S5 若无有效时间，用认证出现的时间，保证 cert 单元与 S5 时间相交。
    s5_time = str(trust.get(time_key) or "").strip()
    if cert_time and _is_empty_time_range(s5_time):
        s5_time = cert_time
        trust[time_key] = s5_time
    cert_id = f"{role[0].upper()}_CERT_S5"
    units[:] = [unit for unit in units if str(unit.get("id")) != cert_id]
    units.append(
        {
            "id": cert_id,
            "time_range": s5_time or cert_time,
            "information": f"{role_label}展示第三方机构认证（KKM/Halal 等）作为信任背书。",
            "voiceover": cert_quote,
            "voiceover_zh": cert_zh,
            "visual_fact": cert_visual,
            "subtitle_fact": "",
        }
    )
    # evidence_id 始终追加 cert_id（安全）。
    trust[evidence_key] = list(
        dict.fromkeys([*[i for i in trust.get(evidence_key, []) if "_NO_" not in str(i)], cert_id])
    )
    # 若 S5 已有独立（非认证、非占位）背书内容，认证并入为附加背书，不覆写原内容；否则用认证填充 S5。
    summary_key = f"{role}_summary"
    message_key = f"{role}_key_message"
    support_key = f"{role}_support_status"
    existing_summary = str(trust.get(summary_key) or "").strip()
    existing_quote = str(trust.get(quote_key) or "").strip()
    has_independent_s5 = (
        bool(existing_summary) and "均未设计" not in existing_summary and not contains_certification(existing_summary)
    ) or (bool(existing_quote) and not contains_certification(existing_quote))
    if has_independent_s5:
        if "认证" not in existing_summary and "背书" not in existing_summary:
            trust[summary_key] = (existing_summary + "；并展示第三方认证作为附加背书。").strip("；")
        trust[visual_key] = list(
            dict.fromkeys([*[str(v) for v in trust.get(visual_key, []) if str(v).strip()], cert_visual])
        )
    else:
        if cert_quote:
            trust[quote_key] = cert_quote
            trust[quote_zh_key] = cert_zh
        trust[message_key] = f"{role_label}用第三方认证建立信任背书。"
        if not existing_summary or "均未设计" in existing_summary:
            trust[summary_key] = f"{role_label}展示第三方认证作为信任背书。"
        trust[visual_key] = [cert_visual]
        trust[support_key] = "voice_only"

    # 指向认证内容的 evidence_unit id（含刚建的 B_CERT_S5），用于从非 S5 阶段剥离引用。
    cert_unit_ids = {
        str(unit.get("id"))
        for unit in units
        if isinstance(unit, dict)
        and contains_certification(json.dumps(unit, ensure_ascii=False))
    }
    for index, stage in enumerate(stages):
        if index == 4:
            continue
        stage[evidence_key] = [
            i for i in stage.get(evidence_key, []) if str(i) not in cert_unit_ids
        ]
        for key in (
            message_key,
            summary_key,
            quote_key,
            quote_zh_key,
            "gap",
        ):
            stage[key] = remove_certification_clauses(stage.get(key), key)
        for key, value in list(stage.items()):
            if not key.startswith(role) or not isinstance(value, dict):
                continue
            cleaned = dict(value)
            for nested_key, nested_value in cleaned.items():
                if nested_key == "evidence_ids" and isinstance(nested_value, list):
                    cleaned[nested_key] = [item for item in nested_value if str(item) not in cert_unit_ids]
                elif isinstance(nested_value, str):
                    cleaned[nested_key] = remove_certification_clauses(nested_value, nested_key)
                elif isinstance(nested_value, list):
                    cleaned[nested_key] = [item for item in nested_value if not contains_certification(item)]
            stage[key] = cleaned
        stage[visual_key] = [
            item
            for item in stage.get(visual_key, [])
            if not contains_certification(item)
        ]
        stage["evidence"] = [
            item
            for item in stage.get("evidence", [])
            if not contains_certification(item)
        ]


def discard_unreferenced_certification_claims(result: dict[str, Any]) -> None:
    """阶段文本提到认证或评论但其引用的 evidence_unit 未承载该信息时，删除该主张。"""
    understanding = result.get("video_understanding", {})
    proof_pattern = r"KKM|KKMA|认证|kelulusan|证书|检测报告|用户评论|用户评价|用户反馈|晒单|用户证言|testimoni|testimonial"
    for stage in result.get("stage_analysis", []):
        stage_source_parts = []
        for role in ("benchmark", "creator"):
            units = understanding.get(role, {}).get("evidence_units", [])
            references = {str(value) for value in stage.get(f"{role}_evidence_ids", [])}
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
    retained = [part for part in clauses if not CERTIFICATION_PATTERN.search(part)]
    if retained:
        return "。".join(retained) + "。"
    if key == "gap":
        return "达人在该阶段缺少与标杆同等清晰的信息传递和可验证画面支撑。"
    return "该阶段以对应口播与可见画面传递信息。"
