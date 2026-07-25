"""Pure helpers for derive calibration and blind-cohort reporting.

These helpers do not decide semantic labels. They only compare labeled cards,
repeat-run outputs, and resolver events so calibration evidence cannot be
confused with production inference.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from ..evidence_states import S3_USAGE_EVIDENCE_STATES, S4_EFFECT_EVIDENCE_STATES
from ..validation_cohort import SOURCE_CONTRACT_FILES, verify_cohort_lock


CALIBRATION_STAGES = ("S3", "S4")
MIN_BOUNDARY_CARDS = 24
MIN_REPEAT_RUNS = 5
MIN_BLIND_PAIRS = 12
MIN_BLIND_CATEGORIES = 4
MIN_BLIND_MARKETS = 2
EXPECTED_FLOOR_OUTCOMES = (
    "trigger_large",
    "no_trigger_medium_kept",
    "uncertain_no_trigger",
    "audit_only_candidate",
)
HARD_FACT_CHECK_STATUSES = ("consistent", "state_conflict", "incomplete")
ACTIVATION_EVIDENCE_KIND = "s4_large_floor_activation_v1"
ACTIVATION_EVIDENCE_SCHEMA_VERSION = 1
REQUIRED_S4_CALIBRATION_SAMPLE_ID = "youkoubo-c1/S4"
REPEAT_STABILITY_FIELDS = (
    "usage_process_visible",
    "core_selling_point_visible",
    "action_proof_met",
    "action_target_contact_met",
    "action_application_change_visible",
    "critical_action_continuity_met",
)
_EXPLICIT_STRENGTHS = {"direct", "explicit"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRUST_TOKEN = object()


class TrustedS4ActivationEvidence:
    """Evidence loaded through the pinned external activation-manifest path.

    A normal result dictionary is deliberately not accepted as activation
    evidence. Keeping this marker out of ``analysis.json`` prevents an
    offline result artifact from enabling a production severity rule.
    """

    __slots__ = ("_payload", "_source_path", "_source_sha256", "_payload_sha256")

    def __init__(self, payload: dict[str, Any], source_path: str, source_sha256: str, token: object) -> None:
        if token is not _TRUST_TOKEN:
            raise TypeError("TrustedS4ActivationEvidence must be loaded from a pinned manifest")
        self._payload = deepcopy(payload)
        self._source_path = source_path
        self._source_sha256 = source_sha256
        self._payload_sha256 = _sha256_json(self._payload)

    @property
    def payload(self) -> dict[str, Any]:
        """Return a copy so callers cannot mutate the trusted snapshot."""
        return deepcopy(self._payload)

    @property
    def source_path(self) -> str:
        return self._source_path

    @property
    def source_sha256(self) -> str:
        return self._source_sha256

    @property
    def payload_sha256(self) -> str:
        return self._payload_sha256

    @classmethod
    def _from_loader(cls, payload: dict[str, Any], source_path: str, source_sha256: str) -> "TrustedS4ActivationEvidence":
        return cls(payload, source_path, source_sha256, _TRUST_TOKEN)


def _count(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return bool(_SHA256_RE.fullmatch(str(value or "").strip().lower()))


def _validate_blind_cohort_lock(
    lock: Any,
    blind_samples: list[Any],
    *,
    expected_lock_sha256: Any,
    expected_source_contract_sha256: Any,
) -> list[str]:
    """Verify that activation metadata points to the independently frozen cohort.

    Shape checks on the activation payload are not enough: a caller must provide
    the complete cohort lock so the verifier can re-hash the repo, source
    contract files, labels, manifest, and every locked video at activation time.
    """
    if not isinstance(lock, dict):
        return ["blind cohort must include a complete cohort_lock object"]

    errors: list[str] = []
    if lock.get("status") != "frozen":
        errors.append("cohort_lock.status must be frozen for activation")
    if _sha256_json(lock) != expected_lock_sha256:
        errors.append("blind cohort lock does not match blind_cohort.lock_sha256")

    source_contract_files = lock.get("source_contract_files")
    if not isinstance(source_contract_files, dict):
        errors.append("cohort_lock.source_contract_files must be an object")
    else:
        missing_contracts = sorted(set(SOURCE_CONTRACT_FILES) - set(source_contract_files))
        if missing_contracts:
            errors.append(
                "cohort_lock.source_contract_files is missing: " + ",".join(missing_contracts)
            )
        if _sha256_json(source_contract_files) != expected_source_contract_sha256:
            errors.append("cohort_lock source contract identities do not match provenance.source_contract_sha256")

    try:
        errors.extend(f"cohort_lock: {error}" for error in verify_cohort_lock(lock))
    except (OSError, TypeError, ValueError) as exc:
        errors.append(f"cohort_lock could not be verified: {exc}")

    locked_samples = lock.get("samples")
    if not isinstance(locked_samples, list):
        errors.append("cohort_lock.samples must be a list")
        locked_samples = []
    locked_ids = [
        str(sample.get("id") or "").strip()
        for sample in locked_samples
        if isinstance(sample, dict)
    ]
    if any(not sample_id for sample_id in locked_ids) or len(locked_ids) != len(set(locked_ids)):
        errors.append("cohort_lock sample ids must be non-empty and unique")
    if lock.get("sample_ids") != locked_ids:
        errors.append("cohort_lock.sample_ids must match cohort_lock.samples")

    blind_ids = [
        str(sample.get("sample_id") or "").strip()
        for sample in blind_samples
        if isinstance(sample, dict)
    ]
    if blind_ids != locked_ids:
        errors.append("blind cohort samples must match the locked sample identities")

    locked_by_id = {
        str(sample.get("id") or "").strip(): sample
        for sample in locked_samples
        if isinstance(sample, dict)
    }
    for sample in blind_samples:
        if not isinstance(sample, dict):
            continue
        sample_id = str(sample.get("sample_id") or "").strip()
        locked = locked_by_id.get(sample_id)
        if not isinstance(locked, dict):
            continue
        if str(sample.get("category") or "").strip() != str(locked.get("product_category") or "").strip():
            errors.append(f"{sample_id}: blind category does not match cohort lock")
        if str(sample.get("market") or "").strip() != str(locked.get("target_market") or "").strip():
            errors.append(f"{sample_id}: blind market does not match cohort lock")
        videos = locked.get("videos")
        if not isinstance(videos, dict) or not all(isinstance(videos.get(role), dict) for role in ("creator", "benchmark")):
            errors.append(f"{sample_id}: cohort lock must contain creator and benchmark video identities")
    return errors


def _trusted_payload(evidence: Any) -> dict[str, Any] | None:
    if not isinstance(evidence, TrustedS4ActivationEvidence):
        return None
    return evidence.payload


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    if successes < 0 or successes > total:
        raise ValueError("successes must be between 0 and total")
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt((proportion * (1.0 - proportion) / total) + (z * z / (4.0 * total * total)))
        / denominator
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def summarize_repeat_stability(
    runs: Iterable[dict[str, Any]],
    fields: Iterable[str],
) -> dict[str, Any]:
    """Measure whether hard-fact outputs stay identical across repeated runs."""
    observations = list(runs)
    field_names = tuple(fields)
    per_field: dict[str, dict[str, Any]] = {}
    for field in field_names:
        values = [observation.get(field) for observation in observations]
        distinct = []
        for value in values:
            if value not in distinct:
                distinct.append(value)
        per_field[field] = {
            "stable": len(distinct) == 1 and bool(values) and all(value is not None for value in values),
            "values": distinct,
            "runs": len(values),
            "missing_runs": sum(value is None for value in values),
        }
    enough_runs = len(observations) >= MIN_REPEAT_RUNS
    stable = enough_runs and bool(field_names) and all(item["stable"] for item in per_field.values())
    return {
        "runs": len(observations),
        "stable": stable,
        "status": "measured" if enough_runs else "insufficient_runs",
        "minimum_runs": MIN_REPEAT_RUNS,
        "fields": per_field,
        "unstable_fields": [field for field, item in per_field.items() if not item["stable"]],
    }


def summarize_floor_coverage(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Report floor capture without turning coverage into an unsafe activation rule.

    A record is eligible for the denominator only when a blind label says that
    a structural gap really exists. The caller supplies ``floor_applied`` from
    the resolver trace; no semantic label is inferred here.
    """
    rows = list(records)
    eligible = [row for row in rows if row.get("ground_truth_structural_gap") is True]
    captured = [row for row in eligible if row.get("floor_applied") is True]
    regressions = [row for row in rows if row.get("derive_regression") is True]
    total = len(eligible)
    coverage = len(captured) / total if total else None
    interval = _wilson_interval(len(captured), total) if total else None
    return {
        "records": len(rows),
        "eligible_structural_gap_cases": total,
        "captured_by_floor": len(captured),
        "coverage": coverage,
        "coverage_wilson_95": interval,
        "floor_applied": sum(1 for row in rows if row.get("floor_applied") is True),
        "derive_regressions": len(regressions),
        "status": "measured" if total else "unavailable_without_blind_ground_truth",
    }


