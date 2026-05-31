"""flayr_core.postprocess.validate：通用校验层。

⚠️ 本模块所有 validate_* 函数在校验失败时会抛 SystemExit，
   触发 pipeline 走 repair payload 重跑 LLM。调用方必须感知这个控制流副作用，
   不要把这些函数和 repair.py 里的"纯数据修补"混用。

注意：健康品类专项的 validate_recommendation_safety / validate_creator_script_language
不在本模块，它们和 sanitize_* 强耦合（共用同一组健康关键词）所以放在 health_rewrite.py。
未来如要把所有 validate_* 集中收口，需要先把品类硬编码抽出去（参见 review TODO）。

TODO: validate_stage_ownership 当前含 MY 市场 KKM/kelulusan 硬编码，
      未来若推广到其他市场应抽到 claims_xx.py 的 validate 区，本模块只保留通用校验。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm.parse import (
    is_effective_voiceover,
    normalized_transcript_text,
    read_transcript_text,
)
from .utils import evidence_overlaps_range


def validate_evidence_alignment(result: dict[str, Any]) -> None:
    """阶段顺序 + evidence_ids 引用合法性 + 认证主张需证据支撑。"""
    understanding = result.get("video_understanding", {})
    available: dict[str, set[str]] = {}
    for role in ("benchmark", "creator"):
        units = understanding.get(role, {}).get("evidence_units", [])
        available[role] = {str(unit.get("id")) for unit in units if isinstance(unit, dict) and unit.get("id")}
        if not available[role]:
            raise SystemExit(f"video_understanding.{role}.evidence_units 不能为空，必须先完成整片事实清单。")
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        expected_stage = f"S{index}"
        if not str(stage.get("stage") or "").strip().startswith(expected_stage):
            raise SystemExit(f"stage_analysis 必须按 S1 到 S6 顺序输出；第 {index} 项不是 {expected_stage}。")
        for role in ("benchmark", "creator"):
            references = stage.get(f"{role}_evidence_ids", [])
            if not references:
                raise SystemExit(f"S{index} 缺少 {role}_evidence_ids，结论无法对应证据。")
            missing = [item for item in references if item not in available[role]]
            if missing:
                raise SystemExit(f"S{index} 引用了不存在的 {role} 证据：{', '.join(missing)}。")
            units = understanding.get(role, {}).get("evidence_units", [])
            is_silent_role = not any(is_effective_voiceover(unit.get("voiceover")) for unit in units if isinstance(unit, dict))
            referenced = [unit for unit in units if str(unit.get("id")) in references]
            if not is_silent_role and not any(
                evidence_overlaps_range(unit, stage.get(f"{role}_time_range")) for unit in referenced
            ):
                raise SystemExit(f"S{index} 的 {role} 证据不在对应阶段时间内，需补充该时段事实单元。")
            stage_text = json.dumps(
                {
                    key: value
                    for key, value in stage.items()
                    if key.startswith(role)
                },
                ensure_ascii=False,
            )
            if re.search(r"KKM|KKMA|认证|kelulusan", stage_text, flags=re.IGNORECASE):
                source_text = json.dumps(referenced, ensure_ascii=False)
                if not re.search(r"KKM|KKMA|认证|kelulusan", source_text, flags=re.IGNORECASE):
                    raise SystemExit(f"S{index} 的 {role} 认证结论没有被所引用事实单元支持。")


def validate_analysis_dimensions(result: dict[str, Any]) -> None:
    """三步分析契约校验：收集 warnings 到 result['qa_warnings']，不再抛 SystemExit。

    ⚠️ 行为变化（保通流程的短期妥协）：原本会抛 SystemExit 触发 repair，
    现在改为软警告。这样即使 LLM 漏字段也能让报告出结果，由报告读者人工识别。
    后续 QA-RULES 实施时可由 R01/R02 接管严格性，届时可恢复硬校验。
    """
    warnings: list[str] = []

    if not str(result.get("one_line_verdict") or "").strip():
        warnings.append("[R01] 缺少第一步整体感知的 one_line_verdict。")
    holistic = result.get("holistic_assessment", {})
    if any(value == "未完成评估。" for value in holistic.values()):
        warnings.append("[R01] 缺少第一步整体感知的五维速评或整体转化预判。")
    visibility = result.get("product_visibility", {})
    if visibility.get("first_appearance_sec") is None or visibility.get("ratio") is None:
        warnings.append("[R04] 缺少第二步产品可见度统计。")
    if result.get("loop_closure", {}).get("note") == "未完成闭环校验。":
        warnings.append("[R18] 缺少第二步槽位间闭环校验。")
    for stage in result.get("stage_analysis", []):
        if not stage.get("gap_summary") or not str(stage.get("module_fit_reason") or "").strip():
            warnings.append(f"[R02] {stage.get('stage')} 缺少模块适配判断或分点差距。")

    if warnings:
        existing = result.get("qa_warnings", [])
        if not isinstance(existing, list):
            existing = []
        result["qa_warnings"] = existing + warnings


def validate_transcript_attribution(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """禁止 benchmark 的 evidence_unit 引用 creator 的口播原文，反之亦然。"""
    transcript_text = {
        role: normalized_transcript_text(read_transcript_text(info))
        for role, info in analysis.get("videos", {}).items()
        if role in {"benchmark", "creator"}
    }
    if not transcript_text.get("benchmark") or not transcript_text.get("creator"):
        return
    for role, other_role in (("benchmark", "creator"), ("creator", "benchmark")):
        units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
        for unit in units:
            quote = normalized_transcript_text(str(unit.get("voiceover") or "")) if isinstance(unit, dict) else ""
            if len(quote) < 12 or quote in transcript_text[role]:
                continue
            if quote in transcript_text[other_role]:
                raise SystemExit(
                    f"{role} 证据 {unit.get('id')} 的口播实际来自 {other_role} 转写，禁止跨视频串证据。"
                )


def validate_stage_ownership(result: dict[str, Any]) -> None:
    """KKM/认证只能归 S2 或独立 S5；S1 Hook 不得携带认证；不得跨阶段重复。

    TODO: 本函数含 MY 市场 KKM/kelulusan 硬编码，未来扩市场时应抽到 claims_xx.py 的 validate 区。
    """
    stages = result.get("stage_analysis", [])
    if not stages:
        return
    hook_text = json.dumps(stages[0], ensure_ascii=False)
    if re.search(r"KKM|KKMA|认证|kelulusan", hook_text, flags=re.IGNORECASE):
        raise SystemExit("S1 Hook 不得承载 KKM/认证信息；将与产品引出同段出现的认证口播只归入 S2，并标明画面是否验证。")
    certification_stages = [
        str(stage.get("stage") or f"S{index}")
        for index, stage in enumerate(stages, start=1)
        if re.search(
            r"KKM|KKMA|认证|kelulusan",
            json.dumps(
                {
                    key: value
                    for key, value in stage.items()
                    if key.startswith("benchmark")
                },
                ensure_ascii=False,
            ),
            flags=re.IGNORECASE,
        )
    ]
    if len(certification_stages) > 1:
        raise SystemExit(
            "标杆认证信息不得重复归入多个阶段；请选择其主要作用阶段一次呈现。"
            f"当前重复阶段：{', '.join(certification_stages)}。"
        )
    if certification_stages and not (
        certification_stages[0].startswith("S2") or certification_stages[0].startswith("S5")
    ):
        raise SystemExit("认证信息只能归入 S2 产品引出或独立的 S5 信任放大，不得归入其他阶段。")
