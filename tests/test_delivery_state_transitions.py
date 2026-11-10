"""Offline journeys across lifecycle, publication, review and source bindings."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import flayr, web_app
from scripts.flayr_core import human_review, requirement_review, run_state
from scripts.flayr_core.llm import pipeline
from scripts.flayr_core.llm.stage1_qualification import build_stage1_qualification_plan
from scripts.flayr_core.run_manifest import validate_success_manifest
from scripts.flayr_core.utils import write_json
from tests import test_human_review as review_fixtures
from tests.test_creator_report import ONE_PIXEL_PNG


class LifecycleTransitionMatrixTests(unittest.TestCase):
    def test_normal_edges_and_terminal_states_match_the_contract(self):
        allowed = {
            "CREATED": {"PROCESSING", "FAILED"},
            "PROCESSING": {"ANALYSIS_COMPLETED", "FAILED"},
            "ANALYSIS_COMPLETED": {
                "REPORT_GENERATING",
                "DEGRADED",
                "UNPUBLISHED",
                "FAILED",
            },
            "REPORT_GENERATING": {"COMPLETED", "DEGRADED", "FAILED"},
            "COMPLETED": set(),
            "FAILED": set(),
            "DEGRADED": set(),
            "UNPUBLISHED": set(),
        }
        paths = {
            "CREATED": (),
            "PROCESSING": ("PROCESSING",),
            "ANALYSIS_COMPLETED": ("PROCESSING", "ANALYSIS_COMPLETED"),
            "REPORT_GENERATING": (
                "PROCESSING",
                "ANALYSIS_COMPLETED",
                "REPORT_GENERATING",
            ),
            "COMPLETED": (
                "PROCESSING",
                "ANALYSIS_COMPLETED",
                "REPORT_GENERATING",
                "COMPLETED",
            ),
            "FAILED": ("PROCESSING", "FAILED"),
            "DEGRADED": ("PROCESSING", "ANALYSIS_COMPLETED", "DEGRADED"),
            "UNPUBLISHED": ("PROCESSING", "ANALYSIS_COMPLETED", "UNPUBLISHED"),
        }
        for source, targets in allowed.items():
            for target in allowed:
                with self.subTest(
                    source=source, target=target
                ), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    run_state.initialize_run_state(root, job_id="matrix")
                    for step in paths[source]:
                        run_state.transition_run_state(root, step)
                    self.assertEqual(run_state.read_run_state(root)["state"], source)
                    before = (root / run_state.RUN_STATE_FILE).read_bytes()
                    if source == target:
                        run_state.transition_run_state(root, target)
                        self.assertEqual(
                            (root / run_state.RUN_STATE_FILE).read_bytes(), before
                        )
                    elif target in targets:
                        result = run_state.transition_run_state(root, target)
                        self.assertEqual(result["state"], target)
                        self.assertEqual(result["history"][-1]["state"], target)
                    else:
                        with self.assertRaises(run_state.RunStateError):
                            run_state.transition_run_state(root, target)
                        self.assertEqual(
                            (root / run_state.RUN_STATE_FILE).read_bytes(), before
                        )

    def test_terminal_rejection_is_an_explicit_recovery_not_a_normal_edge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_state.initialize_run_state(root, job_id="recovery")
            run_state.recover_run_state(
                root, run_state.COMPLETED, reason="recovered fixture"
            )
            with self.assertRaises(run_state.RunStateError):
                run_state.transition_run_state(root, run_state.FAILED)
            rejected = run_state.recover_run_state(
                root, run_state.FAILED, reason="invalid artifacts"
            )
            self.assertTrue(rejected["history"][-1]["recovered"])
            self.assertEqual(rejected["history"][-1]["from_state"], run_state.COMPLETED)
            before = (root / run_state.RUN_STATE_FILE).read_bytes()
            with self.assertRaises(run_state.RunStateError):
                run_state.recover_run_state(root, run_state.COMPLETED)
            self.assertEqual((root / run_state.RUN_STATE_FILE).read_bytes(), before)


class DeliveryStateJourneyTests(unittest.TestCase):
    def setUp(self):
        self.helper = review_fixtures.HumanReviewTests()
        self.temp, self.run = self.helper.fresh_run()
        self.addCleanup(self.temp.cleanup)
        self.inputs = {"input.mp4": Path(self.temp.name) / "input.mp4"}

    def approve(self):
        human_review.init_review(self.run)
        self.helper._approve_all(self.run)
        self.assertEqual(human_review.load_review(self.run)["status"], "approved")

    def test_manifest_before_terminal_commit_is_not_publication_or_review_readiness(
        self,
    ):
        self.assertTrue(validate_success_manifest(self.run, self.inputs))
        marker = (self.run / "_SUCCESS.json").read_bytes()
        run_state.reset_run_state(self.run)
        run_state.begin_report_generation(self.run)
        self.assertFalse(validate_success_manifest(self.run, self.inputs))
        self.assertEqual(
            web_app._publication_status(self.run, self.inputs, returncode=0), "failed"
        )
        with self.assertRaises(human_review.HumanReviewError):
            human_review.init_review(self.run)
        self.assertFalse((self.run / human_review.SIDECAR_NAME).exists())

        run_state.transition_run_state(self.run, run_state.COMPLETED)
        self.assertEqual((self.run / "_SUCCESS.json").read_bytes(), marker)
        self.assertTrue(validate_success_manifest(self.run, self.inputs))
        self.assertEqual(
            web_app._publication_status(self.run, self.inputs, returncode=0),
            "completed",
        )
        self.assertEqual(human_review.init_review(self.run)["status"], "pending")

    def test_failed_lifecycle_overrides_existing_manifest_and_report_pair(self):
        self.assertTrue(validate_success_manifest(self.run, self.inputs))
        run_state.recover_run_state(
            self.run, run_state.FAILED, reason="publication rejected"
        )
        self.assertTrue((self.run / "_SUCCESS.json").is_file())
        self.assertTrue(web_app.report_variants_ready(self.run))
        self.assertFalse(validate_success_manifest(self.run, self.inputs))
        for options in ({"returncode": 0}, {"recovery": True}):
            with self.subTest(options=options):
                self.assertEqual(
                    web_app._publication_status(self.run, self.inputs, **options),
                    "failed",
                )
        with self.assertRaises(human_review.HumanReviewError):
            human_review.init_review(self.run)

    def test_analysis_only_cannot_be_promoted_by_leftover_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_state.initialize_run_state(root)
            run_state.transition_run_state(root, run_state.PROCESSING)
            analysis = {
                "analysis_run_state": "completed",
                "segmented_pipeline": {
                    "candidate_status": "completed",
                    "synthesis_status": "completed",
                    "unresolved_stages": [],
                    "stage_groups": [
                        {"group": group, "status": "completed"}
                        for group in (["S1", "S2"], ["S3", "S4"], ["S5"], ["S6"])
                    ],
                },
            }
            for name in ("analysis.json", "analysis_result.json"):
                write_json(root / name, analysis)
            flayr._finish_analysis_only(root, analysis)
            for name in ("bd_report.html", "creator_report.html"):
                (root / name).write_text("<html>leftover</html>", encoding="utf-8")
            self.assertTrue(web_app.report_variants_ready(root))
            self.assertEqual(
                run_state.read_run_state(root)["state"], run_state.UNPUBLISHED
            )
            self.assertFalse(validate_success_manifest(root))
            self.assertEqual(
                web_app._publication_status(root, {}, recovery=True), "failed"
            )
            with self.assertRaises(human_review.HumanReviewError):
                human_review.init_review(root)
            execution = json.loads(
                (root / "execution_status.json").read_text(encoding="utf-8")
            )
            self.assertFalse(execution["review_source_ready"])
            self.assertFalse((root / "_SUCCESS.json").exists())

    def test_stage_edit_revokes_reviewed_outputs_not_the_original_model_draft(self):
        source = {
            name: (self.run / name).read_bytes()
            for name in ("analysis.json", "_SUCCESS.json")
        }
        self.approve()
        reviewed = human_review.write_reviewed_reports(self.run)
        self.assertTrue(all(path.is_file() for path in reviewed))

        human_review.update_stage(
            self.run, "S1", "corrected", relation="tie", gap="none", note="Recheck"
        )
        self.assertEqual(human_review.load_review(self.run)["status"], "pending")
        self.assertTrue(all(not path.exists() for path in reviewed))
        with self.assertRaises(human_review.HumanReviewError):
            human_review.project_reviewed_analysis(self.run)
        self.assertEqual(
            source, {name: (self.run / name).read_bytes() for name in source}
        )
        self.assertTrue(validate_success_manifest(self.run, self.inputs))
        self.assertEqual(
            web_app._publication_status(self.run, self.inputs, returncode=0),
            "completed",
        )

        human_review.update_stage(
            self.run, "S1", "corrected", relation="tie", gap="none", note="Rechecked"
        )
        self.assertEqual(human_review.load_review(self.run)["status"], "approved")
        self.assertTrue(
            all(
                path.is_file() for path in human_review.write_reviewed_reports(self.run)
            )
        )

    def test_new_valid_model_publication_does_not_inherit_old_human_confirmation(self):
        self.approve()
        review_before = (self.run / human_review.SIDECAR_NAME).read_bytes()
        analysis = human_review.load_analysis(self.run)
        analysis["one_line_summary"] = "Updated source version"
        self.helper._replace_run_analysis(self.temp, self.run, analysis)
        self.assertTrue(validate_success_manifest(self.run, self.inputs))
        self.assertEqual(
            web_app._publication_status(self.run, self.inputs, returncode=0),
            "completed",
        )
        with self.assertRaises(human_review.StaleHumanReviewError):
            human_review.load_review(self.run)
        self.assertEqual(
            (self.run / human_review.SIDECAR_NAME).read_bytes(), review_before
        )

    def test_requirement_notes_and_changed_requirements_do_not_rewrite_stage_approval(
        self,
    ):
        analysis = human_review.load_analysis(self.run)
        media = self.run / "creator"
        media.mkdir()
        frame = media / "frame.png"
        frame.write_bytes(ONE_PIXEL_PNG)
        analysis["videos"]["creator"] = {
            "path": str(self.inputs["input.mp4"]),
            "duration_seconds": 12,
            "transcription_status": "completed",
            "work_dir": str(media),
            "analysis_frames": [{"path": str(frame), "timestamp_seconds": 3}],
        }
        analysis["video_understanding"]["creator"]["evidence_units"][0][
            "time_range"
        ] = [2, 4]
        self.helper._replace_run_analysis(self.temp, self.run, analysis)
        self.approve()
        review_before = (self.run / human_review.SIDECAR_NAME).read_bytes()
        source = Path(self.temp.name) / "requirements.txt"
        source.write_text("Show the product.", encoding="utf-8")
        requirements = Path(self.temp.name) / "requirements.json"
        write_json(
            requirements,
            {
                "id": "shoot",
                "version": "1",
                "approved_by": "operator",
                "requirements": [
                    {
                        "id": "R1",
                        "text": "Show the product.",
                        "source_type": "user_provided",
                        "source_path": "requirements.txt",
                        "source_locator": {
                            "kind": "quote",
                            "value": "Show the product.",
                        },
                    }
                ],
            },
        )
        links = Path(self.temp.name) / "links.json"
        write_json(links, {"R1": ["C1"]})
        packet_path = Path(self.temp.name) / "requirements-review.json"
        requirement_review.init_packet(self.run, requirements, links, packet_path)
        packet = requirement_review.record_note(
            packet_path, "R1", "operator", "Review this candidate."
        )
        self.assertEqual(len(packet["human_notes"]), 1)
        self.assertNotIn("status", packet["rows"][0])

        source.write_text(
            "Show the product.\nThen show the connector.", encoding="utf-8"
        )
        packet_before = packet_path.read_bytes()
        with self.assertRaises(requirement_review.RequirementReviewError):
            requirement_review.record_note(
                packet_path, "R1", "operator", "Must not append to stale packet."
            )
        self.assertEqual(packet_path.read_bytes(), packet_before)
        self.assertEqual(
            (self.run / human_review.SIDECAR_NAME).read_bytes(), review_before
        )
        self.assertEqual(human_review.load_review(self.run)["status"], "approved")


class AnalysisCompletionTests(unittest.TestCase):
    def test_candidate_completion_cannot_override_final_outcomes(self):
        base = {
            "stage2_candidate_status": "completed",
            "stage_analysis": [
                {
                    "stage": f"S{i}",
                    "analysis_status": "grounded",
                    "stage_state": "completed",
                    "model_gap_magnitude": "none",
                }
                for i in range(1, 7)
            ],
            "segmented_pipeline": {
                "candidate_status": "completed",
                "synthesis_status": "completed",
                "stage_groups": [
                    {"group": group, "status": "completed"}
                    for group in (["S1", "S2"], ["S3", "S4"], ["S5"], ["S6"])
                ],
            },
        }
        for case, expected in (
            ("completed", "completed"),
            ("closed_na", "completed"),
            ("unknown_stage", "degraded"),
            ("failed_group", "degraded"),
            ("missing_group", "degraded"),
            ("failed_synthesis", "degraded"),
            ("failed_candidate", "failed"),
        ):
            with self.subTest(case=case):
                result = copy.deepcopy(base)
                if case in {"closed_na", "unknown_stage"}:
                    result["stage_analysis"][3].update(
                        analysis_status="unknown", stage_state="unknown"
                    )
                    if case == "closed_na":
                        result["stage_analysis"][3][
                            "comparison_status"
                        ] = "not_applicable"
                elif case == "failed_group":
                    result["segmented_pipeline"]["stage_groups"][0]["status"] = "failed"
                elif case == "missing_group":
                    result["segmented_pipeline"]["stage_groups"].pop()
                elif case == "failed_synthesis":
                    result["segmented_pipeline"]["synthesis_status"] = "failed"
                elif case == "failed_candidate":
                    result["stage2_candidate_status"] = "failed"
                status, unresolved = pipeline._refresh_segmented_pipeline_status(result)
                self.assertEqual(status, expected)
                self.assertEqual(result["stage2_pipeline_status"], expected)
                self.assertEqual(result["segmented_pipeline"]["status"], expected)
                self.assertEqual(unresolved, ["S4"] if case == "unknown_stage" else [])


class RecoveryAllowanceTests(unittest.TestCase):
    def test_consumed_recovery_does_not_restart_when_stages_remain_unknown(self):
        for status in ("focused_recovery", "focused_recovery_with_unresolved"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                facts = {
                    "evidence_units": [
                        {
                            "id": "C1",
                            "time_range": [0, 1],
                            "visual_fact": "Product visible",
                        }
                    ],
                    "stage_evidence_checks": [
                        {"stage": "S3", "status": "unknown", "evidence_ids": []}
                    ],
                    "stage1_recovery": {
                        "source": "pipeline",
                        "status": status,
                        "target_stages": ["S3"],
                        "unresolved_stages": (
                            ["S3"] if status.endswith("unresolved") else []
                        ),
                    },
                }
                original = copy.deepcopy(facts)
                with mock.patch.object(
                    pipeline, "_prepare_stage1_recovery_request"
                ) as prepare, mock.patch.object(
                    pipeline, "provider_call_with_artifact"
                ) as provider:
                    for _ in range(2):
                        result = pipeline._maybe_recover_video_facts(
                            SimpleNamespace(llm_dry_run=False),
                            {},
                            root,
                            "",
                            "creator",
                            facts,
                        )
                        self.assertIs(result, facts)
                    prepare.assert_not_called()
                    provider.assert_not_called()
                self.assertEqual(facts, original)
                self.assertEqual(list(root.iterdir()), [])

    def test_focused_qualification_preserves_failure_outside_its_targets(self):
        facts = {
            "evidence_units": [{"id": "C1"}],
            "stage1_qualification": {
                "status": "completed",
                "failed_stage_codes": ["S3", "S6"],
                "group_records": [{"group": ["S1", "S2"], "status": "completed"}],
            },
        }
        original = copy.deepcopy(facts)
        plan = build_stage1_qualification_plan(facts, ["S3"])
        self.assertEqual(plan.provider_phase, "D")
        self.assertEqual(plan.groups_to_run, [["S3"]])
        self.assertEqual(plan.failed_stage_codes, ["S6"])
        self.assertEqual(plan.prior_failed_stage_codes, {"S3", "S6"})
        self.assertEqual(facts, original)


if __name__ == "__main__":
    unittest.main()