def validate_derive_calibration_card(card: Any) -> list[str]:
    """Validate one calibration-only boundary card without assigning labels.

    Cards are deliberately separate from blind validation samples. The caller
    still has to provide the two human annotations and fresh-cohort evidence;
    this helper only prevents malformed or unsafe card metadata from entering
    those reports.
    """
    if not isinstance(card, dict):
        return ["card must be an object"]
    errors: list[str] = []
    sample_id = str(card.get("sample_id") or "").strip()
    if not sample_id:
        errors.append("sample_id is required")
    if card.get("partition") != "calibration":
        errors.append("partition must be calibration; blind cards are not accepted")
    stage = str(card.get("stage") or "").strip()
    if stage not in CALIBRATION_STAGES:
        errors.append("stage must be S3 or S4")
    if sample_id == REQUIRED_S4_CALIBRATION_SAMPLE_ID:
        if stage != "S4":
            errors.append(f"{REQUIRED_S4_CALIBRATION_SAMPLE_ID} must be an S4 card")
        if card.get("background_known") is not False:
            errors.append(f"{REQUIRED_S4_CALIBRATION_SAMPLE_ID} must be marked background_known=false")

    allowed_states = S3_USAGE_EVIDENCE_STATES if stage == "S3" else S4_EFFECT_EVIDENCE_STATES
    if stage not in CALIBRATION_STAGES:
        return errors

    annotations: list[dict[str, Any]] = []
    annotator_ids: list[str] = []
    for annotation_key in ("annotation_a", "annotation_b"):
        annotation = card.get(annotation_key)
        if not isinstance(annotation, dict):
            errors.append(f"{annotation_key} must be an object with independent labels")
            continue
        annotator_id = str(annotation.get("annotator_id") or "").strip()
        if not annotator_id:
            errors.append(f"{annotation_key}.annotator_id is required")
        annotator_ids.append(annotator_id)
        for role in ("creator", "benchmark"):
            state_key = f"{role}_state"
            status_key = f"{role}_hard_fact_status"
            strength_key = f"{role}_strength"
            if annotation.get(state_key) not in allowed_states:
                errors.append(f"{annotation_key}.{state_key} is invalid for the selected stage")
            if annotation.get(status_key) not in HARD_FACT_CHECK_STATUSES:
                errors.append(f"{annotation_key}.{status_key} is invalid")
            if annotation.get(strength_key) not in ("direct", "explicit", "inferred", "absent"):
                errors.append(f"{annotation_key}.{strength_key} is invalid")
        annotations.append(annotation)

    if len(annotations) == 2 and annotator_ids[0] and annotator_ids[0] == annotator_ids[1]:
        errors.append("annotation_a and annotation_b must be different annotators")

    for role in ("creator", "benchmark"):
        if card.get(f"expected_{role}_state") not in allowed_states:
            errors.append(f"expected_{role}_state is invalid for the selected stage")
        if card.get(f"expected_{role}_hard_fact_status") not in HARD_FACT_CHECK_STATUSES:
            errors.append(f"expected_{role}_hard_fact_status is invalid")
        if card.get(f"expected_{role}_strength") not in (None, "direct", "explicit", "inferred", "absent"):
            errors.append(f"expected_{role}_strength is invalid")
    if card.get("expected_floor_outcome") not in EXPECTED_FLOOR_OUTCOMES:
        errors.append("expected_floor_outcome is invalid")

    if len(annotations) == 2 and all(annotator_ids):
        for role in ("creator", "benchmark"):
            state_values = [annotation[f"{role}_state"] for annotation in annotations]
            status_values = [annotation[f"{role}_hard_fact_status"] for annotation in annotations]
            strength_values = [annotation[f"{role}_strength"] for annotation in annotations]
            expected_state = state_values[0] if state_values[0] == state_values[1] else "uncertain"
            expected_status = status_values[0] if status_values[0] == status_values[1] else "incomplete"
            expected_strength = strength_values[0] if strength_values[0] == strength_values[1] else None
            if card.get(f"expected_{role}_state") != expected_state:
                errors.append(f"expected_{role}_state does not match the two annotations")
            if card.get(f"expected_{role}_hard_fact_status") != expected_status:
                errors.append(f"expected_{role}_hard_fact_status does not match the two annotations")
            if card.get(f"expected_{role}_strength") != expected_strength:
                errors.append(f"expected_{role}_strength does not match the two annotations")

        creator_state = card.get("expected_creator_state")
        benchmark_state = card.get("expected_benchmark_state")
        creator_status = card.get("expected_creator_hard_fact_status")
        benchmark_status = card.get("expected_benchmark_hard_fact_status")
        creator_strength = card.get("expected_creator_strength")
        benchmark_strength = card.get("expected_benchmark_strength")
        if (
            creator_state == "uncertain"
            or benchmark_state == "uncertain"
            or creator_status != "consistent"
            or benchmark_status != "consistent"
            or creator_strength not in _EXPLICIT_STRENGTHS
            or benchmark_strength not in _EXPLICIT_STRENGTHS
        ):
            expected_outcome = "uncertain_no_trigger"
        elif stage == "S3" and creator_state == "none" and benchmark_state == "complete":
            expected_outcome = "trigger_large"
        elif stage == "S4" and creator_state == "none" and benchmark_state == "verified":
            expected_outcome = "audit_only_candidate"
        else:
            expected_outcome = "no_trigger_medium_kept"
        if card.get("expected_floor_outcome") != expected_outcome:
            errors.append("expected_floor_outcome does not match the independent annotations")
    return errors


