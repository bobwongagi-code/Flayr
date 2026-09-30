"""Focused regressions for deterministic segmented Stage2 projection."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.stage2_projection import (  # noqa: E402
    _SEGMENTED_FLAG_REQUIRED_KEYS,
    _normalize_segmented_stage,
)
from flayr_core.llm.payload import (  # noqa: E402
    build_llm_payload,
    build_stage_group_judgment_payload,
)
from flayr_core.postprocess.validate import validate_s4_effect_flags  # noqa: E402
from flayr_core.stage_contract_registry import S3_EXPRESSION_PROMPT  # noqa: E402
from flayr_core.stage_evidence_contracts import (  # noqa: E402
    STAGE1_ACQUISITION_VERSION,
    STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
    STAGE1_COVERAGE_AUDIT_VERSION,
    STAGE_EVIDENCE_CONTRACT_VERSION,
    freeze_stage_evidence,
    stage_codes,
    stage_evidence_contract,
)


def _signal_bindings(stage: str, evidence_id: str) -> dict[str, object]:
    return {
        signal: {
            "status": "supported",
            "evidence_ids": [evidence_id],
            "invalid_evidence_ids": [],
            "reason": "fixture binding",
        }
        for signal in stage_evidence_contract(stage).required_signals
    }


def _side(role_code: str, s4_status: str) -> dict[str, object]:
    evidence_id = f"{role_code}4"
    s4_contract = stage_evidence_contract("S4")
    checks: list[dict[str, object]] = []
    for stage in stage_codes():
        if stage != "S4":
            checks.append(
                {
                    "stage": stage,
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "signal_bindings": {},
                }
            )
            continue
        if s4_status == "present":
            checks.append(
                {
                    "stage": stage,
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": [evidence_id],
                    "observed_signals": list(s4_contract.required_signals),
                    "missing_signals": [],
                    "signal_bindings": _signal_bindings(stage, evidence_id),
                    "evidence_strength": "direct",
                }
            )
        elif s4_status == "absent":
            checks.append(
                {
                    "stage": stage,
                    "status": "absent",
                    "coverage": "complete",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": list(s4_contract.required_signals),
                    "signal_bindings": {},
                    "observed_disqualifiers": [s4_contract.disqualifiers[0]],
                    "evidence_strength": "absent",
                }
            )
        else:
            checks.append(
                {
                    "stage": stage,
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "signal_bindings": {},
                }
            )

    audit_stages = {
        stage: {
            "status": "unknown",
            "coverage": "unknown",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": [],
            "signal_bindings": {},
        }
        for stage in stage_codes()
    }
    if s4_status == "present":
        audit_stages["S4"] = {
            "status": "found",
            "coverage": "complete",
            "evidence_ids": [evidence_id],
            "observed_signals": list(s4_contract.required_signals),
            "missing_signals": [],
            "signal_bindings": _signal_bindings("S4", evidence_id),
        }
    elif s4_status == "absent":
        audit_stages["S4"] = {
            "status": "clear",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(s4_contract.required_signals),
            "signal_bindings": {},
        }

    side: dict[str, object] = {
        "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
        "stage1_acquisition": {
            "version": STAGE1_ACQUISITION_VERSION,
            "source": "pipeline",
            "status": "complete",
            "input_mode": "native_video",
            "speech_mode": "visual_driven",
            "duration_seconds": 6.0,
            "channels": {
                "visual": {"status": "ready", "coverage": "full", "count": 6, "boundary_precision": "frame"},
                "voiceover": {"status": "unknown", "coverage": "unknown", "count": 0},
                "subtitle": {"status": "unknown", "coverage": "unknown", "count": 0},
                "audio": {"status": "ready", "coverage": "full", "count": 1},
            },
            "stage_coverage": {stage: {"status": "observed", "count": 1} for stage in stage_codes()},
            "visual_input_timestamps": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
            "native_video_windows": [{"start_seconds": 0.0, "end_seconds": 6.0}],
            "errors": [],
        },
        "stage_evidence_checks": checks,
        "stage1_coverage_audit": {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "pipeline",
            "status": "completed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "stages": audit_stages,
            "errors": [],
        },
        "evidence_units": (
            [{"id": evidence_id, "time_range": "3.0s - 4.0s", "evidence_strength": "direct", "visual_fact": "S4 观察事实"}]
            if s4_status == "present"
            else []
        ),
    }
    freeze_stage_evidence(side)
    return side


def _provider_s4_flag(role_code: str) -> dict[str, object]:
    flag = {key: False for key in _SEGMENTED_FLAG_REQUIRED_KEYS["S4"]}
    flag.update(
        {
            "effect_type": "before_after",
            "effect_evidence_state": "uncertain",
            "effect_salience": "clear",
            "effect_reason": "provider judgment",
            "evidence_ids": [f"{role_code}4"],
            "proposition_ids": [],
        }
    )
    return flag


def _provider_s3_flag(role_code: str) -> dict[str, object]:
    flag = {key: False for key in _SEGMENTED_FLAG_REQUIRED_KEYS["S3"]}
    flag.update(
        {
            "module_type": "unknown",
            "usage_evidence_state": "none",
            "scene_mode": "unknown",
            "presentation_overlays": ["none"],
            "start_seconds": 0,
            "end_seconds": 0,
            "usage_reason": "provider judgment",
            "evidence_ids": [f"{role_code}3"],
            "proposition_ids": [],
        }
    )
    return flag


def _raw_s4() -> dict[str, object]:
    return {
        "stage": "S4",
        "stage_state": "completed",
        "relation": "creator_better",
        "model_gap_magnitude": "medium",
        "benchmark_evidence_ids": ["B4"],
        "creator_evidence_ids": ["C4"],
        "benchmark_s4": _provider_s4_flag("B"),
        "creator_s4": _provider_s4_flag("C"),
        "judgment_reason": "provider judgment",
    }


def _raw_s3() -> dict[str, object]:
    return {
        "stage": "S3",
        "stage_state": "completed",
        "relation": "creator_better",
        "model_gap_magnitude": "medium",
        "benchmark_evidence_ids": ["B3"],
        "creator_evidence_ids": ["C3"],
        "benchmark_s3": _provider_s3_flag("B"),
        "creator_s3": _provider_s3_flag("C"),
        "judgment_reason": "provider judgment",
    }


def _mark_absent(side: dict[str, object], stage: str) -> dict[str, object]:
    contract = stage_evidence_contract(stage)
    check = next(item for item in side["stage_evidence_checks"] if item["stage"] == stage)
    check.update(
        {
            "status": "absent",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(contract.required_signals),
            "signal_bindings": {},
            "observed_disqualifiers": [contract.disqualifiers[0]],
            "evidence_strength": "absent",
        }
    )
    audit = side["stage1_coverage_audit"]["stages"][stage]
    audit.update(
        {
            "status": "clear",
            "coverage": "complete",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": list(contract.required_signals),
            "signal_bindings": {},
        }
    )
    freeze_stage_evidence(side)
    return side


class Stage2ProjectionRegressionTests(unittest.TestCase):
    def test_s3_candidate_wording_is_not_injected_and_baseline_is_reachable(self) -> None:
        candidate_only = (
            "S3 比较功能与使用表达的整体效果",
            "双方讲解相当不代表整体相当",
            "演示是否清楚增加了对产品功能、用法、适用情境或操作过程的理解",
            "没有有效理解增益不加档",
            "有证据支持的增益按其实际作用判断差距",
            "演示形式或动作的存在本身不自动更优，也不自动触发 medium/large",
        )
        for stages in (["S3"], ["S3", "S4"]):
            with self.subTest(stages=stages):
                payload = build_stage_group_judgment_payload("test", "", {}, {}, stages)
                text = payload["messages"][1]["content"][0]["text"]
                self.assertIn(S3_EXPRESSION_PROMPT, text)
                for clause in candidate_only:
                    self.assertNotIn(clause, text)

        full_payload = build_llm_payload("test", "input", [])
        full_text = full_payload["messages"][0]["content"]
        self.assertIn(S3_EXPRESSION_PROMPT, full_text)
        for clause in candidate_only:
            self.assertNotIn(clause, full_text)

    def test_stage_group_prompt_defines_s4_effect_evidence_state(self) -> None:
        s4_payload = build_stage_group_judgment_payload("test", "", {}, {}, ["S4"])
        s4_text = s4_payload["messages"][1]["content"][0]["text"]

        self.assertIn(
            "effect_evidence_state 四态定义：none=没有效果证据；result_only=只有结果图或结果叙述没有因果桥；"
            "verified=效果可见且归因/过程可信；uncertain=证据冲突或不足",
            s4_text,
        )
        self.assertIn(
            "当 effect_evidence_state=result_only 时，result_only_without_process 必须为 true，"
            "process_linked_effect 必须为 false",
            s4_text,
        )
        self.assertIn("当前 S4 效果与产品操作之间的因果关系，不能用 S3 有动作替代", s4_text)

        s3_payload = build_stage_group_judgment_payload("test", "", {}, {}, ["S3"])
        s3_text = s3_payload["messages"][1]["content"][0]["text"]
        self.assertNotIn("当前 S4 效果与产品操作之间的因果关系，不能用 S3 有动作替代", s3_text)
        self.assertNotIn("effect_evidence_state 四态定义：", s3_text)

    def test_blocked_absent_unknown_is_idempotent_without_either_flag(self) -> None:
        for absent_role in ("benchmark", "creator"):
            with self.subTest(absent_role=absent_role):
                statuses = {"benchmark": "unknown", "creator": "unknown"}
                statuses[absent_role] = "absent"
                facts = {role: _side(role[0].upper(), status) for role, status in statuses.items()}

                first = _normalize_segmented_stage(_raw_s4(), "S4", facts)
                second = _normalize_segmented_stage(copy.deepcopy(first), "S4", facts)

                self.assertEqual(first, second)
                self.assertEqual(first["stage_handoff_status"], "evidence_blocked")
                self.assertNotIn("benchmark_s4", first)
                self.assertNotIn("creator_s4", first)

                result = {"video_understanding": facts, "stage_analysis": [{}, {}, {}, first]}
                validate_s4_effect_flags(
                    result,
                    {"s4_flags_required": False, "evidence_state_required": False},
                )
                result["stage_analysis"][3] = second
                validate_s4_effect_flags(
                    result,
                    {"s4_flags_required": False, "evidence_state_required": False},
                )

    def test_grounded_missing_flags_remain_missing(self) -> None:
        facts = {role: _side(role[0].upper(), "present") for role in ("benchmark", "creator")}
        raw = _raw_s4()
        raw.pop("creator_s4")

        projected = _normalize_segmented_stage(raw, "S4", facts)

        self.assertEqual(projected["stage_handoff_status"], "grounded")
        self.assertIn("benchmark_s4", projected)
        self.assertNotIn("creator_s4", projected)

        result = {"video_understanding": facts, "stage_analysis": [{}, {}, {}, projected]}
        with self.assertRaisesRegex(SystemExit, "S4 缺少 creator_s4"):
            validate_s4_effect_flags(
                result,
                {"s4_flags_required": False, "evidence_state_required": False},
            )

    def test_result_only_requires_unlinked_effect_flags(self) -> None:
        creator_flag = _provider_s4_flag("C")
        creator_flag.update(
            {
                "effect_type": "before_after",
                "effect_evidence_state": "result_only",
                "effect_visible": True,
                "effect_salience": "clear",
                "result_only_without_process": True,
                "process_linked_effect": False,
            }
        )
        result = {
            "video_understanding": {},
            "stage_analysis": [
                {},
                {},
                {},
                {"creator_s4": creator_flag, "benchmark_s4": _provider_s4_flag("B")},
            ],
        }
        analysis = {"s4_flags_required": True, "evidence_state_required": False}

        validate_s4_effect_flags(result, analysis)

        for conflict, value, error in (
            (
                "process_linked_effect",
                True,
                "S4 creator_s4.process_linked_effect 与 result_only 冲突",
            ),
            (
                "result_only_without_process",
                False,
                "S4 creator_s4.result_only_without_process 与 result_only 冲突",
            ),
        ):
            with self.subTest(conflict=conflict):
                conflicted = copy.deepcopy(result)
                creator = conflicted["stage_analysis"][3]["creator_s4"]
                creator[conflict] = value
                with self.assertRaisesRegex(SystemExit, error):
                    validate_s4_effect_flags(conflicted, analysis)
                self.assertEqual(creator[conflict], value)

    def test_bilateral_absence_keeps_code_owned_flags_across_projection(self) -> None:
        facts = {role: _side(role[0].upper(), "absent") for role in ("benchmark", "creator")}

        first = _normalize_segmented_stage(_raw_s4(), "S4", facts)
        second = _normalize_segmented_stage(copy.deepcopy(first), "S4", facts)

        self.assertEqual(first, second)
        self.assertEqual(first["stage_handoff_status"], "not_applicable")
        for role in ("benchmark", "creator"):
            flag = first[f"{role}_s4"]
            self.assertEqual(flag["effect_evidence_state"], "none")
            self.assertEqual(flag["evidence_ids"], [])
            self.assertEqual(flag["proposition_ids"], [])

        validate_s4_effect_flags(
            {"video_understanding": facts, "stage_analysis": [{}, {}, {}, first]},
            {"s4_flags_required": False, "evidence_state_required": False},
        )

    def test_blocked_s3_absent_unknown_is_idempotent_without_either_flag(self) -> None:
        for absent_role in ("benchmark", "creator"):
            with self.subTest(absent_role=absent_role):
                statuses = {"benchmark": "unknown", "creator": "unknown"}
                statuses[absent_role] = "absent"
                facts: dict[str, dict[str, object]] = {}
                for role, status in statuses.items():
                    side = _side(role[0].upper(), "unknown")
                    facts[role] = _mark_absent(side, "S3") if status == "absent" else side

                first = _normalize_segmented_stage(_raw_s3(), "S3", facts)
                second = _normalize_segmented_stage(copy.deepcopy(first), "S3", facts)

                self.assertEqual(first, second)
                self.assertEqual(first["stage_handoff_status"], "evidence_blocked")
                self.assertNotIn("benchmark_s3", first)
                self.assertNotIn("creator_s3", first)


if __name__ == "__main__":
    unittest.main()
