"""Freeze-governance primitives for reproducible validation cohorts.

The functions here classify metadata and produce explicit readiness status. They
do not inspect or alter analysis results, prompts, schemas, labels, or videos.
"""

from __future__ import annotations

from typing import Any, Mapping


FREEZE_CONTRACT_SCHEMA_VERSION = 1
EVALUATION_ROLES = {"calibration", "mechanism_regression", "blind_promotion"}
REQUIRED_FREEZE_CHECKS = (
    "source_commit",
    "prompt_hash",
    "schema_hash",
    "evaluator_hash",
    "gt_hash",
    "model_config_hash",
    "video_identity",
    "validation_root",
)


# These are identity surfaces, not execution hooks. Hashing them makes the
# future cohort lock auditable without importing them into the analysis path.
PROMPT_CONTRACT_FILES = (
    "scripts/flayr_core/prompt.py",
    "scripts/flayr_core/llm/payload.py",
    "scripts/flayr_core/llm/s4_visual_verifier.py",
    "scripts/flayr_core/multimodal.py",
    "scripts/flayr_core/speech_mode.py",
    "scripts/flayr_core/stage_ownership.py",
    "scripts/flayr_core/translation.py",
    "QA-RULES.md",
    "structure_library_full.md",
    "references/observation-guide.md",
    "references/commercial-judgement-framework.md",
    "references/market-knowledge-my.md",
    "references/brand_propositions.json",
)

SCHEMA_CONTRACT_FILES = (
    "references/analysis-output-schema.json",
    "scripts/flayr_core/analysis_model.py",
    "scripts/flayr_core/semantic_model.py",
    "scripts/flayr_core/llm/analysis_contract.py",
    "scripts/flayr_core/llm/stage_review_contract.py",
)

EVALUATOR_CONTRACT_FILES = (
    "scripts/evaluate_analysis.py",
    "scripts/verify_analysis_contracts.py",
    "scripts/check_prompt_reachability.py",
    "scripts/manage_validation_cohort.py",
    "scripts/flayr_core/validation_cohort.py",
    "scripts/flayr_core/freeze_contract.py",
    "scripts/flayr_core/model_execution.py",
)


def _category_values(label: Mapping[str, Any] | None, sample: Mapping[str, Any] | None) -> list[str]:
    values: list[str] = []
    for source in (label, sample):
        if not isinstance(source, Mapping):
            continue
        for key in ("evaluation_role", "partition", "group", "purpose"):
            value = str(source.get(key) or "").strip().lower()
            if value:
                values.append(value)
    return values


def evaluation_role_for_sample(
    label: Mapping[str, Any] | None,
    sample: Mapping[str, Any] | None,
) -> tuple[str | None, list[str]]:
    """Return an orthogonal role while preserving historical metadata.

    Existing ``partition``/``group``/``purpose`` values are read as facts. No
    caller is asked to rewrite those values. A future explicit
    ``evaluation_role`` is accepted only when it agrees with the historical
    classification.
    """
    values = _category_values(label, sample)
    explicit = next(
        (
            str(source.get("evaluation_role") or "").strip().lower()
            for source in (label, sample)
            if isinstance(source, Mapping) and str(source.get("evaluation_role") or "").strip()
        ),
        None,
    )
    errors: list[str] = []
    if explicit and explicit not in EVALUATION_ROLES:
        errors.append(f"evaluation_role 非法：{explicit}")

    inferred: str | None = None
    if "blind" in values:
        inferred = "blind_promotion"
    elif "seen_validation" in values:
        inferred = "mechanism_regression"
    elif "calibration" in values:
        inferred = "calibration"

    if explicit and inferred and explicit != inferred:
        errors.append(f"evaluation_role 与历史分类冲突：{explicit} != {inferred}")
    return explicit or inferred, errors


def cohort_freeze_status(
    checks: Mapping[str, Mapping[str, Any]],
    extra_blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Build the stable ``READY``/``BLOCKED`` status object."""
    blockers = [name for name in REQUIRED_FREEZE_CHECKS if not bool((checks.get(name) or {}).get("ok"))]
    blockers.extend(str(item) for item in (extra_blockers or []) if str(item).strip())
    # Keep output deterministic if a caller reports the same blocker twice.
    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": FREEZE_CONTRACT_SCHEMA_VERSION,
        "status": "READY" if not blockers else "BLOCKED",
        "blocked": blockers,
        "checks": {name: dict(checks.get(name) or {}) for name in REQUIRED_FREEZE_CHECKS},
    }


def format_freeze_blocked(status: Mapping[str, Any]) -> str:
    blocked = status.get("blocked") if isinstance(status.get("blocked"), list) else []
    return "CohortFreezeStatus BLOCKED: [" + ", ".join(str(item) for item in blocked) + "]"