def validate_derive_calibration_cards(cards: Any) -> list[str]:
    """Validate the complete boundary-card set used before S4 activation.

    This validates coverage metadata only. It does not replace the two human
    annotations; those are required separately by the activation evidence.
    """
    if not isinstance(cards, list):
        return ["calibration cards must be a list"]
    errors: list[str] = []
    if len(cards) < MIN_BOUNDARY_CARDS:
        errors.append(f"calibration cards require at least {MIN_BOUNDARY_CARDS} cards")
    sample_ids: set[str] = set()
    annotator_ids: set[str] = set()
    by_stage: dict[str, list[dict[str, Any]]] = {stage: [] for stage in CALIBRATION_STAGES}
    for index, card in enumerate(cards):
        errors.extend(f"card[{index}]: {error}" for error in validate_derive_calibration_card(card))
        if not isinstance(card, dict):
            continue
        sample_id = str(card.get("sample_id") or "").strip()
        if sample_id in sample_ids:
            errors.append(f"duplicate calibration sample_id: {sample_id}")
        sample_ids.add(sample_id)
        for annotation_key in ("annotation_a", "annotation_b"):
            annotation = card.get(annotation_key)
            if isinstance(annotation, dict) and str(annotation.get("annotator_id") or "").strip():
                annotator_ids.add(str(annotation["annotator_id"]).strip())
        stage = str(card.get("stage") or "")
        if stage in by_stage:
            by_stage[stage].append(card)

    required_states = {
        "S3": set(S3_USAGE_EVIDENCE_STATES),
        "S4": set(S4_EFFECT_EVIDENCE_STATES),
    }
    for stage, stage_cards in by_stage.items():
        if not stage_cards:
            errors.append(f"calibration cards missing stage {stage}")
            continue
        observed = {
            value
            for card in stage_cards
            for role in ("creator", "benchmark")
            for value in (card.get(f"expected_{role}_state"),)
        }
        missing_states = sorted(required_states[stage] - observed)
        if missing_states:
            errors.append(f"calibration {stage} boundary-state coverage missing: {','.join(missing_states)}")

    # Stage-level state presence is not enough for the boundaries that gate a
    # floor. Keep the two sides explicit so a card set cannot pass by putting
    # every boundary state on the wrong role.
    required_boundaries = {
        "S3": {
            "creator": {"none", "partial"},
            "benchmark": {"partial", "complete"},
        },
        "S4": {
            "creator": {"result_only", "verified"},
            "benchmark": {"result_only", "verified"},
        },
    }
    for stage, roles in required_boundaries.items():
        stage_cards = by_stage[stage]
        for role, required in roles.items():
            observed = {
                card.get(f"expected_{role}_state")
                for card in stage_cards
                if isinstance(card.get(f"expected_{role}_state"), str)
            }
            missing = sorted(required - observed)
            if missing:
                errors.append(f"calibration {stage} {role} boundary coverage missing: {','.join(missing)}")
    required_s4_cards = [
        card for card in cards
        if isinstance(card, dict) and str(card.get("sample_id") or "").strip() == REQUIRED_S4_CALIBRATION_SAMPLE_ID
    ]
    if len(required_s4_cards) != 1:
        errors.append(f"calibration cards must include exactly one {REQUIRED_S4_CALIBRATION_SAMPLE_ID} card")
    elif required_s4_cards[0].get("background_known") is not False:
        errors.append(f"{REQUIRED_S4_CALIBRATION_SAMPLE_ID} must be an unknown-background blind-style card")
    if annotator_ids != {"annotator_a", "annotator_b"}:
        errors.append("calibration cards must contain the two fixed independent annotators")
    return list(dict.fromkeys(errors))


