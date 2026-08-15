#!/usr/bin/env python3
"""Verify the frozen offline semantic-baseline contract without model calls."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from build_legacy_gt_migration_inventory import build_inventory
from evaluate_analysis import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    PROMOTION_MIN_EVENT_RECALL,
    PROMOTION_MIN_OVERALL_ACCURACY,
    SEMANTIC_ACCEPTANCE_MIN_FACT_EVENTS,
    SEMANTIC_ACCEPTANCE_MIN_RELATION_CELLS,
    SEMANTIC_ACCEPTANCE_MIN_SAMPLE_PAIRS,
)
from flayr_core.validation_cohort import SOURCE_CONTRACT_FILES


FREEZE_PATH = ROOT / "references/semantic-baseline-freeze.json"
AB_PATH = ROOT / "references/s4-gradient-handoff-strict-ab.json"
EXPERT_PATH = ROOT / "references/expert-gap-calibration.json"
LABELS_PATH = ROOT / "references/ground-truth-labels.json"
BASELINE_GT_PATH = ROOT / "references/semantic-baseline-gt.json"
BASELINE_MANIFEST_PATH = ROOT / "references/semantic-baseline-manifest.json"
INVENTORY_PATH = ROOT / "references/legacy-gt-migration-inventory.json"
REVIEW_PATH = ROOT / "references/legacy-gt-migration-review.md"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_freeze() -> list[str]:
    errors: list[str] = []
    freeze = _read_json(FREEZE_PATH)
    ab = _read_json(AB_PATH)
    expert = _read_json(EXPERT_PATH)
    labels = _read_json(LABELS_PATH)
    baseline_gt = _read_json(BASELINE_GT_PATH)
    baseline_manifest = _read_json(BASELINE_MANIFEST_PATH)
    inventory = _read_json(INVENTORY_PATH)

    if freeze.get("schema_version") != 1 or freeze.get("status") != "frozen":
        errors.append("semantic baseline freeze must use schema_version=1 and status=frozen")
    if freeze.get("promotion_eligible") is not False:
        errors.append("offline semantic baseline must never be promotion_eligible")
    if freeze.get("evaluation_report_schema_version") != EVALUATION_REPORT_SCHEMA_VERSION:
        errors.append("evaluation report schema version drifted from the frozen contract")

    gt_snapshot = freeze.get("gt_snapshot") or {}
    if gt_snapshot.get("labels_path") != "references/semantic-baseline-gt.json":
        errors.append("frozen GT snapshot path drifted")
    if gt_snapshot.get("manifest_path") != "references/semantic-baseline-manifest.json":
        errors.append("frozen baseline manifest path drifted")
    if gt_snapshot.get("labels_sha256") != _sha256_file(BASELINE_GT_PATH):
        errors.append("frozen GT snapshot hash drifted")
    if gt_snapshot.get("manifest_sha256") != _sha256_file(BASELINE_MANIFEST_PATH):
        errors.append("frozen baseline manifest hash drifted")
    baseline_samples = baseline_gt.get("samples") if isinstance(baseline_gt.get("samples"), dict) else {}
    manifest_samples = baseline_manifest.get("samples") if isinstance(baseline_manifest.get("samples"), list) else []
    manifest_ids = {
        str(sample.get("id") or "")
        for sample in manifest_samples
        if isinstance(sample, dict) and str(sample.get("id") or "").strip()
    }
    if len(baseline_samples) != gt_snapshot.get("sample_pairs") or set(baseline_samples) != manifest_ids:
        errors.append("frozen GT and manifest must contain the same declared sample pairs")
    relation_cells = 0
    present_key_events = 0
    for sample_id, sample in baseline_samples.items():
        if not isinstance(sample, dict):
            errors.append(f"baseline GT sample is not an object: {sample_id}")
            continue
        gaps = sample.get("human_gap") if isinstance(sample.get("human_gap"), dict) else {}
        relations = sample.get("stage_relations") if isinstance(sample.get("stage_relations"), dict) else {}
        for stage, raw_gap in gaps.items():
            gap = str(raw_gap or "").strip().lower()
            relation = relations.get(stage)
            if gap == "none" and relation != "tie":
                errors.append(f"{sample_id}/{stage} none must use tie")
            elif gap in {"small", "medium", "large"} and relation not in {"creator_better", "benchmark_better"}:
                errors.append(f"{sample_id}/{stage} comparable gap must set a direction")
            elif gap in {"na", "not_applicable"} and relation is not None:
                errors.append(f"{sample_id}/{stage} not_applicable must not set a relation")
            elif gap == "uncertain" and relation not in {None, "uncertain"}:
                errors.append(f"{sample_id}/{stage} uncertain gap cannot set a hard relation")
            elif gap not in {"none", "small", "medium", "large", "uncertain", "na", "not_applicable"}:
                errors.append(f"{sample_id}/{stage} has invalid human_gap: {gap!r}")
        relation_cells += sum(
            1 for relation in relations.values()
            if relation in {"creator_better", "benchmark_better", "tie"}
        )
        for event in sample.get("key_events") or []:
            if not isinstance(event, dict):
                errors.append(f"{sample_id} has a malformed key event")
                continue
            if event.get("expected_state", "present") == "present":
                present_key_events += 1
    if relation_cells != gt_snapshot.get("relation_cells"):
        errors.append("frozen relation-cell count drifted")
    if present_key_events != gt_snapshot.get("present_key_events"):
        errors.append("frozen present-key-event count drifted")

    commit = str(freeze.get("production_behavior_commit") or "")
    try:
        subprocess.check_call(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        errors.append(f"production behavior commit is not available: {commit}")
    else:
        if subprocess.call(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ):
            errors.append("production behavior commit is not an ancestor of HEAD")
        frozen_surfaces = ["scripts/flayr.py", "scripts/flayr_core", *SOURCE_CONTRACT_FILES]
        allowed_governance_change = "scripts/flayr_core/freeze_contract.py"
        committed = set(filter(None, _git_output("diff", "--name-only", f"{commit}..HEAD", "--", *frozen_surfaces).splitlines()))
        working = set(filter(None, _git_output("diff", "--name-only", "--", *frozen_surfaces).splitlines()))
        staged = set(filter(None, _git_output("diff", "--cached", "--name-only", "--", *frozen_surfaces).splitlines()))
        changed = sorted((committed | working | staged) - {allowed_governance_change})
        if changed:
            errors.append("production semantic source drifted after the frozen behavior commit: " + ", ".join(changed))

    route = freeze.get("model_route") or {}
    expected_route = {
        "vision_model": "qwen3-vl-plus",
        "judgment_model": "qwen3.7-plus",
        "explicit_judgment_backup": "qwen3.6-plus",
        "automatic_fallback": False,
    }
    for key, expected in expected_route.items():
        if route.get(key) != expected:
            errors.append(f"model_route.{key} must be {expected!r}")
    if "qwen3-vl-flash" not in (route.get("retired_models") or []):
        errors.append("qwen3-vl-flash must remain retired")

    artifact_identity = freeze.get("artifact_identity") or {}
    if artifact_identity.get("success_manifest") != "_SUCCESS.json":
        errors.append("frozen artifact identity must use _SUCCESS.json")
    if artifact_identity.get("analysis_artifact") != "analysis.json":
        errors.append("frozen artifact identity must use analysis.json")
    if artifact_identity.get("analysis_schema_sha256") != _sha256_file(ROOT / "references/analysis-output-schema.json"):
        errors.append("frozen analysis schema identity drifted")
    if artifact_identity.get("stage2_pipeline_version") != "segmented_stage_v1":
        errors.append("frozen stage2 pipeline identity drifted")
    if artifact_identity.get("on_mismatch") != "exclude_and_inventory_without_scoring":
        errors.append("identity mismatch must remain excluded from scoring")

    acceptance = freeze.get("semantic_acceptance") or {}
    expected_acceptance = {
        "minimum_sample_pairs": SEMANTIC_ACCEPTANCE_MIN_SAMPLE_PAIRS,
        "minimum_relation_cells": SEMANTIC_ACCEPTANCE_MIN_RELATION_CELLS,
        "minimum_present_key_events": SEMANTIC_ACCEPTANCE_MIN_FACT_EVENTS,
        "minimum_gap_accuracy": PROMOTION_MIN_OVERALL_ACCURACY,
        "minimum_relation_accuracy": PROMOTION_MIN_OVERALL_ACCURACY,
        "minimum_stage1_event_recall": PROMOTION_MIN_EVENT_RECALL,
        "required_gap_coverage": 1.0,
        "maximum_two_band_errors": 0,
        "maximum_direction_reversals": 0,
        "maximum_applicability_errors": 0,
    }
    for key, expected in expected_acceptance.items():
        if acceptance.get(key) != expected:
            errors.append(f"semantic_acceptance.{key} must be {expected!r}")

    if (freeze.get("execution_policy") or {}).get("allow_new_video_calls_initially") is not False:
        errors.append("the frozen baseline must start with offline artifacts only")
    if (freeze.get("execution_policy") or {}).get("allow_semantic_rule_change_during_cycle") is not False:
        errors.append("semantic rules cannot change inside the frozen cycle")
    if (freeze.get("execution_policy") or {}).get("targeted_call_preconditions") != [
        "clean_worktree",
        "semantic_baseline_freeze_verifier_passed",
        "explicit_missing_sample_list",
    ]:
        errors.append("targeted-call preconditions are incomplete or have drifted")

    if ab.get("execution", {}).get("live_responses") != 8:
        errors.append("strict S4 gradient A/B must record eight live responses")
    no_gradient = ab.get("arm_results", {}).get("no_gradient", {}).get("outputs")
    with_gradient = ab.get("arm_results", {}).get("with_gradient", {}).get("outputs")
    if not isinstance(no_gradient, dict) or no_gradient != with_gradient:
        errors.append("strict S4 gradient arms must preserve the observed identical outputs")
    if ab.get("gt_revision", {}).get("old_two_of_four_exact_score_is_current") is not False:
        errors.append("the legacy 2/4 exact score must remain explicitly obsolete")

    current_labels = ab.get("gt_revision", {}).get("current_expert_labels") or {}
    expert_samples = expert.get("samples") or {}
    for sample_id, expected_gap in current_labels.items():
        actual_gap = ((expert_samples.get(sample_id) or {}).get("human_gap") or {}).get("S4")
        if actual_gap != expected_gap:
            errors.append(f"S4 A/B current label drift for {sample_id}: {actual_gap!r} != {expected_gap!r}")

    for sample_id, sample in expert_samples.items():
        gap = ((sample.get("human_gap") or {}).get("S4"))
        relation = ((sample.get("stage_relations") or {}).get("S4"))
        if gap == "not_applicable" and relation is not None:
            errors.append(f"{sample_id}/S4 not_applicable must not set a relation")
        elif gap == "none" and relation != "tie":
            errors.append(f"{sample_id}/S4 none must use tie")
        elif gap in {"small", "medium", "large"} and relation not in {"creator_better", "benchmark_better"}:
            errors.append(f"{sample_id}/S4 comparable gap must set a direction")

    rebuilt_inventory = build_inventory(labels)
    if inventory != rebuilt_inventory:
        errors.append("legacy GT migration inventory is stale")
    if inventory.get("summary", {}).get("samples") != freeze.get("legacy_gt_migration", {}).get("source_sample_count"):
        errors.append("legacy GT source sample count drifted")
    if inventory.get("policy", {}).get("automatic_small_to_none_migration") is not False:
        errors.append("legacy small must never migrate to none automatically")
    review_rows = [
        [part.strip() for part in line.strip("|").split("|")]
        for line in REVIEW_PATH.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\| [^|]+ \| S[1-6] \|", line)
    ]
    initial_ambiguous = freeze.get("legacy_gt_migration", {}).get("initial_ambiguous_cells")
    if len(review_rows) != initial_ambiguous:
        errors.append("legacy GT human review table must preserve every initially ambiguous cell")
    for row in review_rows:
        if len(row) < 5:
            errors.append("legacy GT review row is malformed")
            continue
        sample_id, stage, review_status = row[0], row[1], row[-1]
        migration_status = (
            ((inventory.get("samples") or {}).get(sample_id) or {}).get("cells") or {}
        ).get(stage, {}).get("migration_status")
        confirmed = review_status.startswith("已确认")
        if confirmed and migration_status != "canonical_existing":
            errors.append(f"{sample_id}/{stage} claims review confirmation without canonical GT")
        if migration_status == "canonical_existing" and not confirmed:
            errors.append(f"{sample_id}/{stage} has canonical GT but the human review table is still pending")

    relation_rows = [
        [part.strip() for part in line.strip("|").split("|")]
        for line in REVIEW_PATH.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\| R\d+ \|", line)
    ]
    minimum_relations = freeze.get("legacy_gt_migration", {}).get("minimum_relation_review_candidates")
    if len(relation_rows) < minimum_relations:
        errors.append("legacy GT relation review candidates are below the frozen minimum")
    for row in relation_rows:
        if len(row) < 6:
            errors.append("legacy GT relation review row is malformed")
            continue
        _review_id, sample_id, stage, proposed_relation, _reason, review_status = row[:6]
        canonical_relation = (
            ((labels.get("samples") or {}).get(sample_id) or {}).get("stage_relations") or {}
        ).get(stage)
        confirmed = review_status.startswith("已确认")
        if confirmed and canonical_relation != proposed_relation:
            errors.append(f"{sample_id}/{stage} claims relation confirmation without matching canonical GT")
        if canonical_relation and canonical_relation == proposed_relation and not confirmed:
            errors.append(f"{sample_id}/{stage} has canonical relation but the human review table is still pending")

    initial_inventory = freeze.get("initial_offline_inventory") or {}
    if initial_inventory.get("identity_compatible_artifacts") != 0:
        errors.append("initial offline inventory must preserve the observed zero compatible artifacts")
    if initial_inventory.get("model_calls_made") != 0:
        errors.append("initial offline inventory must preserve the zero-call execution")
    if initial_inventory.get("semantic_score_status") != "not_computed":
        errors.append("initial semantic score must remain not_computed without compatible artifacts")

    change_control = freeze.get("change_control") or {}
    required_change_classes = {
        "transport_or_provider_incident",
        "deterministic_pipeline_bug",
        "prompt_schema_qualification_or_gap_rule",
        "gt_or_evaluator_bug",
        "result_driven_threshold_change",
    }
    if set(change_control) != required_change_classes:
        errors.append("freeze change-control classes are incomplete or have drifted")
    return errors


def main() -> int:
    errors = verify_freeze()
    if errors:
        print("semantic baseline freeze verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("semantic baseline freeze verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
