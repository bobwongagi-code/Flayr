"""Shared semantic boundary between model results and report views.

The LLM result and the runtime ``analysis`` object are not report payloads.
This module selects the small, view-neutral semantic surface that reports may
consume.  Report-specific names such as ``planA``, ``statusLabel`` and
``gmvClass`` must be created by the individual view projection instead of
being added to the analysis result.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .analysis_model import ANALYSIS_RESULT_CONTRACT, SEMANTIC_MODEL_VERSION


_STAGE_CODE_RE = re.compile(r"\b(S[1-6])\b", re.IGNORECASE)

# Runtime fields needed to interpret the normalized result.  They are context,
# not model-generated report fields.
_RUNTIME_SEMANTIC_FIELDS = (
    "generated_at",
    "mode",
    "analysis_run_state",
    "analysis_scope",
    "product",
    "videos",
    "degraded_flags",
    "low_confidence_stages",
    "improvements_status",
)

# These fields existed in older callers but are not part of the normalized
# model contract.  Keep them in an explicit compatibility context so they
# cannot silently become shared analysis semantics.
_CREATOR_CONTEXT_FIELDS = (
    "candidate_experiments",
    "highlights",
    "creator_highlights",
    "continuity_record",
    "retained_points",
    "creator_retain",
    "content_intent",
    "creator_content_intent",
)

# Nested model fields are allowlisted as well.  This keeps a future view field
# from hiding inside a stage or improvement and bypassing the top-level gate.
_SEMANTIC_STAGE_FIELDS = (
    "stage",
    "time_range",
    "benchmark_time_range",
    "creator_time_range",
    "core_question",
    "affected_by_global_issues",
    "severity",
    "gap_type",
    "gap_summary",
    "comparison_reason",
    "gap",
    "comparison_status",
    "creator_summary",
    "creator_key_message",
    "creator_visual_evidence",
    "creator_evidence_ids",
    "creator_quote",
    "creator_quote_zh",
    "creator_observation",
    "creator_multimodal",
    "creator_execution",
    "creator_absolute_status",
    "benchmark_summary",
    "benchmark_key_message",
    "benchmark_visual_evidence",
    "benchmark_evidence_ids",
    "benchmark_quote",
    "benchmark_quote_zh",
    "benchmark_multimodal",
    "benchmark_execution",
    "benchmark_absolute_status",
    "communication_strategy",
    "communication_advice",
    "talking_point",
    "communication",
    "_communication",
    "reference_relevance",
    "reference_observation",
    "benchmark_reference",
    "linked_experiment_id",
    "insufficient_evidence",
    "confidence_note",
    "confidence_notes",
    "evidence_confidence",
    "retain",
    "creator_retain",
    "creator_retention",
)

_SEMANTIC_IMPROVEMENT_FIELDS = (
    "title",
    "name",
    "target_stage",
    "stage",
    "stage_code",
    "time_range",
    "creator_time_range",
    "benchmark_time_range",
    "gap_type",
    "priority",
    "problem",
    "observation",
    "benchmark_reference",
    "benchmark_evidence_ids",
    "creator_evidence_ids",
    "evidence",
    "gmv_impact",
    "gmv_reason",
    "suggestion",
    "actions",
    "expected_effect",
    "creator_script",
    "creator_script_zh",
    "experiment_id",
    "hypothesis",
    "demonstration",
    "verification",
    "check",
    "why",
    "confidence",
    "status",
)

_CREATOR_EXPERIMENT_FIELDS = (
    "experiment_id",
    "title",
    "name",
    "target_stage",
    "stage",
    "stage_code",
    "observation",
    "problem",
    "action",
    "suggestion",
    "actions",
    "evidence",
    "creator_evidence_ids",
    "benchmark_evidence_ids",
    "hypothesis",
    "expected_effect",
    "demonstration",
    "creator_script",
    "creator_script_zh",
    "verification",
    "check",
    "why",
    "confidence",
    "status",
)


def _copy_mapping(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _copy_allowlisted(source: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: copy.deepcopy(source[field]) for field in fields if field in source}


def _copy_creator_context(source: Mapping[str, Any]) -> dict[str, Any]:
    context = _copy_allowlisted(source, _CREATOR_CONTEXT_FIELDS)
    candidates = context.get("candidate_experiments")
    if isinstance(candidates, list):
        context["candidate_experiments"] = [
            _copy_allowlisted(item, _CREATOR_EXPERIMENT_FIELDS)
            for item in candidates
            if isinstance(item, dict)
        ]
    return context


def _stage_code(value: Any, index: int) -> str:
    match = _STAGE_CODE_RE.search(str(value or ""))
    return match.group(1).upper() if match else f"S{index}"


@dataclass(frozen=True)
class SemanticStage:
    """A view-neutral stage assessment and its evidence references."""

    index: int
    code: str
    data: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass(frozen=True)
class SemanticImprovement:
    """A view-neutral improvement opportunity."""

    index: int
    data: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass(frozen=True)
class SemanticAnalysis:
    """Immutable-in-practice semantic snapshot consumed by report views."""

    data: dict[str, Any]
    product: dict[str, Any]
    videos: dict[str, Any]
    analysis_scope: dict[str, Any]
    video_understanding: dict[str, Any]
    creator_context: dict[str, Any]
    stages: tuple[SemanticStage, ...]
    improvements: tuple[SemanticImprovement, ...]

    @classmethod
    def from_mapping(cls, analysis: Mapping[str, Any]) -> "SemanticAnalysis":
        """Build a bounded semantic snapshot from runtime analysis data."""
        if not isinstance(analysis, Mapping):
            raise TypeError("analysis must be a mapping")

        # Only normalized contract fields cross from model output into the
        # semantic layer.  Arbitrary report-shaped keys are intentionally lost.
        shared = _copy_allowlisted(analysis, ANALYSIS_RESULT_CONTRACT.projection_fields)
        shared.update(_copy_allowlisted(analysis, _RUNTIME_SEMANTIC_FIELDS))

        product = _copy_mapping(shared.get("product"))
        videos = _copy_mapping(shared.get("videos"))
        analysis_scope = _copy_mapping(shared.get("analysis_scope"))
        video_understanding = _copy_mapping(shared.get("video_understanding"))
        creator_context = _copy_creator_context(analysis)

        raw_stages = shared.get("stage_analysis")
        stages = tuple(
            SemanticStage(
                index=index,
                code=_stage_code(stage.get("stage"), index),
                data=_copy_allowlisted(stage, _SEMANTIC_STAGE_FIELDS),
            )
            for index, stage in enumerate(raw_stages or [], start=1)
            if isinstance(stage, dict)
        )
        raw_improvements = shared.get("improvements")
        improvements = tuple(
            SemanticImprovement(index=index, data=_copy_allowlisted(item, _SEMANTIC_IMPROVEMENT_FIELDS))
            for index, item in enumerate(raw_improvements or [], start=1)
            if isinstance(item, dict)
        )

        return cls(
            data=shared,
            product=product,
            videos=videos,
            analysis_scope=analysis_scope,
            video_understanding=video_understanding,
            creator_context=creator_context,
            stages=stages,
            improvements=improvements,
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def side(self, role: str) -> dict[str, Any]:
        value = self.video_understanding.get(role)
        return value if isinstance(value, dict) else {}

    def metadata(self) -> dict[str, Any]:
        """Describe the boundary without serializing report-specific values."""
        return {
            "version": SEMANTIC_MODEL_VERSION,
            "source_contract_version": ANALYSIS_RESULT_CONTRACT.version,
            "shared_fields": sorted(self.data),
            "creator_context_fields": sorted(self.creator_context),
            "view_contracts": ["bd_internal", "creator"],
        }
