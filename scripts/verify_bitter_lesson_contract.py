#!/usr/bin/env python3
"""Validate the machine-readable bitter-lesson contract and frozen tests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "references" / "bitter-lesson-frozen-spec.json"
REQUIRED_LAYERS = ("provider", "canonical", "finalizer", "report")
REQUIRED_VERIFICATION_ORDER = (
    "fixture",
    "offline_replay",
    "fake_provider",
    "ordinary_sample",
    "boundary_sample",
)
EXPECTED_LAYER_BOUNDARIES = {
    "provider": {
        "owner": "provider_adapter",
        "input": "provider_request",
        "output": "provider_artifact",
        "may_write": ["provider_artifact"],
        "must_not_write": ["canonical_facts", "stage_judgment", "report"],
    },
    "canonical": {
        "owner": "canonical_normalizer",
        "input": "provider_artifact",
        "output": "canonical_facts",
        "may_write": ["canonical_facts", "stage_qualification"],
        "must_not_write": ["stage_judgment", "severity", "report"],
    },
    "finalizer": {
        "owner": "finalizer",
        "input": "canonical_facts_and_stage_judgment",
        "output": "final_result",
        "may_write": ["stage_judgment", "severity", "final_result", "audit_trace"],
        "must_not_write": ["canonical_facts", "provider_artifact"],
    },
    "report": {
        "owner": "report_renderer",
        "input": "final_result",
        "output": "report",
        "may_write": ["report"],
        "must_not_write": ["canonical_facts", "stage_judgment", "severity"],
    },
}
EXPECTED_TYPES = {
    "evidence_state": {
        "kind": "enum",
        "values": ["captured", "explicit_absence", "uncertain", "not_observable", "budget_exhausted"],
    },
    "stage_gate_status": {
        "kind": "enum",
        "values": ["grounded", "blocked", "not_applicable", "not_comparable", "legacy"],
    },
    "provider_artifact": {
        "kind": "object",
        "required": ["schema_version", "status", "request_identity", "response_meta"],
        "response_meta_required": ["logical_request_id", "completion_attempts", "retry_reasons", "usage"],
        "completed_requires": ["provider_response", "response_sha256"],
        "failed_requires": ["error"],
    },
    "field_disposition": {
        "kind": "object",
        "required": ["path", "action", "reason"],
        "action_values": ["preserved", "derived", "discarded", "blocked"],
    },
}
REQUIRED_INVARIANT_SELECTORS = {
    "BL-LAYER-001": (
        "test_layer_ownership_is_unique",
        "test_runtime_field_ownership_gate_rejects_unauthorized_writer",
    ),
    "BL-EVIDENCE-001": ("test_stage1_handoff_is_hash_bound_and_lossless",),
    "BL-HANDOFF-001": ("test_stage1_handoff_is_hash_bound_and_lossless",),
    "BL-UNKNOWN-001": ("test_unknown_stage_never_becomes_publishable_severity",),
    "BL-REPLAY-001": ("test_provider_artifact_replay_requires_exact_identity",),
    "BL-MECHANICAL-001": ("test_stage3_cannot_author_mechanical_fields",),
    "BL-RETRY-001": ("test_provider_artifact_keeps_retry_metadata",),
    "BL-ORDER-001": (
        "test_verification_order_is_frozen",
        "test_verification_order_blocks_boundary_until_prerequisites_pass",
    ),
}


class FrozenContractError(ValueError):
    """Raised when the frozen bitter-lesson contract is incomplete or drifted."""


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenContractError(f"cannot read frozen specification: {path}") from exc
    if not isinstance(value, dict):
        raise FrozenContractError("frozen specification must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    # Git may materialize the same tracked text with CRLF on Windows. The
    # frozen contract locks source content, not checkout-specific newlines.
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def _require_nonempty_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise FrozenContractError(f"{name} must be a non-empty list")
    return value


def validate_spec(spec: dict[str, Any], root: Path = ROOT) -> None:
    if spec.get("schema_version") != 1 or spec.get("status") != "frozen":
        raise FrozenContractError("frozen specification must be schema_version=1 and status=frozen")

    layers = _require_nonempty_list(spec.get("layers"), "layers")
    layer_ids = [item.get("id") for item in layers if isinstance(item, dict)]
    if tuple(layer_ids) != REQUIRED_LAYERS:
        raise FrozenContractError(f"layers must be exactly {REQUIRED_LAYERS}, got {layer_ids!r}")
    owners = [item.get("owner") for item in layers if isinstance(item, dict)]
    if len(owners) != len(set(owners)):
        raise FrozenContractError("layer owners must be unique")
    for item in layers:
        if not isinstance(item, dict) or not item.get("input") or not item.get("output"):
            raise FrozenContractError("every layer needs input and output types")
        if not item.get("may_write") or not item.get("must_not_write"):
            raise FrozenContractError("every layer needs may_write and must_not_write boundaries")
        expected = EXPECTED_LAYER_BOUNDARIES[item["id"]]
        for key, expected_value in expected.items():
            if item.get(key) != expected_value:
                raise FrozenContractError(f"layer boundary drifted: {item['id']}.{key}")

    types = spec.get("types")
    if not isinstance(types, dict):
        raise FrozenContractError("types must be an object")
    for type_name, expected in EXPECTED_TYPES.items():
        actual = types.get(type_name)
        if not isinstance(actual, dict):
            raise FrozenContractError(f"missing type definition: {type_name}")
        for key, expected_value in expected.items():
            if actual.get(key) != expected_value:
                raise FrozenContractError(f"type definition drifted: {type_name}.{key}")

    invariants = _require_nonempty_list(spec.get("invariants"), "invariants")
    invariant_ids: set[str] = set()
    invariant_selectors: dict[str, tuple[str, ...]] = {}
    for invariant in invariants:
        if not isinstance(invariant, dict):
            raise FrozenContractError("invariants must be objects")
        invariant_id = str(invariant.get("id") or "")
        if not invariant_id or invariant_id in invariant_ids:
            raise FrozenContractError(f"duplicate or missing invariant id: {invariant_id!r}")
        invariant_ids.add(invariant_id)
        if not invariant.get("rule") or not _require_nonempty_list(
            invariant.get("test_selectors"), f"test_selectors for {invariant_id}"
        ):
            raise FrozenContractError(f"invariant {invariant_id} is incomplete")
        invariant_selectors[invariant_id] = tuple(str(item) for item in invariant["test_selectors"])
    if set(invariant_selectors) != set(REQUIRED_INVARIANT_SELECTORS):
        raise FrozenContractError("invariant set drifted")
    for invariant_id, selectors in REQUIRED_INVARIANT_SELECTORS.items():
        if invariant_selectors[invariant_id] != selectors:
            raise FrozenContractError(f"invariant selectors drifted: {invariant_id}")

    if spec.get("verification_order") != list(REQUIRED_VERIFICATION_ORDER):
        raise FrozenContractError("verification order drifted")
    non_goals = _require_nonempty_list(spec.get("non_goals"), "non_goals")
    if any(not str(item).strip() for item in non_goals):
        raise FrozenContractError("non_goals cannot contain empty entries")

    ambiguities = _require_nonempty_list(spec.get("ambiguities"), "ambiguities")
    for ambiguity in ambiguities:
        if not isinstance(ambiguity, dict) or ambiguity.get("status") != "resolved" or not ambiguity.get("decision"):
            raise FrozenContractError(f"unresolved ambiguity: {ambiguity!r}")

    scope = spec.get("change_scope")
    if not isinstance(scope, dict):
        raise FrozenContractError("change_scope must be an object")
    for key in ("max_files", "max_added_lines", "max_deleted_lines"):
        if not isinstance(scope.get(key), int) or scope[key] <= 0:
            raise FrozenContractError(f"change_scope.{key} must be a positive integer")
    allowed = _require_nonempty_list(scope.get("allowed_globs"), "change_scope.allowed_globs")
    forbidden = _require_nonempty_list(scope.get("forbidden_globs"), "change_scope.forbidden_globs")
    if set(allowed) & set(forbidden):
        raise FrozenContractError("a path glob cannot be both allowed and forbidden")

    test_files = _require_nonempty_list(spec.get("frozen_contract_test_files"), "frozen_contract_test_files")
    hashes = spec.get("frozen_contract_test_hashes")
    if not isinstance(hashes, dict):
        raise FrozenContractError("frozen_contract_test_hashes must be an object")
    for relative in test_files:
        path = root / str(relative)
        if not path.is_file():
            raise FrozenContractError(f"frozen contract test is missing: {relative}")
        expected = str(hashes.get(str(relative)) or "")
        if not expected:
            raise FrozenContractError(f"frozen contract test hash is missing: {relative}")
        actual = _sha256(path)
        if actual != expected:
            raise FrozenContractError(
                f"frozen contract test changed: {relative}; expected {expected}, got {actual}"
            )
        for selector in [
            selector
            for invariant in invariants
            for selector in invariant.get("test_selectors", [])
        ]:
            if selector.startswith("test_") and f"def {selector}(" not in path.read_text(encoding="utf-8"):
                raise FrozenContractError(f"invariant selector is missing from frozen test: {selector}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    args = parser.parse_args()
    try:
        spec = load_spec(args.spec.expanduser().resolve())
        validate_spec(spec, ROOT)
    except FrozenContractError as exc:
        print(f"bitter-lesson contract failed: {exc}")
        return 1
    print(f"bitter-lesson contract passed: {spec['spec_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
