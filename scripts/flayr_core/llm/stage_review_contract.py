"""Phase C evidence-and-fact patch contract.

The review model may only patch the structured facts that the deterministic
postprocess chain consumes. Narrative fields, severity and improvements remain
owned by the original analysis and the resolver.
"""

from __future__ import annotations


PHASE_C_REVIEW_SCHEMA_VERSION = 2
PHASE_C_REVIEW_MODE = "evidence_fact_patch_v1"
PHASE_C_PATCH_SNAPSHOT_SCHEMA = "phase_c_patch_snapshot_v1"

_COMMON_FIELDS = ("creator_evidence_ids", "benchmark_evidence_ids")

PATCH_FIELDS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "S1": (*_COMMON_FIELDS, "creator_hook", "benchmark_hook"),
    "S2": (*_COMMON_FIELDS, "creator_s2", "benchmark_s2"),
    "S3": (*_COMMON_FIELDS, "creator_s3", "benchmark_s3"),
    "S4": (*_COMMON_FIELDS, "creator_s4", "benchmark_s4"),
    "S5": (*_COMMON_FIELDS, "creator_s5", "benchmark_s5"),
    "S6": (*_COMMON_FIELDS, "creator_s6", "benchmark_s6"),
}


def patch_fields_for_stage(stage_code: str) -> tuple[str, ...]:
    """Return the closed set of facts Phase C may patch for one stage."""
    return PATCH_FIELDS_BY_STAGE.get(str(stage_code or "").upper(), ())
