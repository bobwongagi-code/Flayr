"""LLM 分析结果的边界校验。

稳定字段、运行时投影和结果生命周期由 ``flayr_core.analysis_model`` 统一
声明；本模块只负责把外部数据校验到该领域模型可以消费的最小形状。
"""

from __future__ import annotations

from typing import Any

from ..analysis_model import ANALYSIS_RESULT_CONTRACT, AnalysisResult
from ..stage_catalog import DEFAULT_STAGES


class AnalysisContractError(ValueError):
    """LLM 结果不满足程序处理所需的最小结构。"""


RESULT_STAGE_COUNT = ANALYSIS_RESULT_CONTRACT.stage_count
IMPROVEMENT_COUNT_RANGE = (1, 5)
NORMALIZED_TOP_LEVEL_FIELDS = ANALYSIS_RESULT_CONTRACT.normalized_required_fields


def validate_raw_analysis_envelope(result: Any) -> dict[str, Any]:
    """校验归一化前必须存在的外壳，返回已收窄类型的原始结果。"""
    if not isinstance(result, dict):
        raise AnalysisContractError("analysis_result must be a JSON object.")

    stage_analysis = result.get("stage_analysis")
    if not isinstance(stage_analysis, list) or len(stage_analysis) != RESULT_STAGE_COUNT:
        raise AnalysisContractError(f"analysis_result must contain stage_analysis with {RESULT_STAGE_COUNT} items.")

    improvements = result.get("improvements")
    minimum, maximum = IMPROVEMENT_COUNT_RANGE
    if not isinstance(improvements, list) or not minimum <= len(improvements) <= maximum:
        raise AnalysisContractError(f"analysis_result must contain {minimum} to {maximum} improvements.")
    return result


def validate_normalized_analysis_contract(result: dict[str, Any]) -> None:
    """校验归一化后的公共骨架，避免后处理链在畸形结果上继续运行。"""
    model = AnalysisResult.from_mapping(result)
    missing = model.missing_normalized_fields()
    if missing:
        raise AnalysisContractError(f"normalized analysis_result missing fields: {', '.join(missing)}.")

    stage_analysis = model.stages()
    if not isinstance(stage_analysis, list) or len(stage_analysis) != RESULT_STAGE_COUNT:
        raise AnalysisContractError(f"normalized analysis_result must contain {RESULT_STAGE_COUNT} stages.")

    for definition, stage in zip(DEFAULT_STAGES, stage_analysis, strict=True):
        if not isinstance(stage, dict):
            raise AnalysisContractError(f"{definition.code} stage must be an object.")
        if not str(stage.get("stage") or "").strip().startswith(definition.code):
            raise AnalysisContractError(f"stage_analysis order must match S1-S6; expected {definition.code}.")

    improvements = result["improvements"]
    minimum, maximum = IMPROVEMENT_COUNT_RANGE
    if not isinstance(improvements, list) or not minimum <= len(improvements) <= maximum:
        raise AnalysisContractError(f"normalized analysis_result must contain {minimum} to {maximum} improvements.")