def _validate_s4_activation_payload(payload: Any) -> list[str]:
    """Return blockers for enabling the S4 large floor.

    The production default is audit-only. A caller may only enable the rule
    with explicit, independently produced calibration and blind-cohort
    evidence. This function verifies the evidence shape and minimum gates; it
    never invents labels or treats missing data as a pass.
    """
    if not isinstance(payload, dict):
        return ["S4 large-floor activation evidence payload must be an object"]
    errors: list[str] = []
    if payload.get("kind") != ACTIVATION_EVIDENCE_KIND:
        errors.append(f"activation evidence kind must be {ACTIVATION_EVIDENCE_KIND}")
    if payload.get("schema_version") != ACTIVATION_EVIDENCE_SCHEMA_VERSION:
        errors.append("activation evidence schema_version is unsupported")
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    for key in ("producer", "created_at", "source_contract_sha256", "calibration_cards_sha256", "blind_cohort_lock_sha256"):
        if key in {"producer", "created_at"}:
            if not str(provenance.get(key) or "").strip():
                errors.append(f"provenance.{key} is required")
        elif not _is_sha256(provenance.get(key)):
            errors.append(f"provenance.{key} must be a sha256")
    provenance_annotators = provenance.get("annotator_ids")
    if sorted(str(value) for value in provenance_annotators or []) != ["annotator_a", "annotator_b"]:
        errors.append("provenance.annotator_ids must name both independent annotators")

    calibration = payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
    cards = calibration.get("cards")
    errors.extend(validate_derive_calibration_cards(cards))
    if isinstance(cards, list) and _sha256_json(cards) != provenance.get("calibration_cards_sha256"):
        errors.append("calibration cards do not match provenance.calibration_cards_sha256")
    if calibration.get("s3_boundary_status") != "passed":
        errors.append("calibration requires passed S3 boundary review")
    if calibration.get("s4_boundary_status") != "passed":
        errors.append("calibration requires passed S4 result_only/verified boundary review")
    if calibration.get("excluded_from_blind") is not True:
        errors.append("calibration cards must be excluded from the blind cohort")

    repeat = payload.get("repeat_stability") if isinstance(payload.get("repeat_stability"), dict) else {}
    observations = repeat.get("observations")
    fields = repeat.get("fields")
    repeat_valid = isinstance(observations, list) and isinstance(fields, list) and bool(fields)
    if not repeat_valid:
        errors.append("repeat stability must include raw observations and fields")
        repeat_summary = {}
    else:
        field_names = [field for field in fields if isinstance(field, str) and field.strip()]
        if len(field_names) != len(fields) or len(field_names) != len(set(field_names)):
            errors.append("repeat stability fields must be non-empty and unique")
            repeat_valid = False
        if set(field_names) != set(REPEAT_STABILITY_FIELDS):
            errors.append("repeat stability must measure the closed core hard-fact field set")
            repeat_valid = False
        input_fingerprint = repeat.get("input_fingerprint")
        if not _is_sha256(input_fingerprint):
            errors.append("repeat stability input_fingerprint must be a sha256")
            repeat_valid = False
        for index, observation in enumerate(observations):
            if not isinstance(observation, dict):
                errors.append(f"repeat stability observation[{index}] must be an object")
                repeat_valid = False
                continue
            if observation.get("input_fingerprint") != input_fingerprint:
                errors.append(f"repeat stability observation[{index}] input fingerprint does not match")
                repeat_valid = False
            for field in REPEAT_STABILITY_FIELDS:
                if observation.get(field) not in {True, False}:
                    errors.append(f"repeat stability observation[{index}].{field} must be bool")
                    repeat_valid = False
        if repeat_valid:
            repeat_summary = summarize_repeat_stability(observations, fields)
            if repeat.get("status") != repeat_summary["status"] or repeat.get("stable") is not repeat_summary["stable"]:
                errors.append("repeat stability summary does not match raw observations")
        else:
            repeat_summary = {}
    if repeat_summary.get("status") != "measured" or repeat_summary.get("stable") is not True:
        errors.append("hard-fact repeat stability is not measured and stable")
    if _count(repeat_summary.get("runs")) < MIN_REPEAT_RUNS:
        errors.append(f"repeat stability requires at least {MIN_REPEAT_RUNS} runs")

    blind = payload.get("blind_cohort") if isinstance(payload.get("blind_cohort"), dict) else {}
    if blind.get("status") != "passed" or blind.get("locked") is not True or blind.get("fresh") is not True:
        errors.append("blind cohort must be fresh, locked, and passed")
    if not _is_sha256(blind.get("lock_sha256")) or blind.get("lock_sha256") != provenance.get("blind_cohort_lock_sha256"):
        errors.append("blind cohort must carry the pinned lock sha256")
    blind_samples = blind.get("samples")
    if not isinstance(blind_samples, list):
        errors.append("blind cohort must include locked sample identities")
        blind_samples = []
    errors.extend(
        _validate_blind_cohort_lock(
            blind.get("cohort_lock"),
            blind_samples,
            expected_lock_sha256=blind.get("lock_sha256"),
            expected_source_contract_sha256=provenance.get("source_contract_sha256"),
        )
    )
    blind_ids = [str(sample.get("sample_id") or "").strip() for sample in blind_samples if isinstance(sample, dict)]
    if len(blind_ids) != len(set(blind_ids)) or any(not sample_id for sample_id in blind_ids):
        errors.append("blind cohort sample identities must be non-empty and unique")
    calibration_ids = {
        str(card.get("sample_id") or "").strip()
        for card in cards or []
        if isinstance(card, dict)
    }
    if calibration_ids & set(blind_ids):
        errors.append("calibration cards must be excluded from the blind cohort")
    category_count = len({str(sample.get("category") or "").strip() for sample in blind_samples if isinstance(sample, dict) and str(sample.get("category") or "").strip()})
    market_count = len({str(sample.get("market") or "").strip() for sample in blind_samples if isinstance(sample, dict) and str(sample.get("market") or "").strip()})
    if len(blind_samples) < MIN_BLIND_PAIRS:
        errors.append(f"blind cohort requires at least {MIN_BLIND_PAIRS} independent video pairs")
    if category_count < MIN_BLIND_CATEGORIES:
        errors.append(f"blind cohort requires at least {MIN_BLIND_CATEGORIES} categories")
    if market_count < MIN_BLIND_MARKETS:
        errors.append(f"blind cohort requires at least {MIN_BLIND_MARKETS} markets")
    if any(sample.get("phase_c_regression") is True for sample in blind_samples if isinstance(sample, dict)):
        errors.append("blind cohort contains Phase C regressions")

    coverage = payload.get("floor_coverage") if isinstance(payload.get("floor_coverage"), dict) else {}
    coverage_records = coverage.get("records")
    if not isinstance(coverage_records, list):
        errors.append("floor coverage must include raw records")
        coverage_summary = {}
    else:
        if any(not isinstance(record, dict) for record in coverage_records):
            errors.append("floor coverage records must be objects")
            coverage_summary = {}
        else:
            coverage_summary = summarize_floor_coverage(coverage_records)
            if coverage.get("status") != coverage_summary["status"] or coverage.get("derive_regressions") != coverage_summary["derive_regressions"]:
                errors.append("floor coverage summary does not match raw records")
    if coverage_summary.get("status") != "measured":
        errors.append("floor coverage is unavailable without blind ground truth")
    if _count(coverage_summary.get("derive_regressions"), default=-1) != 0:
        errors.append("floor coverage contains derive regressions")
    return list(dict.fromkeys(errors))


