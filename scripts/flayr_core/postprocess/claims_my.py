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


def reconcile_certification_ownership(result: dict[str, Any]) -> None:
    """把第三方认证主张统一归到 S5（信任放大），并从其他阶段移除重复出现。

    认证功能是外部背书，按功能归 S5，而非按出现位置归 S2。
    """
    stages = result.get("stage_analysis", [])
    if len(stages) < 5:
        return
    cert_re = r"KKM|KKMA|认证|kelulusan"
    trust = stages[4]

    # 认证常与产品引出同框出现（落在 S2 时段），但功能是外部背书 → 按功能归 S5。
    # 在任意标杆阶段找认证 quote，作为 S5 背书内容来源（不只读 S5，否则搬不动 S2 里的认证）。
    cert_quote, cert_zh, cert_time = "", "", ""
    for stage in stages:
        candidate = str(stage.get("benchmark_quote") or "")
        if re.search(cert_re, candidate, flags=re.IGNORECASE):
            cert_quote = candidate
            cert_zh = str(stage.get("benchmark_quote_zh") or "")
            cert_time = str(stage.get("benchmark_time_range") or "")
            break
    has_cert_anywhere = bool(cert_quote) or any(
        re.search(
            cert_re,
            json.dumps({k: v for k, v in stage.items() if k.startswith("benchmark")}, ensure_ascii=False),
            flags=re.IGNORECASE,
        )
        for stage in stages
    )
    if not has_cert_anywhere:
        return

    benchmark = result.get("video_understanding", {}).get("benchmark", {})
    units = benchmark.get("evidence_units", []) if isinstance(benchmark, dict) else []
    # S5 若无有效时间，用认证出现的时间，保证 cert 单元与 S5 时间相交。
    s5_time = str(trust.get("benchmark_time_range") or "").strip()
    if cert_time and (not s5_time or set(s5_time) <= set("0.s -")):
        s5_time = cert_time
        trust["benchmark_time_range"] = s5_time
    cert_id = "B_CERT_S5"
    units[:] = [unit for unit in units if str(unit.get("id")) != cert_id]
    units.append(
        {
            "id": cert_id,
            "time_range": s5_time or cert_time,
            "information": "标杆展示第三方机构认证（KKM/Halal 等）作为信任背书。",
            "voiceover": cert_quote,
            "voiceover_zh": cert_zh,
            "visual_fact": "口播/字幕提及第三方认证背书；当前关键帧未必可核验认证标记。",
            "subtitle_fact": "",
        }
    )
    trust["benchmark_evidence_ids"] = list(
        dict.fromkeys([*[i for i in trust.get("benchmark_evidence_ids", []) if "_NO_" not in str(i)], cert_id])
    )
    if cert_quote:
        trust["benchmark_quote"] = cert_quote
        trust["benchmark_quote_zh"] = cert_zh
    trust["benchmark_key_message"] = "标杆用第三方认证建立信任背书。"
    if not str(trust.get("benchmark_summary") or "").strip() or "均未设计" in str(trust.get("benchmark_summary") or ""):
        trust["benchmark_summary"] = "标杆展示第三方认证作为信任背书。"
    trust["benchmark_visual_evidence"] = ["口播/字幕提及第三方认证背书；当前关键帧未必可核验认证标记。"]
    trust["benchmark_support_status"] = "voice_only"

    # 指向认证内容的 evidence_unit id（含刚建的 B_CERT_S5），用于从非 S5 阶段剥离引用。
    cert_unit_ids = {
        str(unit.get("id"))
        for unit in units
        if isinstance(unit, dict)
        and re.search(cert_re, json.dumps(unit, ensure_ascii=False), flags=re.IGNORECASE)
    }
    for index, stage in enumerate(stages):
        if index == 4:
            continue
        stage["benchmark_evidence_ids"] = [
            i for i in stage.get("benchmark_evidence_ids", []) if str(i) not in cert_unit_ids
        ]
        for key in (
            "benchmark_key_message",
            "benchmark_summary",
            "benchmark_quote",
            "benchmark_quote_zh",
            "gap",
        ):
            stage[key] = remove_certification_clauses(stage.get(key), key)
        stage["benchmark_visual_evidence"] = [
            item
            for item in stage.get("benchmark_visual_evidence", [])
            if not re.search(r"KKM|KKMA|认证|kelulusan", str(item), flags=re.IGNORECASE)
        ]
        stage["evidence"] = [
            item
            for item in stage.get("evidence", [])
            if not re.search(r"KKM|KKMA|认证|kelulusan", str(item), flags=re.IGNORECASE)
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
            unsupported_certification = not re.search(r"KKM|KKMA|认证|kelulusan", source_text, flags=re.IGNORECASE)
            if unsupported_certification:
                for key in (f"{role}_key_message", f"{role}_summary", f"{role}_quote", f"{role}_quote_zh"):
                    stage[key] = remove_certification_clauses(stage.get(key), key)
            removed = unsupported_certification and any(
                re.search(r"KKM|KKMA|认证|kelulusan", str(item), flags=re.IGNORECASE)
                for item in visual_values
            )
            if removed:
                stage[visual_key] = [
                    item
                    for item in visual_values
                    if not re.search(r"KKM|KKMA|认证|kelulusan", str(item), flags=re.IGNORECASE)
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
    if not re.search(r"KKM|KKMA|认证|kelulusan", text, flags=re.IGNORECASE):
        return text
    clauses = [part.strip(" 。；;") for part in re.split(r"(?<=[。；;.!?])\s*", text) if part.strip(" 。；;")]
    retained = [part for part in clauses if not re.search(r"KKM|KKMA|认证|kelulusan", part, flags=re.IGNORECASE)]
    if retained:
        return "。".join(retained) + "。"
    if key == "gap":
        return "达人在该阶段缺少与标杆同等清晰的信息传递和可验证画面支撑。"
    return "该阶段以对应口播与可见画面传递信息。"
