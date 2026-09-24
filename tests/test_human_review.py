from __future__ import annotations

import copy
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

    def _replace_run_analysis(self, temp: tempfile.TemporaryDirectory, run_dir: Path, analysis: dict) -> None:
        write_json(run_dir / "analysis.json", analysis)
        write_bd_report(run_dir, analysis)
        write_creator_report(run_dir, analysis)
        write_success_manifest(run_dir, {"input.mp4": Path(temp.name) / "input.mp4"}, analysis)

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

    def _project_custom_analysis(
        self,
        run_dir: Path,
        analysis: dict,
        decisions: dict[str, dict],
    ) -> dict:
        review = hr.init_review(run_dir)
        for code, decision in decisions.items():
            review["stages"][code] = decision
        review["status"] = "approved"
        with (
            mock.patch.object(hr, "load_analysis", return_value=analysis),
            mock.patch.object(hr, "_validate_review_against_analysis"),
        ):
            return hr.project_reviewed_analysis(run_dir, review)

    @staticmethod
    def _valid_comparison_analysis() -> dict:
        analysis = HumanReviewTests._analysis()
        axes = [
            ("creator_better", "small"),
            ("tie", "none"),
            ("benchmark_better", "medium"),
            ("benchmark_better", "small"),
            ("creator_better", "small"),
            ("benchmark_better", "small"),
        ]
        for stage, (relation, gap) in zip(analysis["stage_analysis"], axes):
            stage["analysis_status"] = "completed"
            stage["relation"] = relation
            stage["model_gap_magnitude"] = gap
            stage["severity"] = gap
            stage["judgment_reason"] = f"原始 {stage['stage']} 理由。"
        return analysis

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
        self.assertEqual(projected["stage_analysis"][4]["gap"], "双方信任证据不足，无法比较。")
        self.assertEqual(projected["stage_analysis"][5]["gap"], "本样本不涉及明确 CTA。")
        self.assertIn("人工逐阶段结论", projected["review_summary"]["verdict"])
        self.assertEqual((run_dir / "analysis.json").read_bytes(), before)

    def test_direction_change_demotes_stage_recommendation_to_candidate(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        custom["stage_analysis"][0]["relation"] = "creator_better"
        custom["stage_analysis"][0]["model_gap_magnitude"] = "medium"
        custom["stage_analysis"][0]["severity"] = "medium"
        custom["improvements"] = [{"target_stage": "S1", "title": "旧建议", "suggestion": "展示售价39.9元。"}]
        review = hr.init_review(run_dir)
        for code in hr.STAGE_CODES:
            review["stages"][code] = {"decision": "corrected", "relation": "benchmark_better", "gap": "medium", "note": "人工确认"}
        review["status"] = "approved"
        with mock.patch.object(hr, "load_analysis", return_value=custom):
            projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(len(projected["improvements"]), 1)
        candidate = projected["improvements"][0]
        self.assertEqual(candidate["status"], "needs_human_confirmation")
        self.assertEqual(candidate["suggestion"], "展示售价39.9元。")
        self.assertNotEqual(candidate["title"], "旧建议")
        self.assertNotIn("benchmark_reference", candidate)

    def test_confirmed_tie_recommendation_stays_and_untargeted_action_is_pending(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        custom["stage_analysis"][0]["relation"] = "tie"
        custom["stage_analysis"][0]["model_gap_magnitude"] = "none"
        custom["stage_analysis"][0]["severity"] = "none"
        custom["improvements"] = [
            {"target_stage": "S1", "title": "tie suggestion", "action": "展示使用动作。"},
            {"title": "no target", "action": "调整画面节奏。"},
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
        self.assertEqual(projected["improvements"][0]["title"], "tie suggestion")
        self.assertNotIn("status", projected["improvements"][0])
        self.assertEqual(projected["improvements"][1]["status"], "needs_human_confirmation")
        self.assertNotEqual(projected["improvements"][1]["title"], "no target")

    def test_same_benchmark_direction_gap_change_demotes_old_recommendation(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        custom["stage_analysis"][0]["relation"] = "benchmark_better"
        custom["stage_analysis"][0]["model_gap_magnitude"] = "small"
        custom["stage_analysis"][0]["severity"] = "small"
        custom["improvements"] = [{"target_stage": "S1", "title": "旧建议", "actions": ["清楚展示产品操作。"]}]
        review = hr.init_review(run_dir)
        for code in hr.STAGE_CODES:
            review["stages"][code] = {"decision": "corrected", "relation": "benchmark_better", "gap": "medium", "note": "人工确认"}
        review["status"] = "approved"
        with mock.patch.object(hr, "load_analysis", return_value=custom):
            projected = hr.project_reviewed_analysis(run_dir, review)
        candidate = projected["improvements"][0]
        self.assertEqual(candidate["status"], "needs_human_confirmation")
        self.assertEqual(candidate["actions"], ["清楚展示产品操作。"])
        self.assertNotEqual(candidate["title"], "旧建议")

    def test_changed_stage_recommendations_remain_pending_in_source_order(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        custom = json.loads(json.dumps(analysis))
        for stage in custom["stage_analysis"]:
            stage["relation"] = "benchmark_better"
            stage["model_gap_magnitude"] = "small"
            stage["severity"] = "small"
        custom["improvements"] = [
            {"target_stage": "S1", "title": "S1", "action": "S1 动作"},
            {"target_stage": "S2", "title": "S2", "action": "S2 动作"},
            {"target_stage": "S3", "title": "S3", "action": "S3 动作"},
        ]
        custom["candidate_experiments"] = [
            {"target_stage": "S1", "title": "C1", "action": "C1 动作"},
            {"target_stage": "S2", "title": "C2", "action": "C2 动作"},
            {"target_stage": "S3", "title": "C3", "action": "C3 动作"},
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
        self.assertEqual([item["target_stage"] for item in projected["improvements"]], ["S1", "S2", "S3"])
        self.assertTrue(all(item["status"] == "needs_human_confirmation" for item in projected["improvements"]))
        self.assertEqual([item["target_stage"] for item in projected["candidate_experiments"]], ["S1", "S2", "S3"])
        self.assertTrue(all(item["status"] == "needs_human_confirmation" for item in projected["candidate_experiments"]))

    def test_changed_review_replaces_stale_summaries_and_demotes_bound_and_unbound_advice(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        analysis["stage_analysis"][0]["relation"] = "benchmark_better"
        analysis["stage_analysis"][0]["model_gap_magnitude"] = "small"
        analysis["stage_analysis"][0]["severity"] = "small"
        analysis["stage_analysis"][0]["judgment_reason"] = "旧方向为标杆开场更强。"
        analysis["one_line_verdict"] = "旧商业判断。"
        analysis["one_line_summary"] = "旧摘要。"
        analysis["executive_summary"] = "旧详细分析。"
        analysis["commercial_priority_summary"] = "旧商业优先级。"
        analysis["global_diagnosis"] = {"findings": [{"summary": "旧全局结论"}]}
        analysis["highlights"] = [{"text": "旧全局亮点"}]
        analysis["content_intent"] = "旧内容意图"
        analysis["retained_points"] = ["旧保留点"]
        analysis["stage_analysis"][0].update(
            {
                "reference_relevance": "旧标杆相关性",
                "reference_observation": "旧标杆观察",
                "benchmark_reference": "旧标杆建议依据",
                "communication_strategy": "旧沟通建议",
                "comparison_reason": "旧模型差距理由",
                "confidence_note": "旧置信说明",
                "retain": "旧保留判断",
                "creator_retain": "旧创作者保留判断",
                "creator_retention": "旧保留理由",
                "linked_experiment_id": "old-exp-1",
            }
        )
        analysis["stage_analysis"][1]["linked_experiment_id"] = "old-exp-2"
        analysis["stage_analysis"][3].update(
            {
                "reference_relevance": "旧 S4 标杆相关性",
                "benchmark_reference": "旧 S4 标杆依据",
                "communication_advice": "旧 S4 沟通建议",
            }
        )
        analysis["improvements"] = [
            {
                "target_stage": "S1",
                "title": "旧开场建议",
                "problem": "旧差距判断",
                "hypothesis": "旧验证假设",
                "benchmark_reference": "旧标杆对比",
                "benchmark_evidence_ids": ["B1"],
                "benchmark_time_range": "00:03-00:05",
                "expected_effect": "预计GMV提升20%",
                "gmv_impact": "提升20%",
                "creator_script": "旧脚本",
                "actions": ["展示售价39.9元。"],
            },
            {
                "target_stage": "S4",
                "title": "旧效果建议",
                "problem": "旧效果差距",
                "benchmark_reference": "旧效果标杆证据",
                "expected_effect": "带来更多销量",
                "actions": ["把充电画面放到前段。", "预计点击率提升20%。"],
            },
            {"title": "旧未绑定建议", "suggestion": "复拍产品操作。"},
            {"target_stage": "S2/S4", "title": "多阶段建议", "benchmark_reference": "旧对比依据", "action": "重拍关键画面。"},
            {"target_stage": "S2", "title": "已确认同阶段建议", "action": "字幕显示原价39元。", "expected_effect": "预计销量增加3倍。"},
        ]
        analysis["candidate_experiments"] = [
            {
                "target_stage": "S1",
                "title": "旧候选实验",
                "observation": "旧观察",
                "problem": "旧问题",
                "hypothesis": "旧假设",
                "benchmark_reference": "旧候选依据",
                "benchmark_evidence_ids": ["B1"],
                "expected_effect": "预计转化率提升10%",
                "experiment_id": "stable-exp-1",
                "action": "标注售价39元。",
            }
        ]
        before = (run_dir / "analysis.json").read_bytes()
        decisions = {
            "S1": {"decision": "corrected", "relation": "creator_better", "gap": "medium", "note": "人工复核后达人开场更清晰。"},
            "S2": {"decision": "confirmed", "relation": "tie", "gap": "none", "note": None},
            "S3": {"decision": "confirmed", "relation": "benchmark_better", "gap": "medium", "note": None},
            "S4": {"decision": "not_applicable", "relation": None, "gap": None, "note": "本轮不比较效果呈现。"},
            "S5": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S6": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
        }

        projected = self._project_custom_analysis(run_dir, analysis, decisions)

        self.assertEqual(projected["review_delivery"]["summary_status"], "replaced_after_stage_review")
        self.assertEqual(projected["review_delivery"]["changed_stages"], ["S1", "S4"])
        self.assertEqual(projected["one_line_verdict"], projected["review_summary"]["verdict"])
        self.assertEqual(projected["executive_summary"], projected["review_summary"]["detail"])
        self.assertNotIn("旧商业", projected["one_line_verdict"])
        self.assertNotIn("one_line_summary", projected)
        self.assertNotIn("commercial_priority_summary", projected)
        self.assertNotIn("global_diagnosis", projected)
        self.assertIn("原商业总结未随确认更新，已停用", projected["executive_summary"])
        self.assertIn("原有建议并未随阶段确认自动获批", projected["executive_summary"])
        self.assertIn("人工复核后达人开场更清晰", projected["one_line_verdict"])
        self.assertIn("本轮不比较效果呈现", projected["one_line_verdict"])
        self.assertEqual(projected["stage_analysis"][0]["relation"], "creator_better")
        self.assertNotIn("comparison_reason", projected["stage_analysis"][0])
        for key in ("reference_relevance", "reference_observation", "benchmark_reference", "communication_strategy", "confidence_note"):
            self.assertNotIn(key, projected["stage_analysis"][0])
        for key in ("retain", "creator_retain", "creator_retention", "linked_experiment_id"):
            self.assertNotIn(key, projected["stage_analysis"][0])
        self.assertNotIn("linked_experiment_id", projected["stage_analysis"][1])
        for key in ("reference_relevance", "benchmark_reference", "communication_advice"):
            self.assertNotIn(key, projected["stage_analysis"][3])

        opening, effect, unbound, ambiguous, unchanged = projected["improvements"]
        self.assertEqual(opening["status"], "needs_human_confirmation")
        self.assertEqual(opening["actions"], ["展示售价39.9元。"])
        self.assertNotEqual(opening["title"], "旧开场建议")
        self.assertNotIn("旧", opening["observation"])
        for key in ("hypothesis", "benchmark_reference", "benchmark_evidence_ids", "benchmark_time_range", "expected_effect", "gmv_impact", "creator_script"):
            self.assertNotIn(key, opening)
        self.assertNotEqual(opening["problem"], "旧差距判断")
        self.assertEqual(effect["actions"], ["把充电画面放到前段。"])
        self.assertEqual(effect["status"], "needs_human_confirmation")
        self.assertEqual(unbound["status"], "needs_human_confirmation")
        self.assertEqual(unbound["suggestion"], "复拍产品操作。")
        self.assertEqual(ambiguous["status"], "needs_human_confirmation")
        self.assertNotIn("target_stage", ambiguous)
        self.assertNotIn("benchmark_reference", ambiguous)
        self.assertNotEqual(ambiguous["title"], "多阶段建议")
        self.assertEqual(unchanged["title"], "已确认同阶段建议")
        self.assertNotIn("expected_effect", unchanged)
        nested = projected["candidate_experiments"][0]
        self.assertEqual(nested["status"], "needs_human_confirmation")
        self.assertEqual(nested["action"], "标注售价39元。")
        self.assertEqual(nested["experiment_id"], "stable-exp-1")
        self.assertNotIn("benchmark_reference", nested)
        self.assertNotIn("expected_effect", nested)
        self.assertNotIn("creator_highlights", projected)
        self.assertNotIn("content_intent", projected)
        self.assertNotIn("retained_points", projected)
        self.assertEqual((run_dir / "analysis.json").read_bytes(), before)

    def test_all_unchanged_confirmations_preserve_body_tie_advice_and_model_summary(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        s6 = analysis["stage_analysis"][5]
        s6["relation"] = None
        s6["model_gap_magnitude"] = None
        s6["severity"] = None
        s6["comparison_status"] = "not_applicable"
        s6["analysis_status"] = "not_applicable"
        s6["stage_evidence_gate"] = {
            "status": "not_applicable",
            "reason_code": "bilateral_stage_absent",
            "source": "code",
        }
        analysis["one_line_verdict"] = "原始整句总结。"
        analysis["one_line_summary"] = "原始一句话摘要。"
        analysis["executive_summary"] = "原始执行摘要。"
        analysis["commercial_priority_summary"] = "原商业优先级。"
        analysis["global_diagnosis"] = {"findings": [{"id": "old", "summary": "旧 finding"}]}
        analysis["improvements"] = [{"target_stage": "S2", "title": "tie 建议", "action": "保留旧 tie 建议。"}]
        decisions = {
            "S1": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S2": {"decision": "confirmed", "relation": "tie", "gap": "none", "note": None},
            "S3": {"decision": "confirmed", "relation": "benchmark_better", "gap": "medium", "note": None},
            "S4": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
            "S5": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S6": {"decision": "not_applicable", "relation": None, "gap": None, "note": "人工确认阶段不适用。"},
        }

        projected = self._project_custom_analysis(run_dir, analysis, decisions)

        self.assertEqual(projected["review_delivery"]["summary_status"], "preserved_after_no_semantic_change")
        self.assertEqual(projected["review_delivery"]["changed_stages"], [])
        self.assertEqual(projected["review_summary"]["detail"], "阶段结论已确认，保留原稿；未逐条审核建议。")
        self.assertEqual(projected["one_line_verdict"], analysis["one_line_verdict"])
        self.assertEqual(projected["one_line_summary"], analysis["one_line_summary"])
        self.assertEqual(projected["executive_summary"], analysis["executive_summary"])
        self.assertEqual(projected["commercial_priority_summary"], analysis["commercial_priority_summary"])
        self.assertEqual(projected["global_diagnosis"], analysis["global_diagnosis"])
        self.assertEqual(projected["improvements"], analysis["improvements"])

        from scripts.flayr_core.bd_report import build_bd_report_data
        from scripts.flayr_core.creator_report import build_creator_report_data
        from scripts.flayr_core.report import ReportAssetContext

        baseline_views = (
            build_bd_report_data(analysis),
            build_creator_report_data(analysis, ReportAssetContext(run_dir)),
        )
        reviewed_views = (
            build_bd_report_data(projected),
            build_creator_report_data(projected, ReportAssetContext(run_dir)),
        )
        metadata_keys = {"reviewStatus", "reviewMode", "reviewSummary", "metadata"}
        for baseline, reviewed in zip(baseline_views, reviewed_views):
            self.assertEqual(
                {key: value for key, value in baseline.items() if key not in metadata_keys},
                {key: value for key, value in reviewed.items() if key not in metadata_keys},
            )

        body = copy.deepcopy(projected)
        body.pop("review_delivery")
        body.pop("review_status")
        body.pop("review_mode")
        body.pop("review_summary")
        for stage in body["stage_analysis"]:
            stage.pop("review_decision", None)
            stage.pop("review_note", None)
        self.assertEqual(body, analysis)

    def test_corrected_code_owned_na_stage_emits_human_axes_without_rewriting_gate(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        stage = analysis["stage_analysis"][3]
        stage["relation"] = None
        stage["model_gap_magnitude"] = None
        stage["severity"] = None
        stage["comparison_status"] = "not_applicable"
        stage["analysis_status"] = "not_applicable"
        stage["stage_evidence_gate"] = {
            "status": "not_applicable",
            "reason_code": "bilateral_stage_absent",
            "source": "code",
        }
        original_gate = copy.deepcopy(stage["stage_evidence_gate"])
        decisions = {
            "S1": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S2": {"decision": "confirmed", "relation": "tie", "gap": "none", "note": None},
            "S3": {"decision": "confirmed", "relation": "benchmark_better", "gap": "medium", "note": None},
            "S4": {"decision": "corrected", "relation": "benchmark_better", "gap": "small", "note": "人工确认此阶段可比较。"},
            "S5": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S6": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
        }

        projected = self._project_custom_analysis(run_dir, analysis, decisions)

        projected_stage = projected["stage_analysis"][3]
        self.assertEqual(projected_stage["review_decision"], "corrected")
        self.assertEqual(projected_stage["relation"], "benchmark_better")
        self.assertEqual(projected_stage["severity"], "small")
        self.assertEqual(projected_stage["stage_evidence_gate"], original_gate)
        self.assertEqual(projected_stage["comparison_status"], "not_applicable")
        self.assertEqual(projected["review_delivery"]["changed_stages"], ["S4"])

    def test_changed_review_removes_only_quantified_performance_sentences(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        analysis["improvements"] = [
            {
                "target_stage": "S1",
                "title": "原建议",
                "actions": [
                    "展示39.9元售价，预计完播率提高10-15%。",
                    "展示售价39.9元并预计GMV提升20%。",
                    "Display the $39.9 price and expect GMV to grow 20%.",
                    "预计转化率提高2倍。",
                    "标出500毫升容量。",
                ],
            }
        ]
        decisions = {
            "S1": {"decision": "corrected", "relation": "benchmark_better", "gap": "large", "note": "人工修正为明显差距。"},
            "S2": {"decision": "confirmed", "relation": "tie", "gap": "none", "note": None},
            "S3": {"decision": "confirmed", "relation": "benchmark_better", "gap": "medium", "note": None},
            "S4": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
            "S5": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S6": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
        }

        projected = self._project_custom_analysis(run_dir, analysis, decisions)

        self.assertEqual(
            projected["improvements"][0]["actions"],
            ["展示39.9元售价", "展示售价39.9元", "Display the $39.9 price", "标出500毫升容量。"],
        )

    def test_insufficient_evidence_replaces_unavailable_model_conclusion(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        stage = analysis["stage_analysis"][3]
        stage["relation"] = None
        stage["model_gap_magnitude"] = None
        stage["severity"] = None
        stage["judgment_reason"] = "模型没有输出可比较结论。"
        analysis["one_line_verdict"] = "旧摘要结论。"
        decisions = {
            "S1": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S2": {"decision": "confirmed", "relation": "tie", "gap": "none", "note": None},
            "S3": {"decision": "confirmed", "relation": "benchmark_better", "gap": "medium", "note": None},
            "S4": {"decision": "insufficient_evidence", "relation": None, "gap": None, "note": "本轮人工确认效果证据不足。"},
            "S5": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S6": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
        }

        projected = self._project_custom_analysis(run_dir, analysis, decisions)

        self.assertTrue(hr.model_stage_values(stage)["prediction_unavailable"])
        self.assertEqual(projected["review_delivery"]["changed_stages"], ["S4"])
        self.assertEqual(projected["stage_analysis"][3]["gap"], "本轮人工确认效果证据不足。")
        self.assertNotIn("旧摘要", projected["one_line_verdict"])
        self.assertEqual(projected["review_summary"]["detail"], "阶段结论已确认；原商业总结未随确认更新，已停用；原有建议并未随阶段确认自动获批，待确认候选需单独核实。")

    def test_pending_candidates_stay_in_bd_review_but_not_creator_actions(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        analysis["one_line_verdict"] = "旧商业总结不得复用。"
        analysis["stage_analysis"][0].pop("creator_evidence_ids", None)
        analysis["improvements"] = [
            {"target_stage": "S1", "title": "旧建议", "benchmark_reference": "旧对比依据", "action": "重新拍摄关键操作。"}
        ]
        analysis["candidate_experiments"] = [
            {"target_stage": "S1", "title": "旧实验", "benchmark_evidence_ids": ["B1"], "action": "重新拍摄关键操作。"}
        ]
        decisions = {
            "S1": {"decision": "corrected", "relation": "creator_better", "gap": "medium", "note": "人工确认开场差距已修正。"},
            "S2": {"decision": "confirmed", "relation": "tie", "gap": "none", "note": None},
            "S3": {"decision": "confirmed", "relation": "benchmark_better", "gap": "medium", "note": None},
            "S4": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
            "S5": {"decision": "confirmed", "relation": "creator_better", "gap": "small", "note": None},
            "S6": {"decision": "confirmed", "relation": "benchmark_better", "gap": "small", "note": None},
        }

        projected = self._project_custom_analysis(run_dir, analysis, decisions)

        from scripts.flayr_core.bd_report import build_bd_report_data
        from scripts.flayr_core.creator_report import build_creator_report_data
        from scripts.flayr_core.report import ReportAssetContext

        bd_view = build_bd_report_data(projected)
        creator_view = build_creator_report_data(projected, ReportAssetContext(run_dir))
        self.assertEqual(len(projected["improvements"]), 1)
        self.assertEqual(projected["improvements"][0]["status"], "needs_human_confirmation")
        self.assertEqual(bd_view["improvements"][0]["priorityLabel"], "待人工确认")
        self.assertEqual(bd_view["improvements"][0]["impactDirection"], "未核实")
        self.assertEqual(bd_view["improvements"][0]["planB"], "理想方向候选，不能作为已确认的标杆示范。")
        self.assertEqual(creator_view["experiments"], [])
        self.assertIsNone(creator_view["topExperiment"])
        self.assertIn("原商业总结未随确认更新，已停用", creator_view["intent"])
        creator_stage = next(stage for stage in creator_view["stages"] if stage["code"] == "S1")
        self.assertIn("原模型记录，未随本次人工更正复核：", creator_stage["observation"])

    def _approve_and_change_s1(self, run_dir: Path) -> None:
        self._approve_all(run_dir)
        correction = {
            "decision": "corrected",
            "relation": "benchmark_better",
            "gap": "medium",
            "note": "人工确认开场建议需要重新核实。",
        }
        hr.update_stage(run_dir, "S1", **{
            "decision": correction["decision"],
            "relation": correction["relation"],
            "gap": correction["gap"],
            "note": correction["note"],
        })
        hr.update_stage(run_dir, "S1", **{
            "decision": correction["decision"],
            "relation": correction["relation"],
            "gap": correction["gap"],
            "note": correction["note"],
        })

    def test_suggestion_review_approves_or_edits_demoted_actions_in_projected_copy(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = hr.load_analysis(run_dir)
        analysis["candidate_experiments"] = [
            {"target_stage": "S1", "title": "旧候选实验", "action": "补拍真实充电过程。"}
        ]
        self._replace_run_analysis(temp, run_dir, analysis)
        before = (run_dir / "analysis.json").read_bytes()
        hr.init_review(run_dir)
        self._approve_and_change_s1(run_dir)

        candidates = hr.reviewable_suggestion_actions(run_dir)
        self.assertEqual([item["key"] for item in candidates], ["improvements:0", "candidate_experiments:0"])
        self.assertTrue(hr.update_suggestion_review(run_dir, "improvements:0", "approved"))
        self.assertTrue(
            hr.update_suggestion_review(
                run_dir,
                "candidate_experiments:0",
                "edited",
                action="补拍真实充电过程，不用动画证明快充。",
            )
        )

        review = hr.load_review(run_dir)
        self.assertEqual(hr.review_status(review), "approved")
        self.assertEqual(review["suggestion_reviews"]["improvements:0"]["decision"], "approved")
        self.assertEqual(review["suggestion_reviews"]["candidate_experiments:0"]["decision"], "edited")
        self.assertTrue(review["suggestion_reviews"]["improvements:0"]["history"])
        self.assertTrue(review["suggestion_reviews"]["candidate_experiments:0"]["timestamp"])

        projected = hr.project_reviewed_analysis(run_dir, review)
        improvement = projected["improvements"][0]
        candidate_experiment = projected["candidate_experiments"][0]
        self.assertEqual(improvement["status"], "human_approved_action")
        self.assertEqual(improvement["suggestion"], "补充开场。")
        self.assertEqual(improvement["title"], "S1 人工确认的拍摄动作")
        self.assertNotIn("待人工确认", improvement["observation"])
        self.assertNotIn("actions", improvement)
        self.assertEqual(candidate_experiment["status"], "human_approved_action")
        self.assertEqual(candidate_experiment["action"], "补拍真实充电过程，不用动画证明快充。")
        self.assertNotIn("actions", candidate_experiment)
        self.assertEqual((run_dir / "analysis.json").read_bytes(), before)

    def test_pending_suggestion_review_keeps_candidate_unapproved(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        self._approve_and_change_s1(run_dir)
        self.assertFalse(hr.update_suggestion_review(run_dir, "improvements:0", "pending"))

        review = hr.load_review(run_dir)
        projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(review["suggestion_reviews"]["improvements:0"]["decision"], "pending")
        self.assertEqual(projected["improvements"][0]["status"], "needs_human_confirmation")
        self.assertNotEqual(projected["improvements"][0]["status"], "human_approved_action")

    def test_suggestion_action_can_be_corrected_and_revoked(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        self._approve_and_change_s1(run_dir)
        hr.update_suggestion_review(run_dir, "improvements:0", "approved")

        listed = hr.reviewable_suggestion_actions(run_dir)
        self.assertEqual(listed[0]["decision"], "approved")
        self.assertTrue(
            hr.update_suggestion_review(
                run_dir,
                "improvements:0",
                "edited",
                action="改为补拍真实操作过程。",
            )
        )
        self.assertTrue(hr.update_suggestion_review(run_dir, "improvements:0", "pending"))

        review = hr.load_review(run_dir)
        record = review["suggestion_reviews"]["improvements:0"]
        self.assertEqual(record["decision"], "pending")
        self.assertIsNone(record["action"])
        self.assertEqual(
            [event["decision"] for event in record["history"]],
            ["approved", "edited", "pending"],
        )
        projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(projected["improvements"][0]["status"], "needs_human_confirmation")

    def test_stage_edit_clears_suggestion_approval_and_invalidates_reports(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        self._approve_and_change_s1(run_dir)
        hr.update_suggestion_review(run_dir, "improvements:0", "approved")
        (run_dir / hr.REVIEWED_BD_REPORT_NAME).write_text("reviewed", encoding="utf-8")
        (run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).write_text("reviewed", encoding="utf-8")

        hr.update_stage(
            run_dir,
            "S1",
            "corrected",
            relation="creator_better",
            gap="large",
            note="再次调整人工判断。",
        )
        review = hr.load_review(run_dir)
        suggestion = review["suggestion_reviews"]["improvements:0"]
        self.assertEqual(review["stages"]["S1"]["decision"], "pending")
        self.assertEqual(suggestion["decision"], "pending")
        self.assertIsNone(suggestion["action"])
        self.assertEqual(suggestion["history"][-1]["reason"], "stage_edit")
        self.assertFalse((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertFalse((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())

    def test_all_unchanged_stage_confirmations_do_not_expose_suggestions_for_review(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        analysis = self._valid_comparison_analysis()
        review = hr.init_review(run_dir)
        axes = [
            ("creator_better", "small"),
            ("tie", "none"),
            ("benchmark_better", "medium"),
            ("benchmark_better", "small"),
            ("creator_better", "small"),
            ("benchmark_better", "small"),
        ]
        for code, (relation, gap) in zip(hr.STAGE_CODES, axes):
            review["stages"][code] = {
                "decision": "confirmed",
                "relation": relation,
                "gap": gap,
                "note": None,
            }
        review["status"] = "approved"
        with mock.patch.object(hr, "load_analysis", return_value=analysis):
            self.assertEqual(hr.reviewable_suggestion_actions(run_dir, review), [])

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

    def test_cli_a_entry_can_edit_a_pending_action_and_regenerates_reports(self) -> None:
        temp, run_dir = self.fresh_run()
        self.addCleanup(temp.cleanup)
        hr.init_review(run_dir)
        self._approve_and_change_s1(run_dir)

        with mock.patch(
            "builtins.input",
            side_effect=["a", "e", "补拍真实充电过程，不用动画证明快充。", "q"],
        ):
            self.assertEqual(review_analysis.main([str(run_dir)]), 0)

        review = hr.load_review(run_dir)
        self.assertEqual(review["suggestion_reviews"]["improvements:0"]["decision"], "edited")
        self.assertTrue((run_dir / hr.REVIEWED_BD_REPORT_NAME).exists())
        self.assertTrue((run_dir / hr.REVIEWED_CREATOR_REPORT_NAME).exists())
        projected = hr.project_reviewed_analysis(run_dir, review)
        self.assertEqual(projected["improvements"][0]["suggestion"], "补拍真实充电过程，不用动画证明快充。")

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
        self._approve_and_change_s1(run_dir)
        self.assertTrue(hr.update_suggestion_review(run_dir, "improvements:0", "approved"))
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
