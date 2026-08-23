from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.flayr_core import human_review as hr
from scripts.flayr_core.bd_report import write_bd_report
from scripts.flayr_core.creator_report import write_creator_report
from scripts.flayr_core.run_manifest import write_success_manifest
from scripts.flayr_core.run_state import (
    ANALYSIS_COMPLETED,
    COMPLETED,
    PROCESSING,
    REPORT_GENERATING,
    initialize_run_state,
    transition_run_state,
)
from scripts.flayr_core.utils import write_json, write_text
from scripts import review_analysis
from scripts import flayr




class HumanReviewTests(unittest.TestCase):
    def fresh_run(self):
        temp = tempfile.TemporaryDirectory()
        run_dir = Path(temp.name) / "run"
        run_dir.mkdir(parents=True)
        input_video = Path(temp.name) / "input.mp4"
        input_video.write_bytes(b"test-video")
        self._write_completed_run(run_dir, input_video)
        return temp, run_dir

    @staticmethod
    def _analysis() -> dict:
        stages = [
            {
                "stage": "S1 Hook",
                "analysis_status": "completed",
                "relation": "creator_better",
                "model_gap_magnitude": "small",
                "severity": "small",
                "judgment_reason": "达人开场更直接。",
                "creator_summary": "达人直接展示产品。",
                "benchmark_summary": "标杆先建立观看理由。",
                "creator_evidence_ids": ["C1"],
                "benchmark_evidence_ids": ["B1"],
            },
            {
                "stage": "S2 Promise",
                "analysis_status": "completed",
                "relation": "tie",
                "model_gap_magnitude": "medium",
                "severity": "medium",
                "judgment_reason": "双方都完成承诺表达。",
                "creator_summary": "达人说明产品用途。",
                "benchmark_summary": "标杆说明产品用途。",
            },
            {
                "stage": "S3 Demo",
                "analysis_status": "completed",
                "relation": "tie",
                "model_gap_magnitude": "medium",
                "severity": "medium",
                "judgment_reason": "双方都有使用过程。",
                "creator_summary": "达人展示使用过程。",
                "benchmark_summary": "标杆展示使用过程。",
            },
            {
                "stage": "S4 Effect",
                "analysis_status": "completed",
                "relation": None,
                "model_gap_magnitude": None,
                "severity": None,
                "judgment_reason": "效果证据不足。",
                "creator_summary": "没有足够效果证据。",
                "benchmark_summary": "没有足够效果证据。",
            },
            {
                "stage": "S5 Trust",
                "analysis_status": "completed",
                "relation": None,
                "model_gap_magnitude": None,
                "severity": None,
                "judgment_reason": "没有可比较的模型结论。",
                "creator_summary": "没有信任证据。",
                "benchmark_summary": "没有信任证据。",
            },
            {
                "stage": "S6 CTA",
                "analysis_status": "not_applicable",
                "relation": None,
                "model_gap_magnitude": None,
                "severity": None,
                "judgment_reason": "本样本不涉及 CTA。",
                "creator_summary": "没有购买引导。",
                "benchmark_summary": "没有购买引导。",
            },
        ]
        return {
            "mode": "compare",
            "analysis_run_state": "completed",
            "generated_at": "2026-08-23T00:00:00Z",
            "product": {"name": "测试产品", "target_market": "my"},
            "videos": {},
            "analysis_scope": {"level": "basic"},
            "one_line_verdict": "标杆和达人在部分阶段存在差异。",
            "one_line_summary": "用于人工审核测试。",
            "executive_summary": "双方证据已整理。",
            "global_diagnosis": {"findings": []},
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1", "information": "达人展示产品。"}]},
                "benchmark": {"evidence_units": [{"id": "B1", "information": "标杆展示产品。"}]},
            },
            "stage_analysis": stages,
            "improvements": [{"target_stage": "S1", "title": "旧建议", "suggestion": "补充开场。"}],
        }

    def _write_completed_run(self, run_dir: Path, input_video: Path) -> None:
        analysis = self._analysis()
        write_json(run_dir / "analysis.json", analysis)
        write_text(run_dir / "report.html", "<html>report</html>")
        write_json(run_dir / "raw_model_response.json", {"response": "ok"})
        write_json(run_dir / "validated_normalized_result.json", {"status": "ok"})
        write_json(
            run_dir / "final_derived_result.json",
            {"postprocess_provenance": {"field_sources": {"coverage": "complete", "unresolved_paths": [], "truncated": False}}},
        )
        write_json(run_dir / "postprocess_change_log.json", {"changes": []})
        write_bd_report(run_dir, analysis)
        write_creator_report(run_dir, analysis)
        initialize_run_state(run_dir, job_id="test")
        transition_run_state(run_dir, PROCESSING)
        transition_run_state(run_dir, ANALYSIS_COMPLETED)
        transition_run_state(run_dir, REPORT_GENERATING)
        transition_run_state(run_dir, COMPLETED)
        write_success_manifest(run_dir, {"input.mp4": input_video}, analysis)

    def test_init_binds_sources_and_keeps_pending_status(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        review = hr.init_review(run_dir)
        self.assertEqual(review["schema_version"], 1)
        self.assertEqual(review["review_mode"], "draft_visible")
        self.assertEqual(review["status"], "pending")
        self.assertEqual(set(review["stages"]), set(hr.STAGE_CODES))
        self.assertEqual(
            review["source"]["review_inputs"].keys(),
            {"_SUCCESS.json", "analysis.json"},
        )
        self.assertEqual(hr.review_status(review), "pending")

    def test_prediction_unavailable_cannot_be_confirmed(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        with self.assertRaises(hr.HumanReviewError):
            hr.update_stage(run_dir, "S4", "confirmed")

    def test_not_applicable_uses_code_owned_stage_skip_contract(self) -> None:
        analysis = self._analysis()
        stage = analysis["stage_analysis"][4]
        stage["analysis_status"] = "not_applicable"
        self.assertFalse(hr.model_stage_values(stage)["not_applicable"])
        stage["comparison_status"] = "not_applicable"
        self.assertFalse(hr.model_stage_values(stage)["not_applicable"])
        stage["stage_evidence_gate"] = {"status": "not_applicable", "source": "provider"}
        self.assertFalse(hr.model_stage_values(stage)["not_applicable"])
        stage["stage_evidence_gate"] = {
            "status": "not_applicable",
            "reason_code": "bilateral_stage_absent",
            "source": "code",
        }
        self.assertTrue(hr.model_stage_values(stage)["not_applicable"])

        blocked = self._analysis()["stage_analysis"][3]
        blocked["stage_evidence_gate"] = {"status": "blocked", "source": "code"}
        self.assertFalse(hr.model_stage_values(blocked)["not_applicable"])
        not_comparable = self._analysis()["stage_analysis"][3]
        not_comparable["stage_evidence_gate"] = {"status": "not_comparable", "source": "code"}
        self.assertFalse(hr.model_stage_values(not_comparable)["not_applicable"])

    def test_analysis_stage_map_rejects_missing_duplicate_and_unknown_stages(self) -> None:
        analysis = self._analysis()
        missing = json.loads(json.dumps(analysis))
        missing["stage_analysis"].pop()
        with self.assertRaises(hr.HumanReviewError):
            hr.analysis_stage_map(missing)

        duplicate = json.loads(json.dumps(analysis))
        duplicate["stage_analysis"][-1]["stage"] = "S1 Duplicate"
        with self.assertRaises(hr.HumanReviewError):
            hr.analysis_stage_map(duplicate)

        unknown = json.loads(json.dumps(analysis))
        unknown["stage_analysis"][-1]["stage"] = "S7 Unknown"
        with self.assertRaises(hr.HumanReviewError):
            hr.analysis_stage_map(unknown)

    def test_init_rejects_invalid_stage_map_before_writing_sidecar(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._analysis()
        analysis["stage_analysis"][-1]["stage"] = "S7 Unknown"
        write_json(run_dir / "analysis.json", analysis)
        with mock.patch.object(
            hr,
            "_source_fingerprint",
            return_value={"success_manifest_sha256": "x", "analysis_sha256": "y", "review_inputs": {}},
        ):
            with self.assertRaises(hr.HumanReviewError):
                hr.init_review(run_dir)
        self.assertFalse((run_dir / hr.SIDECAR_NAME).exists())

    def test_na_and_insufficient_evidence_require_note_and_no_hard_axes(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        with self.assertRaises(hr.HumanReviewError):
            hr.update_stage(run_dir, "S4", "insufficient_evidence")
        hr.update_stage(run_dir, "S4", "insufficient_evidence", note="双方关键效果证据不足")
        hr.update_stage(run_dir, "S5", "not_applicable", note="双方均未涉及信任背书")
        review = hr.load_review(run_dir)
        self.assertEqual(review["stages"]["S4"]["relation"], None)
        self.assertEqual(review["stages"]["S5"]["gap"], None)

    def test_relation_gap_consistency_is_rejected(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        with self.assertRaises(hr.HumanReviewError):
            hr.update_stage(run_dir, "S1", "corrected", relation="tie", gap="medium", note="不应成立")
        with self.assertRaises(hr.HumanReviewError):
            hr.update_stage(run_dir, "S1", "corrected", relation="benchmark_better", gap="none", note="不应成立")

    def test_history_records_before_after_and_status_is_derived(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        hr.update_stage(
            run_dir,
            "S1",
            "corrected",
            relation="benchmark_better",
            gap="medium",
            note="标杆的开场更直接。",
        )
        review = hr.load_review(run_dir)
        self.assertEqual(review["status"], "pending")
        event = review["history"][-1]
        self.assertEqual(event["before"]["decision"], "pending")
        self.assertEqual(event["after"]["decision"], "corrected")
        self.assertEqual(event["after"]["gap"], "medium")

    def test_priority_is_derived_without_sidecar_field(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        stages = {
            f"S{index}": stage for index, stage in enumerate(analysis["stage_analysis"], start=1)
        }
        self.assertEqual(hr.priority_key("S4", stages["S4"])[0], 0)
        self.assertEqual(hr.priority_key("S5", stages["S5"])[0], 0)
        self.assertEqual(hr.priority_key("S2", stages["S2"])[0], 0)
        review = hr.init_review(run_dir)
        self.assertNotIn("priority", review["stages"]["S1"])

    def _approve_all(self, run_dir: Path) -> None:
        hr.update_stage(run_dir, "S1", "confirmed")
        hr.update_stage(run_dir, "S2", "corrected", relation="tie", gap="none", note="双方钩子差距不构成可比较方向。")
        hr.update_stage(run_dir, "S3", "corrected", relation="tie", gap="none", note="双方使用过程差距不构成可比较方向。")
        hr.update_stage(run_dir, "S4", "corrected", relation="benchmark_better", gap="large", note="标杆有更完整的效果证明。")
        hr.update_stage(run_dir, "S5", "insufficient_evidence", note="双方信任证据不足，无法比较。")
        hr.update_stage(run_dir, "S6", "not_applicable", note="本样本不涉及明确 CTA。")

    def test_approved_projection_is_deterministic_and_does_not_mutate_analysis(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        before = (run_dir / "analysis.json").read_bytes()
        hr.init_review(run_dir)
        self._approve_all(run_dir)
        review = hr.load_review(run_dir)
        self.assertEqual(hr.review_status(review), "approved")
        projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(projected["review_status"], "approved")
        self.assertEqual(projected["stage_analysis"][3]["severity"], "large")
        self.assertEqual(projected["stage_analysis"][4]["gap"], "证据不足，无法比较")
        self.assertEqual(projected["stage_analysis"][5]["gap"], "未涉及")
        self.assertIn("人工复核已完成", projected["review_summary"]["verdict"])
        self.assertEqual((run_dir / "analysis.json").read_bytes(), before)

    def test_direction_change_removes_stage_recommendation(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        custom["stage_analysis"][0]["relation"] = "creator_better"
        custom["stage_analysis"][0]["model_gap_magnitude"] = "medium"
        custom["stage_analysis"][0]["severity"] = "medium"
        custom["improvements"] = [{"target_stage": "S1", "title": "旧建议"}]
        review = hr.init_review(run_dir)
        for code in hr.STAGE_CODES:
            review["stages"][code] = {"decision": "corrected", "relation": "benchmark_better", "gap": "medium", "note": "人工确认"}
        review["status"] = "approved"
        with mock.patch.object(hr, "load_analysis", return_value=custom):
            projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(projected["improvements"], [])

    def test_confirmed_tie_and_untargeted_recommendation_are_removed(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        custom["stage_analysis"][0]["relation"] = "tie"
        custom["stage_analysis"][0]["model_gap_magnitude"] = "none"
        custom["stage_analysis"][0]["severity"] = "none"
        custom["improvements"] = [
            {"target_stage": "S1", "title": "tie suggestion"},
            {"title": "no target"},
        ]
        review = hr.init_review(run_dir)
        for code in hr.STAGE_CODES:
            review["stages"][code] = {
                "decision": "corrected",
                "relation": "benchmark_better",
                "gap": "medium",
                "note": "人工确认",
            }
        review["stages"]["S1"] = {
            "decision": "confirmed",
            "relation": "tie",
            "gap": "none",
            "note": None,
        }
        review["status"] = "approved"
        with (
            mock.patch.object(hr, "load_analysis", return_value=custom),
            mock.patch.object(hr, "_validate_review_against_analysis"),
        ):
            projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(projected["improvements"], [])

    def test_same_benchmark_direction_gap_change_preserves_recommendation(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        custom["stage_analysis"][0]["relation"] = "benchmark_better"
        custom["stage_analysis"][0]["model_gap_magnitude"] = "small"
        custom["stage_analysis"][0]["severity"] = "small"
        custom["improvements"] = [{"target_stage": "S1", "title": "保留建议"}]
        review = hr.init_review(run_dir)
        for code in hr.STAGE_CODES:
            review["stages"][code] = {"decision": "corrected", "relation": "benchmark_better", "gap": "medium", "note": "人工确认"}
        review["status"] = "approved"
        with mock.patch.object(hr, "load_analysis", return_value=custom):
            projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(projected["improvements"][0]["title"], "保留建议")

    def test_preserved_recommendations_are_sorted_by_reviewed_gap(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        for stage in custom["stage_analysis"]:
            stage["relation"] = "benchmark_better"
            stage["model_gap_magnitude"] = "small"
            stage["severity"] = "small"
        custom["improvements"] = [
            {"target_stage": "S1", "title": "S1"},
            {"target_stage": "S2", "title": "S2"},
            {"target_stage": "S3", "title": "S3"},
        ]
        custom["candidate_experiments"] = [
            {"target_stage": "S1", "title": "C1"},
            {"target_stage": "S2", "title": "C2"},
            {"target_stage": "S3", "title": "C3"},
        ]
        review = hr.init_review(run_dir)
        gaps = ["medium", "large", "small"]
        for code, gap in zip(hr.STAGE_CODES, gaps + ["small", "small", "small"]):
            review["stages"][code] = {
                "decision": "corrected",
                "relation": "benchmark_better",
                "gap": gap,
                "note": "人工确认",
            }
        review["status"] = "approved"
        with mock.patch.object(hr, "load_analysis", return_value=custom):
            projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual([item["title"] for item in projected["improvements"]], ["S2", "S1", "S3"])
        self.assertEqual([item["title"] for item in projected["candidate_experiments"]], ["C2", "C1", "C3"])

    def test_reviewed_reports_have_distinct_names_and_badges(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        from scripts.flayr_core.bd_report import write_bd_report
        from scripts.flayr_core.bd_report import build_bd_report_data
        from scripts.flayr_core.creator_report import write_creator_report, build_creator_report_data
        from scripts.flayr_core.report import ReportAssetContext

        draft_temp, draft_dir = self.fresh_run()
        self.addCleanup(draft_temp.cleanup)
        draft_analysis = hr.load_analysis(draft_dir)
        self.assertEqual(build_bd_report_data(draft_analysis)["reviewStatus"], "pending")
        self.assertEqual(
            build_creator_report_data(draft_analysis, ReportAssetContext(draft_dir))["reviewStatus"],
            "pending",
        )
        draft_bd = write_bd_report(draft_dir, draft_analysis)
        draft_creator = write_creator_report(draft_dir, draft_analysis)
        self.assertIn("待人工确认，仅供复核", draft_bd.read_text(encoding="utf-8"))
        self.assertIn("待人工确认，仅供复核", draft_creator.read_text(encoding="utf-8"))
        hr.init_review(run_dir)
        self._approve_all(run_dir)
        reviewed_bd, reviewed_creator = hr.write_reviewed_reports(run_dir)
        projected = hr.project_reviewed_analysis(run_dir)
        self.assertEqual(build_bd_report_data(projected)["reviewStatus"], "approved")
        self.assertEqual(
            build_creator_report_data(projected, ReportAssetContext(run_dir))["reviewStatus"],
            "approved",
        )
        self.assertEqual(reviewed_bd.name, hr.REVIEWED_BD_REPORT_NAME)
        self.assertEqual(reviewed_creator.name, hr.REVIEWED_CREATOR_REPORT_NAME)
        self.assertIn('"reviewStatus":"approved"', reviewed_bd.read_text(encoding="utf-8"))
        self.assertIn('"reviewStatus":"approved"', reviewed_creator.read_text(encoding="utf-8"))

    def test_reviewed_report_pair_is_removed_when_creator_render_fails(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        self._approve_all(run_dir)
        (run_dir / hr.REVIEWED_BD_REPORT_NAME).write_text("old", encoding="utf-8")
        (run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).write_text("old", encoding="utf-8")
        with mock.patch(
            "scripts.flayr_core.creator_report.write_creator_report",
            side_effect=RuntimeError("creator render failed"),
        ):
            with self.assertRaises(RuntimeError):
                hr.write_reviewed_reports(run_dir)
        self.assertFalse((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertFalse((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())

    def test_cli_requires_n_for_code_owned_not_applicable_stage(self) -> None:
        analysis = self._analysis()
        stage = analysis["stage_analysis"][4]
        stage["stage_evidence_gate"] = {
            "status": "not_applicable",
            "reason_code": "bilateral_stage_absent",
            "source": "code",
        }
        with mock.patch("builtins.input", side_effect=["c", "n", "代码门禁确认双方均未涉及。"]):
            decision = review_analysis._prompt_decision(
                "S5", stage, {"decision": "pending"}
            )
        self.assertEqual(decision["decision"], "not_applicable")

    def test_cli_interrupts_then_resumes_and_blocks_unavailable_confirm(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        with mock.patch("builtins.input", side_effect=["c", "q"]):
            self.assertEqual(review_analysis.main([str(run_dir)]), 0)
        self.assertEqual(hr.review_status(hr.load_review(run_dir)), "pending")

        analysis = hr.load_analysis(run_dir)
        stages = {
            _code: stage
            for _code, stage in (
                (f"S{index}", stage)
                for index, stage in enumerate(analysis["stage_analysis"], start=1)
            )
        }
        review = hr.load_review(run_dir)
        pending = [code for code in hr.STAGE_CODES if review["stages"][code]["decision"] == "pending"]
        pending.sort(key=lambda code: hr.priority_key(code, stages[code]))
        actions: list[str] = []
        for code in pending:
            values = hr.model_stage_values(stages[code])
            if values["prediction_unavailable"]:
                actions.extend(["i", "证据不足，无法比较"])
            elif values["not_applicable"]:
                actions.extend(["n", "本阶段不适用"])
            else:
                actions.append("c")
        with mock.patch("builtins.input", side_effect=actions):
            self.assertEqual(review_analysis.main([str(run_dir)]), 0)
        self.assertEqual(hr.review_status(hr.load_review(run_dir)), "approved")
        self.assertTrue((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertTrue((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())
        with mock.patch(
            "builtins.input",
            side_effect=["S1", "e", "benchmark_better", "large", "修订后的人工判断", "q"],
        ):
            self.assertEqual(review_analysis.main([str(run_dir)]), 0)
        self.assertEqual(hr.review_status(hr.load_review(run_dir)), "approved")
        self.assertTrue((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())

    def test_rerun_cleanup_keeps_sidecar_and_removes_reviewed_reports(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        hr.update_stage(run_dir, "S1", "corrected", relation="creator_better", gap="small", note="旧审核历史")
        (run_dir / hr.REVIEWED_BD_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        (run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        flayr._prepare_explicit_run_dir(run_dir, reuse=True)
        archived = list(run_dir.glob("human_review.archived.*.json"))
        self.assertEqual(len(archived), 1)
        archived_review = json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(archived_review["history"][-1]["note"], "旧审核历史")
        self.assertFalse((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertFalse((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())
        # Simulate the next completed run's artifacts, then ensure a fresh
        # active sidecar can be initialized without deleting the archive.
        self._write_completed_run(run_dir, Path(temp.name) / "input.mp4")
        fresh = hr.init_review(run_dir)
        self.assertEqual(fresh["status"], "pending")
        self.assertTrue((run_dir / archived[0].name).exists())

    def test_rerun_unknown_file_fails_before_review_archive_or_cleanup(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        (run_dir / hr.REVIEWED_BD_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        (run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        unknown = run_dir / "unrecognized.txt"
        unknown.write_text("do not touch", encoding="utf-8")
        with self.assertRaises(SystemExit):
            flayr._prepare_explicit_run_dir(run_dir, reuse=True)
        self.assertTrue((run_dir / hr.SIDECAR_NAME).exists())
        self.assertTrue((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertTrue((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())
        self.assertTrue(unknown.exists())
        self.assertEqual(list(run_dir.glob("human_review.archived.*.json")), [])

    def test_source_change_invalidates_sidecar(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        analysis_path = run_dir / "analysis.json"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        analysis["one_line_summary"] = "changed after review"
        analysis_path.unlink()
        analysis_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_path = run_dir / "_SUCCESS.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(analysis_path.read_bytes()).hexdigest()
        manifest["artifacts"]["analysis.json"] = {"size_bytes": analysis_path.stat().st_size, "sha256": digest}
        manifest_path.unlink()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(hr.StaleHumanReviewError):
            hr.load_review(run_dir)

    def test_approved_edit_returns_pending_and_invalidates_reviewed_reports(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        self._approve_all(run_dir)
        (run_dir / hr.REVIEWED_BD_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        (run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        hr.update_stage(run_dir, "S1", "corrected", relation="benchmark_better", gap="large", note="重新确认。")
        review = hr.load_review(run_dir)
        self.assertEqual(hr.review_status(review), "pending")
        self.assertEqual(review["stages"]["S1"]["decision"], "pending")
        self.assertFalse((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertFalse((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())


if __name__ == "__main__":
    unittest.main()