def load_s4_large_floor_activation_evidence(path: Path | str, expected_sha256: str) -> TrustedS4ActivationEvidence:
    """Load a pinned activation manifest; result artifacts cannot call this path."""
    manifest_path = Path(path).expanduser()
    if not _is_sha256(expected_sha256):
        raise ValueError("expected_sha256 must be a 64-character sha256")
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("activation evidence sha256 does not match the pinned digest")
    payload = json.loads(raw.decode("utf-8"))
    errors = _validate_s4_activation_payload(payload)
    if errors:
        raise ValueError("invalid S4 activation evidence:\n- " + "\n- ".join(errors))
    return TrustedS4ActivationEvidence._from_loader(payload, str(manifest_path.resolve()), digest)


def validate_s4_large_floor_activation_evidence(evidence: Any) -> list[str]:
    """Validate only evidence loaded through the pinned external manifest path."""
    payload = _trusted_payload(evidence)
    if payload is None:
        return ["S4 activation evidence must be loaded from a pinned external manifest"]
    if evidence.payload_sha256 != _sha256_json(payload):
        return ["S4 activation evidence payload was modified after loading"]
    return _validate_s4_activation_payload(payload)


__all__ = [
    "CALIBRATION_STAGES",
    "EXPECTED_FLOOR_OUTCOMES",
    "HARD_FACT_CHECK_STATUSES",
    "ACTIVATION_EVIDENCE_KIND",
    "ACTIVATION_EVIDENCE_SCHEMA_VERSION",
    "REPEAT_STABILITY_FIELDS",
    "REQUIRED_S4_CALIBRATION_SAMPLE_ID",
    "MIN_BOUNDARY_CARDS",
    "MIN_BLIND_CATEGORIES",
    "MIN_BLIND_MARKETS",
    "MIN_BLIND_PAIRS",
    "MIN_REPEAT_RUNS",
    "summarize_floor_coverage",
    "summarize_repeat_stability",
    "validate_derive_calibration_card",
    "validate_derive_calibration_cards",
    "validate_s4_large_floor_activation_evidence",
    "TrustedS4ActivationEvidence",
    "load_s4_large_floor_activation_evidence",
]
