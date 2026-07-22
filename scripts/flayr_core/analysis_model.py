"""Versioned domain model for the analysis result.

The JSON file under ``references/`` remains the model-facing schema.  This
module is the application-facing boundary around that schema: it owns the
stable field groups, the runtime projection into ``analysis`` and the
artifact lifecycle metadata.  Renderers and pipeline code should not invent
another list of result fields.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from .stage_catalog import DEFAULT_STAGES, StageDefinition


ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_SCHEMA_PATH = ROOT / "references" / "analysis-output-schema.json"

# Increment when the shape or ownership of the runtime result changes.  This
# is independent from the provider/model name and is written to provenance.
RESULT_CONTRACT_VERSION = 1
POSTPROCESS_LIFECYCLE_VERSION = 1


@dataclass(frozen=True)
class ResultLifecyclePhase:
    """A durable result boundary with an explicit input/output artifact."""

    name: str
    version: int
    input_artifact: str
    output_artifact: str


RESULT_LIFECYCLE = (
    ResultLifecyclePhase("raw_model_response", 1, "provider_response", "raw_model_response.json"),
    ResultLifecyclePhase(
        "validated_normalized_result",
        1,
        "raw_model_response.json",
        "validated_normalized_result.json",
    ),
    ResultLifecyclePhase(
        "final_derived_result",
        POSTPROCESS_LIFECYCLE_VERSION,
        "validated_normalized_result.json",
        "final_derived_result.json",
    ),
)


# These fields are the minimum shape that postprocess and report code consume.
# They are checked against the model-facing schema at import time by the
# contract tests, so a schema edit cannot silently leave this list stale.
NORMALIZED_REQUIRED_FIELDS = (
    "one_line_summary",
    "executive_summary",
    "holistic_assessment",
    "product_visibility",
    "loop_closure",
    "video_understanding",
    "stage_analysis",
    "improvements",
)


# This is the only approved normalized-result -> runtime-analysis projection.
# Runtime-only fields such as videos and resource budgets are assembled by the
# CLI and are deliberately outside this list.
ANALYSIS_PROJECTION_FIELDS = (
    "one_line_verdict",
    "one_line_summary",
    "executive_summary",
    "holistic_assessment",
    "key_conclusions",
    "comparison_contract",
    "comparison_eligibility",
    "product_visibility",
    "loop_closure",
    "video_understanding",
    "stage_analysis",
    "improvements",
    "category_profile",
    "product_profile",
    "s3_s4_relationship",
    "promise_chain",
    "product_proposition_contract",
    "cross_stage_state",
    "proposition_trace",
    "absolute_quality",
    "absolute_execution_shadow",
    "computed_loop_closure",
    "qa_warnings",
    "quality_audit",
    "improvement_reconciliation",
    "s4_visual_verifier",
    "global_diagnosis",
    "commercial_priorities",
    "commercial_priority_summary",
    "postprocess_provenance",
)


def _load_schema_fields() -> tuple[str, ...]:
    try:
        raw = json.loads(ANALYSIS_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load analysis result schema: {ANALYSIS_SCHEMA_PATH}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"Analysis result schema must be a JSON object: {ANALYSIS_SCHEMA_PATH}")
    return tuple(str(key) for key in raw)


@lru_cache(maxsize=1)
def schema_fields() -> tuple[str, ...]:
    """Return top-level fields declared by the model-facing schema."""
    return _load_schema_fields()


def schema_sha256() -> str:
    """Return the schema digest used in run provenance and cache audits."""
    return hashlib.sha256(ANALYSIS_SCHEMA_PATH.read_bytes()).hexdigest()


@dataclass(frozen=True)
class AnalysisResultContract:
    """Stable application contract shared by parser, pipeline and report."""

    version: int
    schema_path: str
    schema_fields: tuple[str, ...]
    normalized_required_fields: tuple[str, ...]
    projection_fields: tuple[str, ...]
    lifecycle: tuple[ResultLifecyclePhase, ...]

    @property
    def stage_count(self) -> int:
        return len(DEFAULT_STAGES)

    def metadata(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "schema_path": "references/analysis-output-schema.json",
            "schema_sha256": schema_sha256(),
            "normalized_required_fields": list(self.normalized_required_fields),
            "projection_fields": list(self.projection_fields),
            "lifecycle": [
                {
                    "name": phase.name,
                    "version": phase.version,
                    "input_artifact": phase.input_artifact,
                    "output_artifact": phase.output_artifact,
                }
                for phase in self.lifecycle
            ],
        }


ANALYSIS_RESULT_CONTRACT = AnalysisResultContract(
    version=RESULT_CONTRACT_VERSION,
    schema_path=str(ANALYSIS_SCHEMA_PATH),
    schema_fields=schema_fields(),
    normalized_required_fields=NORMALIZED_REQUIRED_FIELDS,
    projection_fields=ANALYSIS_PROJECTION_FIELDS,
    lifecycle=RESULT_LIFECYCLE,
)


@dataclass(frozen=True)
class AnalysisResult:
    """Read-only view over a normalized result with explicit projections."""

    data: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AnalysisResult":
        if not isinstance(value, Mapping):
            raise TypeError("analysis result must be a mapping")
        return cls(dict(value))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def stages(self) -> list[Any]:
        value = self.data.get("stage_analysis")
        return list(value) if isinstance(value, list) else []

    def improvements(self) -> list[Any]:
        value = self.data.get("improvements")
        return list(value) if isinstance(value, list) else []

    def missing_normalized_fields(self) -> list[str]:
        return [field for field in ANALYSIS_RESULT_CONTRACT.normalized_required_fields if field not in self.data]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.data))

    def project_into(self, analysis: MutableMapping[str, Any]) -> None:
        """Copy only approved result fields into the runtime analysis object."""
        for field in ANALYSIS_RESULT_CONTRACT.projection_fields:
            if field in self.data:
                analysis[field] = copy.deepcopy(self.data[field])
        analysis["analysis_result_contract"] = ANALYSIS_RESULT_CONTRACT.metadata()


def placeholder_stage(definition: StageDefinition) -> dict[str, Any]:
    """Create the single placeholder shape used before an LLM result exists."""
    return {
        "stage": definition.name,
        "time_range": definition.default_time_range,
        "core_question": definition.core_question,
        "benchmark_summary": "待基于关键帧和转录补充。",
        "creator_summary": "待基于关键帧和转录补充。",
        "gap": "待人工或模型分析后填写。",
        "severity": None,
        "placeholder": True,
    }


def placeholder_stages() -> list[dict[str, Any]]:
    return [placeholder_stage(definition) for definition in DEFAULT_STAGES]
