"""当前主链的架构契约回归：不调用模型、不读取真实视频。"""

from __future__ import annotations

import inspect
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr
from flayr_core import asr, report as report_module, subtitle_track, translation, utils, video
from flayr_core.report import (
    ReportAssetContext,
    executive_summary,
    render_gap_badge,
    render_gap_overview,
    render_global_cause_note,
    render_global_diagnosis,
    render_improvement_meta,
    image_src_for_frame,
    stage_skipped,
)
from flayr_core.artifacts import (
    build_frame_manifest,
    parse_time_range_seconds,
    parse_timestamp_seconds,
    select_frames_for_time_range,
)
from flayr_core.llm import api as llm_api
from flayr_core.llm import media as llm_media
from flayr_core.llm import pipeline
from flayr_core.llm.provider_artifacts import ProviderReplayError, read_provider_artifact
from flayr_core.llm.analysis_contract import (
    AnalysisContractError,
    validate_normalized_analysis_contract,
    validate_raw_analysis_envelope,
)
from flayr_core.llm.json_codec import parse_json_text
from flayr_core.llm.product_profile import normalize_product_profile, normalize_proof_contract
from flayr_core.llm.s4_visual_verifier import (
    _visual_verifier_skip_reason,
    _visual_verifier_scope_rule,
    apply_s4_visual_verifier_result,
    build_s4_visual_verifier_payload,
)
from flayr_core.llm.payload import (
    build_comparison_eligibility_payload,
    build_improvement_reconciliation_payload,
    build_llm_comparison_payload,
    build_llm_payload,
    build_llm_repair_payload,
    build_product_foundation_payload,
    build_product_foundation_repair_payload,
    build_stage_group_judgment_payload,
    build_stage_synthesis_payload,
    build_stage_evidence_qualification_payload,
    build_video_fact_recovery_payload,
    full_analysis_output_fields,
    build_stage_review_payload,
    build_video_fact_payload,
    full_analysis_output_budget,
    build_video_identity_payload,
    load_brand_proposition,
    resolve_brand_key,
    STAGE_JUDGMENT_GROUPS,
)
from flayr_core.llm.parse import (
    normalize_analysis_result,
    normalize_comparison_contract,
    normalize_comparison_eligibility,
    normalize_hook_flags,
    normalize_multimodal_assessment,
    normalize_module_id,
    normalize_s3_flags,
    normalize_time_range_value,
    normalize_video_fact_result,
    normalize_video_understanding,
)
from flayr_core.multimodal import channel_requirement_for, multimodal_execution
from flayr_core.stage_evidence_contracts import (
    STAGE1_QUALIFICATION_GROUPS,
    STAGE_EVIDENCE_CONTRACT_VERSION,
    stage_codes,
)
from flayr_core.llm.pipeline import preserve_valid_repair_sections
from flayr_core.postprocess.proposition import materialize_cross_stage_inputs, materialize_quality_audits
from flayr_core.postprocess.utils import parse_srt_timestamp, read_srt_segments
from flayr_core.postprocess.chain import finalize_severity_after_repairs, stamp_comparison_eligibility
from flayr_core.postprocess.audit import PostprocessAudit, build_field_sources
from flayr_core.postprocess.derive import (
    _derive_one,
    _s1_hook_exec,
    _s2_contract_exec,
    _s3_usage_exec,
    _s4_effect_exec,
    _s5_trust_exec,
    _s6_cta_exec,
)
from flayr_core.postprocess.global_diagnosis import materialize_global_diagnosis
from flayr_core.verification_order import assert_verification_order, run_verification_stage
from scripts.audit_result_field_ownership import inventory, ownership_violations
from flayr_core.video_evidence import build_transcript_pack, parse_srt_time_range
from flayr_core.postprocess.repair import (
    align_stage_flag_evidence,
    apply_comparison_eligibility,
    fill_missing_evidence_references,
    prune_multimodal_evidence_to_stage,
    reconcile_s3_s4_evidence_coherence,
    reconcile_unsupported_cta,
    reconcile_s5_trust_sources,
    stabilize_improvement_priorities,
    validate_s3_s4_hard_fact_consistency,
    validate_stage_evidence_temporal_consistency,
)
from flayr_core.postprocess.repair_stages import infer_s1_boundary_candidate
from flayr_core.postprocess.repair_stages import align_clear_commerce_evidence
from flayr_core.postprocess.repair_stages import comparison_scope_summary
from flayr_core.postprocess.health_rewrite import (
    is_child_toothpaste_context,
    sanitize_health_recommendations,
    validate_recommendation_safety,
)
from flayr_core.postprocess.validate import (
    validate_analysis_dimensions,
    validate_chain_relationships,
    validate_multimodal_assessments,
    validate_required_stage_narratives,
    validate_s1_hook_flags,
    validate_s3_usage_flags,
    validate_s6_cta_flags,
    validate_evidence_alignment,
    validate_stage_time_coherence,
)
from flayr_core.prompt import write_analysis_input
from flayr_core.proposition_contract import build_product_proposition_contract
from flayr_core.stage_catalog import DEFAULT_STAGES, fallback_artifact_ranges, stage_tuples
from flayr_core.stage_ownership import CERTIFICATION_OWNERSHIP_PROMPT


class ArchitectureContractTests(unittest.TestCase):
    def test_degraded_report_does_not_render_unknown_severity_as_medium(self) -> None:
        analysis = {
            "mode": "compare",
            "analysis_run_state": "degraded",
            "stage_analysis": [
                {"stage": "S1 Hook", "severity": None},
            ],
        }
        badge = render_gap_badge(None)
        overview = render_gap_overview(analysis)

        self.assertIn("未分析", badge)
        self.assertIn('gap-tile-unknown', overview)
        self.assertIn('<span class="gap-level">未分析</span>', overview)
        self.assertNotIn('gap-tile-mid', overview)
        self.assertNotIn("差距中等", badge)
        self.assertIn("未完成大模型分析", executive_summary(analysis))

    def test_report_assets_are_confined_to_run_dir_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            image = run_dir / "frames" / "frame.png"
            image.parent.mkdir()
            image.write_bytes(b"\x89PNG\r\n\x1a\nreport-fixture")
            outside = root / "outside.png"
            outside.write_bytes(b"\x89PNG\r\n\x1a\noutside-secret")
            escaped = run_dir / "escaped.png"
            escaped.symlink_to(outside)

            assets = ReportAssetContext(run_dir)
            with mock.patch.object(
                report_module,
                "encode_file_data_url",
                wraps=report_module.encode_file_data_url,
            ) as encode:
                first = image_src_for_frame({"path": "frames/frame.png"}, assets)
                second = image_src_for_frame({"path": str(image)}, assets)
                self.assertTrue(first.startswith("data:image/png;base64,"))
                self.assertEqual(first, second)
                self.assertEqual(encode.call_count, 1)

            self.assertEqual(image_src_for_frame({"path": str(outside)}, assets), "")
            self.assertEqual(image_src_for_frame({"path": str(escaped)}, assets), "")

    def test_postprocess_audit_records_field_rule_and_evidence(self) -> None:
        result = {
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_summary": "old",
                    "creator_evidence_ids": ["C1"],
                }
            ]
        }
        audit = PostprocessAudit()

        def mutate(value: dict[str, object]) -> None:
            value["stage_analysis"][0]["creator_summary"] = "new"

        audit.run(result, "postprocess.test_rule", mutate, result)
        self.assertEqual(result["stage_analysis"][0]["creator_summary"], "new")
        self.assertEqual(len(audit.changes), 1)
        change = audit.changes[0]
        self.assertEqual(change["path"], "/stage_analysis/0/creator_summary")
        self.assertEqual(change["old"], "old")
        self.assertEqual(change["new"], "new")
        self.assertEqual(change["rule"], "postprocess.test_rule")
        self.assertEqual(change["evidence"], ["C1"])

    def test_field_sources_cover_unchanged_and_derived_final_fields(self) -> None:
        raw = {"stage_analysis": [{"severity": "medium", "summary": "model text"}]}
        normalized = json.loads(json.dumps(raw))
        final = json.loads(json.dumps(normalized))
        final["stage_analysis"][0]["severity"] = "large"
        audit = PostprocessAudit()
        audit.record(normalized, final, "postprocess.derive_severity")

        sources = build_field_sources(raw, normalized, final, audit.changes)
        self.assertEqual(sources["coverage"], "complete")
        severity = sources["fields"]["/stage_analysis/0/severity"]
        self.assertEqual(severity["source_artifact"], "postprocess_change_log.json")
        self.assertEqual(severity["rule"], "postprocess.derive_severity")
        summary = sources["fields"]["/stage_analysis/0/summary"]
        self.assertEqual(summary["source_artifact"], "raw_model_response.json")

    def test_field_sources_cover_container_emptied_by_deterministic_changes(self) -> None:
        raw = {"improvements": [{"title": "one"}, {"title": "two"}]}
        normalized = json.loads(json.dumps(raw))
        final = {"improvements": []}
        audit = PostprocessAudit()
        audit.record(normalized, final, "postprocess.apply_comparison_eligibility")

        sources = build_field_sources(raw, normalized, final, audit.changes)

        self.assertEqual(sources["coverage"], "complete")
        self.assertEqual(
            sources["fields"]["/improvements"]["rule"],
            "postprocess.apply_comparison_eligibility",
        )

    def test_post_finalize_audit_updates_final_artifact_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            provenance = {
                "schema_version": 1,
                "change_count": 0,
                "change_log_truncated": False,
            }
            final = {
                "stage_analysis": [{"stage": "S4 Effect", "severity": "medium"}],
                "postprocess_provenance": provenance,
            }
            (run_dir / "final_derived_result.json").write_text(json.dumps(final), encoding="utf-8")
            (run_dir / "raw_model_response.json").write_text(
                json.dumps({"stage_analysis": [{"stage": "S4 Effect", "severity": "medium"}]}),
                encoding="utf-8",
            )
            (run_dir / "validated_normalized_result.json").write_text(
                json.dumps({"stage_analysis": [{"stage": "S4 Effect", "severity": "medium"}]}),
                encoding="utf-8",
            )
            (run_dir / "postprocess_change_log.json").write_text(
                json.dumps({"schema_version": 1, "change_count": 0, "truncated": False, "changes": []}),
                encoding="utf-8",
            )
            before = json.loads(json.dumps(final))
            after = json.loads(json.dumps(final))
            after["stage_analysis"][0]["severity"] = "large"
            after["s4_visual_verifier"] = {"applied": True}
            audit = PostprocessAudit()
            audit.record(before, after, "postprocess.s4_visual_verifier")

            pipeline._merge_postprocess_audit({"run_dir": str(run_dir)}, after, audit=audit)
            logged = json.loads((run_dir / "postprocess_change_log.json").read_text(encoding="utf-8"))
            written = json.loads((run_dir / "final_derived_result.json").read_text(encoding="utf-8"))
            paths = {change["path"] for change in logged["changes"]}
            self.assertIn("/stage_analysis/0/severity", paths)
            self.assertIn("/s4_visual_verifier", paths)
            self.assertEqual(written, after)
            self.assertEqual(written["postprocess_provenance"]["change_count"], logged["change_count"])
            self.assertFalse(written["postprocess_provenance"]["change_log_truncated"])
            self.assertEqual(written["postprocess_provenance"]["field_sources"]["coverage"], "complete")
            self.assertIn("/stage_analysis/0/severity", logged["field_sources"]["fields"])

    def test_replay_provenance_uses_serialized_validated_artifact_hash(self) -> None:
        canonical = {"stage_analysis": []}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            analysis = {"run_dir": str(run_dir)}
            no_op = mock.Mock()
            with (
                mock.patch.object(pipeline, "apply_postprocess_chain", no_op),
                mock.patch.object(pipeline, "reconcile_stage_evidence_links", no_op),
                mock.patch.object(pipeline, "validate_evidence_alignment", no_op),
                mock.patch.object(pipeline, "validate_stage_ownership", no_op),
                mock.patch.object(pipeline, "sanitize_health_recommendations", no_op),
                mock.patch.object(pipeline, "sanitize_child_toothpaste_recommendations", no_op),
                mock.patch.object(pipeline, "stabilize_improvement_priorities", no_op),
                mock.patch.object(pipeline, "ground_improvement_evidence", no_op),
                mock.patch.object(pipeline, "validate_analysis_dimensions", no_op),
                mock.patch.object(pipeline, "validate_recommendation_safety", no_op),
                mock.patch.object(pipeline, "validate_creator_script_language", no_op),
                mock.patch.object(pipeline, "remove_unverified_brand_models", no_op),
                mock.patch.object(pipeline, "_clamp_result_time_ranges", no_op),
                mock.patch.object(pipeline, "materialize_global_diagnosis", no_op),
                mock.patch.object(pipeline, "validate_quality_contract", no_op),
                mock.patch.object(pipeline, "stage_evidence_link_issues", return_value=[]),
                mock.patch.object(pipeline, "stage_evidence_immutability_issues", return_value=[]),
                mock.patch.object(pipeline, "normalize_analysis_result", return_value=canonical),
                mock.patch.object(pipeline, "validate_normalized_analysis_contract"),
            ):
                pipeline.finalize_canonical_analysis_result(
                    pipeline.CanonicalAnalysisResult.from_mapping(canonical),
                    analysis,
                    "analysis input",
                    raw_snapshot=canonical,
                )

            validated_path = run_dir / "validated_normalized_result.json"
            provenance = json.loads(
                (run_dir / "final_derived_result.json").read_text(encoding="utf-8")
            )["postprocess_provenance"]
            serialized_hash = hashlib.sha256(validated_path.read_bytes()).hexdigest()
            self.assertEqual(provenance["validated_normalized_sha256"], serialized_hash)

    def test_preflight_finalization_does_not_persist_lifecycle_artifacts(self) -> None:
        canonical = {"stage_analysis": []}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            analysis = {"run_dir": str(run_dir)}
            no_op = mock.Mock()
            with (
                mock.patch.object(pipeline, "apply_postprocess_chain", no_op),
                mock.patch.object(pipeline, "reconcile_stage_evidence_links", no_op),
                mock.patch.object(pipeline, "validate_evidence_alignment", no_op),
                mock.patch.object(pipeline, "validate_stage_ownership", no_op),
                mock.patch.object(pipeline, "sanitize_health_recommendations", no_op),
                mock.patch.object(pipeline, "sanitize_child_toothpaste_recommendations", no_op),
                mock.patch.object(pipeline, "stabilize_improvement_priorities", no_op),
                mock.patch.object(pipeline, "ground_improvement_evidence", no_op),
                mock.patch.object(pipeline, "validate_analysis_dimensions", no_op),
                mock.patch.object(pipeline, "validate_recommendation_safety", no_op),
                mock.patch.object(pipeline, "validate_creator_script_language", no_op),
                mock.patch.object(pipeline, "remove_unverified_brand_models", no_op),
                mock.patch.object(pipeline, "_clamp_result_time_ranges", no_op),
                mock.patch.object(pipeline, "materialize_global_diagnosis", no_op),
                mock.patch.object(pipeline, "validate_quality_contract", no_op),
                mock.patch.object(pipeline, "stage_evidence_link_issues", return_value=[]),
                mock.patch.object(pipeline, "stage_evidence_immutability_issues", return_value=[]),
                mock.patch.object(pipeline, "normalize_analysis_result", return_value=canonical),
                mock.patch.object(pipeline, "validate_normalized_analysis_contract"),
            ):
                pipeline.finalize_analysis_result(
                    canonical,
                    analysis,
                    "analysis input",
                    persist_artifacts=False,
                )
            self.assertEqual(list(run_dir.iterdir()), [])

    def test_preflight_finalization_does_not_mutate_provider_response(self) -> None:
        raw = {"stage_analysis": [], "nested": {"source": "provider"}}
        analysis = {"run_dir": "/tmp/flayr-preflight-test", "state": "authoritative"}

        def mutate(candidate, candidate_analysis, *_args, **_kwargs):
            candidate["nested"]["source"] = "normalized"
            candidate_analysis["state"] = "mutated"
            return candidate

        with mock.patch.object(pipeline, "finalize_analysis_result", side_effect=mutate):
            returned = pipeline._process_llm_result(raw, analysis, "analysis input", None)

        self.assertEqual(raw["nested"]["source"], "provider")
        self.assertEqual(analysis["state"], "authoritative")
        self.assertEqual(returned["nested"]["source"], "normalized")

    @staticmethod
    def _multimodal(
        role_prefix: str,
        *,
        visual: str = "strong_positive",
        speech: str = "neutral",
        text: str = "positive",
        sound_rhythm: str = "neutral",
        dominant: str = "visual",
        relation: str = "complementary",
        effect: str = "strong",
        compensation: bool = True,
    ) -> dict[str, object]:
        evidence_id = "C1" if role_prefix == "creator" else "B1"
        return {
            "channel_impacts": {
                "visual": visual,
                "speech": speech,
                "text": text,
                "sound_rhythm": sound_rhythm,
            },
            "channel_evidence_ids": {
                "visual": [evidence_id],
                "speech": [] if speech == "absent" else [evidence_id],
                "text": [] if text == "absent" else [evidence_id],
                "sound_rhythm": [] if sound_rhythm == "absent" else [evidence_id],
            },
            "dominant_channel": dominant,
            "cross_channel_relation": relation,
            "integrated_effect": effect,
            "compensation_applied": compensation,
            "integration_reason": "强视觉承担核心任务，其他渠道只作补充或保持中性。",
        }

    def test_multimodal_normalization_keeps_closed_decision_space(self) -> None:
        normalized = normalize_multimodal_assessment(
            {
                "channel_impacts": {"visual": "STRONG_POSITIVE", "speech": "invented"},
                "channel_evidence_ids": {"visual": ["C1"]},
                "dominant_channel": "visual",
                "cross_channel_relation": "complementary",
                "integrated_effect": "strong",
                "compensation_applied": True,
                "integration_reason": "视觉主导。",
            }
        )
        self.assertEqual(normalized["channel_impacts"]["visual"], "strong_positive")
        self.assertEqual(normalized["channel_impacts"]["speech"], "unknown")
        self.assertEqual(normalized["channel_evidence_ids"]["visual"], ["C1"])

    def test_multimodal_normalization_repairs_mechanical_contradictions(self) -> None:
        normalized = normalize_multimodal_assessment(
            {
                "channel_impacts": {
                    "visual": "positive",
                    "speech": "neutral",
                    "text": "absent",
                    "sound_rhythm": "neutral",
                },
                "dominant_channel": "speech",
                "integrated_effect": "effective",
                "compensation_applied": True,
            }
        )
        self.assertEqual(normalized["dominant_channel"], "visual")
        self.assertFalse(normalized["compensation_applied"])

        no_positive = normalize_multimodal_assessment(
            {
                "channel_impacts": {
                    "visual": "neutral",
                    "speech": "negative",
                    "text": "absent",
                    "sound_rhythm": "neutral",
                },
                "dominant_channel": "visual",
                "integrated_effect": "strong",
            }
        )
        self.assertIsNone(no_positive["compensation_applied"])
        self.assertEqual(no_positive["integrated_effect"], "weak")

    def test_channel_requirement_axis_is_canonical(self) -> None:
        self.assertEqual(channel_requirement_for("S1")["level"], "any_channel_sufficient")
        self.assertEqual(channel_requirement_for("S2")["level"], "any_channel_sufficient")
        self.assertEqual(channel_requirement_for("S3")["required_signal"], "visible_usage_process")
        self.assertEqual(channel_requirement_for("S4")["required_signal"], "visible_effect")
        self.assertEqual(channel_requirement_for("S5")["level"], "source_grounded")
        self.assertEqual(channel_requirement_for("S6")["required_signal"], "explicit_purchase_action")

    def test_s1_strong_visual_can_compensate_weak_or_absent_speech(self) -> None:
        creator = self._multimodal("creator", speech="absent")
        benchmark = self._multimodal("benchmark", compensation=False)
        stage = {
            "stage": "S1 Hook",
            "model_severity": "medium",
            "creator_execution": 0.5,
            "benchmark_execution": 2.0,
            "creator_multimodal": creator,
            "benchmark_multimodal": benchmark,
        }
        trace = _derive_one("S1", stage, {"S1": 1.0}, [])
        self.assertEqual(trace["derived_creator_execution"], 2.0)
        self.assertEqual(trace["derived_benchmark_execution"], 2.0)
        self.assertEqual(trace["status"], "model_preserved")
        self.assertEqual(trace["severity"], "medium")

    def test_multimodal_s1_is_not_reinflated_by_legacy_anchor_gap(self) -> None:
        stage = {
            "stage": "S1 Hook",
            "model_severity": "medium",
            "creator_execution": 0.5,
            "benchmark_execution": 2.0,
            "creator_multimodal": self._multimodal("creator", effect="effective", compensation=False),
            "benchmark_multimodal": self._multimodal("benchmark", effect="strong", compensation=False),
            "creator_hook": {"anchors_proposition": False},
            "benchmark_hook": {"anchors_proposition": True},
            "painpoint_relevance": "benchmark_only",
        }
        trace = _derive_one("S1", stage, {"S1": 1.5}, [], allow_legacy_text_fallback=False)
        self.assertEqual(trace["derived_creator_execution"], 1.0)
        self.assertEqual(trace["derived_benchmark_execution"], 2.0)
        self.assertNotIn("E", trace)
        self.assertEqual(trace["status"], "model_preserved")
        self.assertEqual(trace["severity"], "medium")

    def test_multimodal_gate_rejects_strong_effect_with_strong_conflict(self) -> None:
        creator = self._multimodal(
            "creator",
            speech="strong_negative",
            relation="conflicting",
            effect="strong",
            compensation=False,
        )
        benchmark = self._multimodal("benchmark", compensation=False)
        result = {
            "stage_analysis": [{
                "stage": "S1 Hook",
                "creator_evidence_ids": ["C1"],
                "benchmark_evidence_ids": ["B1"],
                "creator_multimodal": creator,
                "benchmark_multimodal": benchmark,
            }]
        }
        with self.assertRaises(SystemExit):
            validate_multimodal_assessments(result, {"multimodal_assessment_required": True})

    def test_multimodal_gate_accepts_locked_same_stage_unit_not_selected_as_summary(self) -> None:
        creator = self._multimodal("creator", compensation=False)
        creator["channel_evidence_ids"] = {
            "visual": ["C2"],
            "speech": ["C2"],
            "text": ["C2"],
            "sound_rhythm": ["C2"],
        }
        result = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C2", "functions": ["S1_hook"]}]},
                "benchmark": {"evidence_units": [{"id": "B1", "functions": ["S1_hook"]}]},
            },
            "stage_analysis": [{
                "stage": "S1 Hook",
                "creator_evidence_ids": ["C1"],
                "benchmark_evidence_ids": ["B1"],
                "creator_multimodal": creator,
                "benchmark_multimodal": self._multimodal("benchmark", compensation=False),
            }],
        }
        validate_multimodal_assessments(result, {"multimodal_assessment_required": True})

    def test_multimodal_gate_does_not_require_citations_from_unresolved_stage1(self) -> None:
        creator = self._multimodal("creator", compensation=False)
        benchmark = self._multimodal("benchmark", compensation=False)
        for assessment in (creator, benchmark):
            assessment["channel_evidence_ids"] = {
                channel: [] for channel in ("visual", "speech", "text", "sound_rhythm")
            }
        result = {
            "video_understanding": {
                "creator": {"stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION},
                "benchmark": {"stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION},
            },
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_evidence_ids": [],
                    "benchmark_evidence_ids": [],
                    "creator_multimodal": creator,
                    "benchmark_multimodal": benchmark,
                }
            ],
        }

        validate_multimodal_assessments(result, {"multimodal_assessment_required": True})

    def test_s3_multimodal_can_enhance_complete_process_but_not_replace_it(self) -> None:
        assessment = self._multimodal("creator", compensation=False)
        complete = {
            "creator_multimodal": assessment,
            "creator_s3": {
                "usage_process_visible": True,
                "core_selling_point_visible": True,
                "action_proof_met": True,
                "action_target_contact_met": True,
                "action_application_change_visible": True,
                "critical_action_continuity_met": True,
                "missing_selling_points": [],
            },
        }
        missing = {
            "creator_multimodal": assessment,
            "creator_s3": {
                "usage_process_visible": False,
                "core_selling_point_visible": False,
            },
        }
        self.assertEqual(multimodal_execution("S3", complete, "creator", 1.0), 2.0)
        self.assertEqual(multimodal_execution("S3", missing, "creator", 0.0), 0.0)
        incomplete_comparison = {
            "creator_multimodal": assessment,
            "creator_s3": {
                "usage_process_visible": True,
                "core_selling_point_visible": True,
                "action_proof_met": True,
                "action_target_contact_met": True,
                "action_application_change_visible": True,
                "critical_action_continuity_met": True,
            },
        }
        self.assertEqual(multimodal_execution("S3", incomplete_comparison, "creator", 1.0), 1.0)

    def test_evidence_stages_cannot_be_rescued_by_atmosphere(self) -> None:
        for stage_id in ("S4", "S5", "S6"):
            stage = {"creator_multimodal": self._multimodal("creator", effect="strong")}
            self.assertEqual(multimodal_execution(stage_id, stage, "creator", 0.0), 0.0)

    def test_s4_thin_effect_floor_runs_when_coarse_execution_scores_tie(self) -> None:
        def effect_flag(salience: str, maximized: bool) -> dict[str, object]:
            return {
                "effect_type": "before_after",
                "effect_visible": True,
                "effect_salience": salience,
                "effect_proposition_matched": True,
                "comparison_control_met": False,
                "closeup_or_focus_met": True,
                "visual_difference_observed": True,
                "module_constraints_met": True,
                "effect_maximized": maximized,
                "requires_close_inspection": False,
                "effect_attribution_supported": True,
                "result_only_without_process": False,
                "process_linked_effect": True,
                "tamper_or_cut_risk": False,
            }

        stage = {
            "stage": "S4 效果呈现",
            "model_severity": "medium",
            "creator_s4": effect_flag("clear", False),
            "benchmark_s4": effect_flag("strong", True),
            "creator_multimodal": self._multimodal("creator", effect="effective", compensation=False),
            "benchmark_multimodal": self._multimodal("benchmark", effect="strong", compensation=False),
        }
        trace = _derive_one("S4", stage, {"S4": 1.0}, [])
        self.assertEqual(trace["derived_creator_execution"], 1.0)
        self.assertEqual(trace["derived_benchmark_execution"], 1.0)
        self.assertEqual(trace["severity"], "medium")

    def test_multimodal_evidence_alignment_only_adds_same_stage_locked_units(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {"id": "C1", "time_range": "0s - 3s"},
                        {"id": "C2", "time_range": "3s - 6s"},
                        {"id": "C9", "time_range": "20s - 24s"},
                    ]
                },
                "benchmark": {"evidence_units": []},
            },
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_time_range": "0s - 6s",
                    "creator_evidence_ids": ["C1"],
                    "creator_multimodal": {
                        "channel_evidence_ids": {"visual": ["C2", "C9", "UNKNOWN"]}
                    },
                }
            ],
        }
        align_stage_flag_evidence(result)
        self.assertEqual(result["stage_analysis"][0]["creator_evidence_ids"], ["C1", "C2"])

    def test_variant_attribution_uses_visual_threshold_and_explicit_comparison(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"black": 0.70, "silver": 0.30},
                            "variant_speech_shares": {"black": 0.20, "silver": 0.80},
                            "variant_relation_mode": "single_focus",
                            "comparison_purpose_explicit": False,
                        },
                        {
                            "id": "C2",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"black": 0.69, "silver": 0.31},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "single_focus",
                            "comparison_purpose_explicit": False,
                        },
                        {
                            "id": "C3",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"black": 0.5, "silver": 0.5},
                            "variant_speech_shares": {"black": 0.5, "silver": 0.5},
                            "variant_relation_mode": "explicit_comparison",
                            "comparison_purpose_explicit": True,
                        },
                    ]
                }
            }
        )
        units = understanding["creator"]["evidence_units"]
        self.assertEqual(units[0]["primary_variant_id"], "black")
        self.assertTrue(units[0]["variant_attribution_confident"])
        self.assertEqual(units[1]["primary_variant_id"], "")
        self.assertFalse(units[1]["variant_attribution_confident"])
        self.assertEqual(units[2]["primary_variant_id"], "")
        self.assertTrue(units[2]["variant_attribution_confident"])

    def test_invalid_variant_share_keys_cannot_create_primary_variant(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "gate_observation_status": {"variant_focus": "complete"},
                    "variant_decision_rule": {},
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"gold": 0.8},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "single_focus",
                            "comparison_purpose_explicit": False,
                        }
                    ],
                }
            }
        )
        unit = understanding["creator"]["evidence_units"][0]
        self.assertFalse(unit["variant_data_valid"])
        self.assertFalse(unit["variant_attribution_confident"])
        self.assertEqual(understanding["creator"]["gate_observation_status"]["variant_focus"], "unknown")

    def test_temporal_comparison_allows_one_variant_per_evidence_unit(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "gate_observation_status": {"variant_focus": "complete"},
                    "variant_decision_rule": {"speech_explains_choice": True},
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": ["black"],
                            "variant_visual_shares": {"black": 1.0},
                            "variant_speech_shares": {"black": 1.0},
                            "variant_relation_mode": "explicit_comparison",
                            "comparison_purpose_explicit": True,
                        },
                        {
                            "id": "C2",
                            "variant_ids": ["silver"],
                            "variant_visual_shares": {"silver": 1.0},
                            "variant_speech_shares": {"silver": 1.0},
                            "variant_relation_mode": "explicit_comparison",
                            "comparison_purpose_explicit": True,
                        },
                    ],
                }
            }
        )
        self.assertTrue(all(unit["variant_data_valid"] for unit in understanding["creator"]["evidence_units"]))
        self.assertEqual(understanding["creator"]["gate_observation_status"]["variant_focus"], "complete")

    def test_postprocess_placeholder_does_not_invalidate_gate_observation(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "gate_observation_status": {"variant_focus": "complete"},
                    "variant_decision_rule": {},
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": [],
                            "variant_visual_shares": {},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "none",
                            "comparison_purpose_explicit": False,
                        },
                        {"id": "C_NO_CTA", "information": "结尾未识别到明确购买指令。"},
                    ],
                }
            }
        )
        self.assertFalse(understanding["creator"]["evidence_units"][1]["variant_data_valid"])
        self.assertEqual(understanding["creator"]["gate_observation_status"]["variant_focus"], "complete")

    def test_attention_scan_requires_audit_and_competitor_detail(self) -> None:
        base = {
            "creator": {
                "gate_observation_status": {"attention_scan": "complete"},
                "attention_competitors": [],
                "evidence_units": [],
            }
        }
        without_audit = normalize_video_understanding(base)
        self.assertEqual(without_audit["creator"]["gate_observation_status"]["attention_scan"], "unknown")

        base["creator"]["attention_scan_audit"] = {
            "recording_equipment_visible": True,
            "foreground_non_task_object_visible": True,
            "evidence_ids": [],
        }
        missing_detail = normalize_video_understanding(base)
        self.assertEqual(missing_detail["creator"]["gate_observation_status"]["attention_scan"], "unknown")

        base["creator"]["attention_competitors"] = [
            {
                "id": "AC1",
                "object_label": "手持录音设备",
                "persistent_motion": True,
                "high_salience": True,
                "participates_in_product_task": False,
            }
        ]
        complete = normalize_video_understanding(base)
        self.assertEqual(complete["creator"]["gate_observation_status"]["attention_scan"], "complete")

    def test_global_priority_orders_route_block_before_attention_major(self) -> None:
        result = self._global_result()
        result["product_profile"] = {
            "proof_contract_source": "operator",
            "short_video_proof_plan": {
                "valid": True,
                "s4_anchor_candidate_id": "oil_control",
                "selection_source": "operator_priority",
                "anchor_confidence": "high",
            }
        }
        creator = result["video_understanding"]["creator"]
        creator["selling_point_observations"] = [
            {
                "candidate_id": "cooling",
                "text": "冰凉感",
                "visual_share": 0.8,
                "speech_share": 0.7,
                "proof_signal_present": False,
                "evidence_ids": ["C1"],
            }
        ]
        creator["attention_competitors"] = [
            {
                "object_label": "手持麦克风",
                "time_ranges": ["0.0s - 5.0s"],
                "persistent_motion": True,
                "high_salience": True,
                "participates_in_product_task": False,
                "occludes_proof_area": False,
                "evidence_ids": ["C1"],
            }
        ]
        result["stage_analysis"][2]["creator_absolute_status"] = "weak"
        result["stage_analysis"][3]["creator_absolute_status"] = "missing"
        materialize_global_diagnosis(result, {})
        priorities = result["commercial_priorities"]
        self.assertEqual((priorities[0]["id"], priorities[0]["tier"]), ("global:selling_point_route", "P0"))
        self.assertIn(("global:attention_cleanliness", "P2"), [(item["id"], item["tier"]) for item in priorities])

        result["product_profile"]["proof_contract_source"] = "inferred"
        materialize_global_diagnosis(result, {})
        route = next(item for item in result["global_diagnosis"]["findings"] if item["id"] == "selling_point_route")
        self.assertEqual(route["impact"], "major")

    def test_unknown_stage_severity_does_not_become_commercial_priority(self) -> None:
        result = self._global_result()
        for stage in result["stage_analysis"]:
            stage["severity"] = None
        materialize_global_diagnosis(result, {})
        self.assertFalse(
            any(item.get("source") == "stage" for item in result["commercial_priorities"])
        )

    def test_provider_replay_rejects_malformed_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "status": "completed",
                        "request_identity": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "response metadata missing"):
                read_provider_artifact(path)

            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "status": "partial",
                        "request_identity": {},
                        "response_meta": {
                            "logical_request_id": "fixture",
                            "completion_attempts": 1,
                            "retry_reasons": [],
                            "usage": {},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "status invalid"):
                read_provider_artifact(path)

    def test_verification_marker_requires_a_successful_command_and_changed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "fixture.txt"
            marker = run_verification_stage(
                root,
                "fixture",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    f"Path({str(evidence)!r}).write_text('passed', encoding='utf-8')",
                ],
                evidence_paths=[evidence],
                repo_root=ROOT,
            )
            value = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(value["schema_version"], 3)
            self.assertEqual(value["stage"], "fixture")
            self.assertEqual(value["status"], "passed")
            self.assertNotEqual(value["proof"]["source_commit"], "0" * 40)
            self.assertRegex(value["proof"]["proof_sha256"], r"^[0-9a-f]{64}$")
            assert_verification_order(root, "offline_replay", repo_root=ROOT)

            unchanged = root / "unchanged.txt"
            unchanged.write_text("same", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not changed"):
                run_verification_stage(
                    root,
                    "offline_replay",
                    command=[sys.executable, "-c", "pass"],
                    evidence_paths=[unchanged],
                    repo_root=ROOT,
                )

    def test_result_field_ownership_gate_rejects_new_runtime_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = root / "scripts" / "flayr_core" / "report.py"
            writer.parent.mkdir(parents=True)
            writer.write_text("result['severity'] = 'large'\n", encoding="utf-8")
            result = inventory(root, ("severity",))
            self.assertIn("unauthorized production writer", " ".join(ownership_violations(result)))

    def test_operator_primary_selling_point_creates_trusted_route(self) -> None:
        foundation = {
            "product_profile": {
                "short_video_proof_plan": {
                    "valid": True,
                    "primary_candidate_id": "cooling",
                    "selection_source": "model_category_default",
                    "anchor_confidence": "low",
                    "candidates": [
                        {"id": "cooling", "selling_point": "冰凉肤感"},
                        {"id": "oil_control", "selling_point": "控油"},
                    ],
                }
            }
        }
        pipeline._stamp_proof_contract_source(
            foundation,
            {"product": {"primary_selling_point": "控油"}},
        )
        profile = foundation["product_profile"]
        self.assertEqual(profile["proof_contract_source"], "operator")
        self.assertEqual(profile["short_video_proof_plan"]["primary_candidate_id"], "oil_control")
        self.assertEqual(profile["short_video_proof_plan"]["selection_source"], "operator_priority")

    def test_focus_block_precedes_s4_large_and_adds_causal_labels(self) -> None:
        result = self._global_result()
        creator = result["video_understanding"]["creator"]
        creator["variant_decision_rule"] = {"speech_explains_choice": False, "evidence_ids": ["C1"]}
        creator["evidence_units"][0].update(
            {
                "variant_ids": ["black", "silver"],
                "variant_relation_mode": "ambiguous",
                "variant_attribution_confident": False,
                "functions": ["S4_effect"],
            }
        )
        result["stage_analysis"][3]["severity"] = "large"
        materialize_global_diagnosis(result, {})
        self.assertEqual([item["id"] for item in result["commercial_priorities"][:2]], ["global:focus_coherence", "stage:S4"])
        self.assertIn("focus_coherence", result["stage_analysis"][3]["affected_by_global_issues"])
        self.assertIn("focus_coherence", result["improvements"][0]["root_cause_ids"])

    def test_attention_gate_ignores_product_task_motion_and_respects_asymmetric_temporal_mode(self) -> None:
        result = self._global_result()
        creator = result["video_understanding"]["creator"]
        creator["attention_competitors"] = [
            {
                "object_label": "粉扑",
                "time_ranges": ["1.0s - 6.0s"],
                "persistent_motion": True,
                "high_salience": True,
                "participates_in_product_task": True,
                "occludes_proof_area": False,
                "evidence_ids": ["C1"],
            }
        ]
        result["video_understanding"]["benchmark"]["temporal_evidence_mode"] = "static_only"
        materialize_global_diagnosis(result, {})
        attention = next(item for item in result["global_diagnosis"]["findings"] if item["id"] == "attention_cleanliness")
        self.assertEqual(attention["impact"], "pass")
        self.assertEqual(attention["comparative_status"], "unknown")

        creator["attention_competitors"][0]["participates_in_product_task"] = False
        materialize_global_diagnosis(result, {})
        attention = next(item for item in result["global_diagnosis"]["findings"] if item["id"] == "attention_cleanliness")
        self.assertEqual(attention["impact"], "major")
        self.assertEqual(attention["comparative_status"], "unknown")

    def test_old_video_understanding_degrades_global_gates_to_unknown(self) -> None:
        normalized = normalize_video_understanding(
            {"creator": {"evidence_units": [{"id": "C1", "information": "旧 facts"}]}}
        )
        self.assertEqual(normalized["creator"]["temporal_evidence_mode"], "unknown")
        result = self._global_result()
        result["video_understanding"] = normalized
        materialize_global_diagnosis(result, {})
        self.assertNotEqual(result["global_diagnosis"]["overall_status"], "blocking")

    def test_report_renders_root_findings_and_uses_commercial_summary(self) -> None:
        result = self._global_result()
        result["global_diagnosis"] = {
            "temporal_capability": {"comparative": "full_temporal"},
            "findings": [
                {
                    "id": "selling_point_route",
                    "impact": "blocking",
                    "summary": "主卖点路线错误。",
                    "downstream_impact": "S2-S4 围绕错误价值展开。",
                    "suggested_action": "先改主卖点。",
                    "affected_stages": ["S2", "S3", "S4"],
                }
            ],
        }
        result["commercial_priority_summary"] = "先修正主卖点路线。"
        result["commercial_priorities"] = [
            {"tier": "P0", "title": "主卖点路线", "summary": "先修正主卖点路线。"}
        ]
        self.assertEqual(executive_summary(result), "先修正主卖点路线。")
        rendered = render_global_diagnosis(result)
        self.assertIn("根本性问题", rendered)
        self.assertIn("主卖点路线错误", rendered)
        self.assertIn("商业处理顺序", rendered)
        self.assertIn("先处理根因", render_global_cause_note(["selling_point_route"]))
        self.assertIn(
            "根因关联",
            render_improvement_meta({"root_cause_ids": ["selling_point_route"]}, "0.0s - 3.0s"),
        )

    def test_finalized_result_copies_global_commercial_fields(self) -> None:
        normalized = {
            "executive_summary": "旧摘要",
            "one_line_summary": "旧摘要",
            "one_line_verdict": "结论",
            "holistic_assessment": {},
            "key_conclusions": [],
            "comparison_contract": {},
            "comparison_eligibility": {},
            "product_visibility": {},
            "loop_closure": {},
            "video_understanding": {},
            "stage_analysis": [],
            "improvements": [],
            "global_diagnosis": {"overall_status": "major"},
            "commercial_priorities": [{"id": "global:focus_coherence"}],
            "commercial_priority_summary": "先统一产品焦点。",
        }
        analysis: dict[str, object] = {}
        pipeline.apply_finalized_analysis_result(analysis, normalized, Path("analysis_result.json"))
        self.assertEqual(analysis["global_diagnosis"], {"overall_status": "major"})
        self.assertEqual(analysis["commercial_priority_summary"], "先统一产品焦点。")

    def test_s1_srt_boundary_does_not_backproject_later_evidence_cue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            (role_dir / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:04,000\n毛孔粗大、油皮和混油皮注意\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\n下午出油又不知道怎么补妆\n\n"
                "3\n00:00:08,000 --> 00:00:10,000\n推荐用这个解决\n",
                encoding="utf-8",
            )
            result = {
                "video_understanding": {
                    "benchmark": {
                        "evidence_units": [
                            {
                                "id": "B1",
                                "time_range": "0.0s - 10.0s",
                                "information": "痛点后推荐产品。",
                                "voiceover_zh": "毛孔粗大、下午出油，推荐用这个解决。",
                                "functions": ["S1_hook"],
                            },
                            {
                                "id": "B2",
                                "time_range": "10.0s - 13.0s",
                                "information": "介绍产品名称。",
                                "functions": ["S2_intro"],
                            },
                        ]
                    }
                }
            }
            candidate = infer_s1_boundary_candidate(
                "benchmark", result, {"videos": {"benchmark": {"work_dir": str(role_dir)}}}
            )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["seconds"], 10.0)
        self.assertEqual(candidate["source"], "evidence")

    def test_promise_chain_allows_generic_conversion_chain_when_only_s1_to_s4_are_named(self) -> None:
        result = {
            "s3_s4_relationship": {
                "creator_relationship": "process_without_effect",
                "benchmark_relationship": "process_creates_effect",
                "creator_reason": "达人展示了使用过程，但没有放大结果。",
                "benchmark_reason": "标杆将过程与效果连续展示。",
            },
            "promise_chain": {
                "s1_promise": "解决出油和毛孔问题。",
                "s2_answer": "粉饼作为解决方案出现。",
                "s3_proof_target": "按压上脸的使用过程。",
                "s4_outcome": "半脸效果对比。",
                "chain_closed": False,
                "broken_at": "S4",
                "break_reason": "效果验证不足，转化链条在 S4 断裂。",
            },
        }
        validate_chain_relationships(result, {"s3_flags_required": True, "s4_flags_required": True})

    def test_full_multimodal_analysis_is_the_cli_default(self) -> None:
        args = flayr.build_parser().parse_args(["compare", "--verification-stage", "production"])
        self.assertTrue(args.llm_include_images)
        legacy = flayr.build_parser().parse_args(
            ["--no-llm-include-images", "compare", "--verification-stage", "production"]
        )
        self.assertFalse(legacy.llm_include_images)

    def test_cli_requires_explicit_execution_intent(self) -> None:
        with self.assertRaises(SystemExit):
            flayr.build_parser().parse_args(["compare"])

    def test_legacy_text_entrypoint_is_explicitly_rejected(self) -> None:
        args = flayr.build_parser().parse_args(
            ["--no-llm-include-images", "compare", "--verification-stage", "production"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "text-only LLM"):
                pipeline.run_large_model_analysis(
                    args,
                    {},
                    root / "analysis_input.md",
                    root,
                )

    def test_external_analysis_import_requires_explicit_legacy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            result = root / "analysis_result.json"
            benchmark.write_bytes(b"fixture")
            creator.write_bytes(b"fixture")
            result.write_text("{}", encoding="utf-8")
            args = flayr.build_parser().parse_args(
                [
                    "compare",
                    "--benchmark-video",
                    str(benchmark),
                    "--creator-video",
                    str(creator),
                    "--verification-stage",
                    "production",
                    "--analysis-result-json",
                    str(result),
                ]
            )
            with self.assertRaisesRegex(SystemExit, "--analysis-result-json"):
                flayr.validate_inputs(args)
            args = flayr.build_parser().parse_args(
                [
                    "compare",
                    "--benchmark-video",
                    str(benchmark),
                    "--creator-video",
                    str(creator),
                    "--verification-stage",
                    "production",
                    "--analysis-result-json",
                    str(result),
                    "--legacy-import",
                ]
            )
            self.assertEqual(set(flayr.validate_inputs(args)), {"benchmark", "creator"})

    def test_cli_does_not_accept_abbreviated_protected_network_flags(self) -> None:
        with self.assertRaises(SystemExit):
            flayr.build_parser().parse_args(["compare", "--llm-api-u", "https://attacker.invalid"])

    def test_module_id_uses_structure_library_as_the_only_enum_source(self) -> None:
        self.assertEqual(normalize_module_id("S4-F", 4), "S4-F")
        self.assertEqual(normalize_module_id("S4-G", 4), "unknown")
        self.assertEqual(normalize_module_id("S3-A", 4), "unknown")

    def test_repair_keeps_omitted_stage_fields_and_canonicalizes_preserved_module(self) -> None:
        original = {
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "creator_module_id": "S4-G" if index == 4 else "unknown",
                    "benchmark_summary": f"标杆 {index}",
                    "creator_summary": f"达人 {index}",
                    "gap": f"差距 {index}",
                    "creator_s4": {"effect_visible": True} if index == 4 else {},
                }
                for index in range(1, 7)
            ],
            "improvements": [{"target_stage": "S4", "title": "保留建议"}],
        }
        repaired = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "improvements": [],
        }
        merged = preserve_valid_repair_sections(original, repaired)
        s4 = merged["stage_analysis"][3]
        self.assertEqual(s4["creator_module_id"], "unknown")
        self.assertEqual(s4["benchmark_summary"], "标杆 4")
        self.assertEqual(s4["creator_s4"], {"effect_visible": True})
        self.assertEqual(merged["improvements"], original["improvements"])

    def test_repair_preserves_omitted_nested_s3_fields(self) -> None:
        original = {
            "stage_analysis": [
                {
                    "stage": "S3 使用过程",
                    "creator_s3": {
                        "exists": True,
                        "usage_evidence_state": "partial",
                        "evidence_ids": ["C3"],
                    },
                    "benchmark_s3": {
                        "exists": False,
                        "usage_evidence_state": "partial",
                        "evidence_ids": ["B3"],
                    },
                }
            ]
        }
        repaired = {
            "stage_analysis": [
                {
                    "stage": "S3 使用过程",
                    "creator_s3": {"exists": True, "usage_evidence_state": None},
                    "benchmark_s3": {"exists": False},
                }
            ]
        }

        merged = preserve_valid_repair_sections(original, repaired)
        creator = merged["stage_analysis"][0]["creator_s3"]
        benchmark = merged["stage_analysis"][0]["benchmark_s3"]
        self.assertEqual(creator["usage_evidence_state"], "partial")
        self.assertEqual(creator["evidence_ids"], ["C3"])
        self.assertEqual(benchmark["usage_evidence_state"], "partial")
        self.assertEqual(benchmark["evidence_ids"], ["B3"])

    def test_repair_preserves_valid_stage_evidence_context(self) -> None:
        original = {
            "stage_analysis": [
                {"stage": f"S{index}"}
                for index in range(1, 4)
            ] + [
                {
                    "stage": "S4 效果呈现",
                    "benchmark_time_range": "10.0s - 20.0s",
                    "benchmark_evidence_ids": ["B4"],
                    "benchmark_s4": {"evidence_ids": ["B4"]},
                    "creator_time_range": "none",
                    "creator_evidence_ids": [],
                }
            ],
        }
        repaired = {
            "stage_analysis": [
                {"stage": f"S{index}"}
                for index in range(1, 4)
            ] + [
                {
                    "stage": "S4 效果呈现",
                    "benchmark_time_range": "20.0s - 30.0s",
                    "benchmark_evidence_ids": ["B2"],
                    "benchmark_s4": {"evidence_ids": ["B2"]},
                    "creator_time_range": "15.0s - 25.0s",
                    "creator_evidence_ids": ["C3"],
                }
            ],
        }
        merged = preserve_valid_repair_sections(original, repaired)
        stage = merged["stage_analysis"][3]
        self.assertEqual(stage["benchmark_time_range"], "10.0s - 20.0s")
        self.assertEqual(stage["benchmark_evidence_ids"], ["B4"])
        self.assertEqual(stage["benchmark_s4"]["evidence_ids"], ["B4"])
        self.assertEqual(stage["creator_time_range"], "15.0s - 25.0s")
        self.assertEqual(stage["creator_evidence_ids"], ["C3"])

    def test_repair_keeps_non_independent_s5_basis_flags_coherent(self) -> None:
        original = {
            "stage_analysis": [
                {"stage": f"S{index}"}
                for index in range(1, 5)
            ] + [
                {
                    "stage": "S5 信任放大",
                    "benchmark_s5": {
                        "exists": True,
                        "trust_basis": "independent_user",
                        "independent_trust_purpose": True,
                    },
                }
            ],
        }
        repaired = {
            "stage_analysis": [
                {"stage": f"S{index}"}
                for index in range(1, 5)
            ] + [
                {
                    "stage": "S5 信任放大",
                    "benchmark_s5": {
                        "trust_basis": "product_claim",
                    },
                }
            ],
        }
        merged = preserve_valid_repair_sections(original, repaired)
        flag = merged["stage_analysis"][4]["benchmark_s5"]
        self.assertEqual(flag["trust_basis"], "product_claim")
        self.assertFalse(flag["exists"])
        self.assertFalse(flag["independent_trust_purpose"])

    def test_time_clamp_accepts_fact_precision_at_video_end(self) -> None:
        result = {
            "video_understanding": {
                "benchmark": {"evidence_units": [{"id": "B5", "time_range": "38.3s - 45.7s"}]},
                "creator": {"evidence_units": [{"id": "C5", "time_range": "78.3s - 84.3s"}]},
            },
            "stage_analysis": [
                {
                    "benchmark_time_range": "38.3s - 45.7s",
                    "creator_time_range": "78.3s - 84.3s",
                }
                for _ in range(6)
            ],
            "improvements": [],
        }
        analysis = {
            "videos": {
                "benchmark": {"duration_seconds": 45.666667},
                "creator": {"duration_seconds": 84.266667},
            }
        }
        pipeline._clamp_result_time_ranges(result, analysis)
        self.assertEqual(result["stage_analysis"][5]["benchmark_time_range"], "38.3s - 45.7s")
        self.assertEqual(result["stage_analysis"][5]["creator_time_range"], "78.3s - 84.3s")

    def test_health_sanitizer_runs_for_auto_language_in_malaysia_market(self) -> None:
        analysis_input = (
            "## 产品信息\n"
            "- 品类：保健品 - 女性生理期营养补充\n"
            "- 目标市场：my\n"
            "- 检测语言：auto\n"
        )
        result = {
            "improvements": [
                {
                    "title": "增加痛点Hook提升停留率",
                    "suggestion": "Period lambat datang",
                    "creator_script": "Korang pernah tak rasa macam ni? Period lambat datang...",
                    "creator_script_zh": "经期延迟时可以这样表达。",
                }
            ]
        }

        sanitize_health_recommendations(result, analysis_input)
        validate_recommendation_safety(result, analysis_input)
        self.assertNotIn("period", result["improvements"][0]["creator_script"].lower())

    def test_commerce_evidence_alignment_does_not_split_s4_stage_and_flag(self) -> None:
        result = {
            "video_understanding": {
                "benchmark": {
                    "evidence_units": [
                        {
                            "id": "B3",
                            "time_range": "13.8s - 25.5s",
                            "information": "用户评论反馈",
                            "voiceover": "",
                            "voiceover_zh": "",
                        },
                        {
                            "id": "B4",
                            "time_range": "25.5s - 38.3s",
                            "information": "展示产品并出现效果字幕",
                            "voiceover": "",
                            "voiceover_zh": "",
                        },
                    ]
                }
            },
            "stage_analysis": [
                {"stage": f"S{index}", "benchmark_evidence_ids": []}
                for index in range(1, 7)
            ],
        }
        result["stage_analysis"][2].update(
            {
                "benchmark_time_range": "25.5s - 38.3s",
                "benchmark_evidence_ids": ["B4"],
            }
        )
        result["stage_analysis"][3].update(
            {
                "benchmark_time_range": "25.5s - 38.3s",
                "benchmark_evidence_ids": ["B4"],
                "benchmark_s4": {"evidence_ids": ["B4"]},
            }
        )

        align_clear_commerce_evidence(result)

        stage = result["stage_analysis"][3]
        self.assertEqual(stage["benchmark_evidence_ids"], ["B4"])
        self.assertEqual(stage["benchmark_s4"]["evidence_ids"], ["B4"])

    def test_repair_marks_are_xie_cross_stage_evidence_as_temporal_state_conflict(self) -> None:
        fixture_path = ROOT / "tests" / "fixtures" / "are_xie_s4_temporal_mismatch.json"
        result = json.loads(fixture_path.read_text(encoding="utf-8"))

        finalize_severity_after_repairs(result, {})

        s4 = result["stage_analysis"][3]
        temporal = s4["_postprocess_state"]["evidence_temporal_checks"]
        self.assertEqual(temporal["benchmark_evidence_ids"]["status"], "consistent")
        self.assertEqual(temporal["benchmark_s4.evidence_ids"]["status"], "state_conflict")
        self.assertEqual(
            temporal["benchmark_s4.evidence_ids"]["reason_code"],
            "evidence_temporal_mismatch",
        )
        self.assertEqual(
            temporal["benchmark_s4.evidence_ids"]["conflicting_evidence_ids"],
            ["B3"],
        )
        self.assertEqual(
            temporal["benchmark_multimodal.channel_evidence_ids.visual"]["reason_code"],
            "evidence_temporal_mismatch",
        )

        hard_fact = s4["_postprocess_state"]["evidence_hard_fact_checks"]["benchmark_s4"]
        self.assertEqual(hard_fact["status"], "state_conflict")
        self.assertEqual(hard_fact["reason_code"], "evidence_temporal_mismatch")

    def test_repair_temporal_check_covers_all_stage_flags_and_both_sides(self) -> None:
        stages = []
        for index, flag_name in enumerate(("hook", "s2", "s3", "s4", "s5", "s6"), start=1):
            stages.append(
                {
                    "stage": f"S{index}",
                    "benchmark_time_range": "0.0s - 1.0s",
                    "creator_time_range": "0.0s - 1.0s",
                    f"benchmark_{flag_name}": {"evidence_ids": ["B_OUTSIDE"]},
                    f"creator_{flag_name}": {"evidence_ids": ["C_OUTSIDE"]},
                }
            )
        result = {
            "video_understanding": {
                "benchmark": {"evidence_units": [{"id": "B_OUTSIDE", "time_range": "2.0s - 3.0s"}]},
                "creator": {"evidence_units": [{"id": "C_OUTSIDE", "time_range": "2.0s - 3.0s"}]},
            },
            "stage_analysis": stages,
        }

        validate_stage_evidence_temporal_consistency(result)

        for index, flag_name in enumerate(("hook", "s2", "s3", "s4", "s5", "s6")):
            checks = result["stage_analysis"][index]["_postprocess_state"]["evidence_temporal_checks"]
            for role in ("benchmark", "creator"):
                check = checks[f"{role}_{flag_name}.evidence_ids"]
                self.assertEqual(check["status"], "state_conflict")
                self.assertEqual(check["reason_code"], "evidence_temporal_mismatch")

    def test_repair_temporal_check_accepts_empty_absence_and_positive_overlap(self) -> None:
        result = {
            "video_understanding": {
                "benchmark": {
                    "evidence_units": [
                        {"id": "B_CROSS", "time_range": "24.0s - 27.0s"},
                    ]
                },
                "creator": {
                    "evidence_units": [
                        {"id": "C_CROSS", "time_range": "24.0s - 27.0s"},
                    ]
                },
            },
            "stage_analysis": [
                {"stage": f"S{index}", "benchmark_time_range": "0.0s - 1.0s", "creator_time_range": "0.0s - 1.0s"}
                for index in range(1, 7)
            ],
        }
        s4 = result["stage_analysis"][3]
        s4.update(
            {
                "benchmark_s4": {"evidence_ids": []},
                "creator_s4": {"evidence_ids": []},
                "benchmark_time_range": "25.5s - 38.3s",
                "creator_time_range": "25.5s - 38.3s",
            }
        )
        validate_stage_evidence_temporal_consistency(result)
        checks = s4["_postprocess_state"]["evidence_temporal_checks"]
        self.assertEqual(checks["benchmark_s4.evidence_ids"]["status"], "consistent")
        self.assertEqual(checks["creator_s4.evidence_ids"]["status"], "consistent")

        s4["benchmark_s4"]["evidence_ids"] = ["B_CROSS"]
        s4["creator_s4"]["evidence_ids"] = ["C_CROSS"]
        validate_stage_evidence_temporal_consistency(result)
        checks = s4["_postprocess_state"]["evidence_temporal_checks"]
        self.assertEqual(checks["benchmark_s4.evidence_ids"]["status"], "consistent")
        self.assertEqual(checks["creator_s4.evidence_ids"]["status"], "consistent")

    def test_s3_temporal_mismatch_is_checked_symmetrically(self) -> None:
        result = {
            "video_understanding": {
                "benchmark": {"evidence_units": [{"id": "B3", "time_range": "13.8s - 25.5s"}]},
                "creator": {"evidence_units": [{"id": "C3", "time_range": "13.8s - 25.5s"}]},
            },
            "stage_analysis": [
                {"stage": f"S{index}", "benchmark_time_range": "0.0s - 1.0s", "creator_time_range": "0.0s - 1.0s"}
                for index in range(1, 7)
            ],
        }
        result["stage_analysis"][2].update(
            {
                "benchmark_time_range": "25.5s - 38.3s",
                "creator_time_range": "25.5s - 38.3s",
                "benchmark_s3": {"evidence_ids": ["B3"]},
                "creator_s3": {"evidence_ids": ["C3"]},
            }
        )

        validate_stage_evidence_temporal_consistency(result)

        checks = result["stage_analysis"][2]["_postprocess_state"]["evidence_temporal_checks"]
        for role, evidence_id in (("benchmark", "B3"), ("creator", "C3")):
            check = checks[f"{role}_s3.evidence_ids"]
            self.assertEqual(check["status"], "state_conflict")
            self.assertEqual(check["reason_code"], "evidence_temporal_mismatch")
            self.assertEqual(check["conflicting_evidence_ids"], [evidence_id])

    def test_llm_stream_retries_cleanup_sensitive_artifacts_and_accept_only_completed_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text(json.dumps({"model": "test", "messages": []}), encoding="utf-8")
            calls: list[list[str]] = []
            stdin_values: list[str | bytes | None] = []
            response_meta: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append(command)
                stdin_values.append(kwargs.get("stdin_text"))
                callback = kwargs["stdout_callback"]
                assert callable(callback)
                if len(calls) == 1:
                    callback(b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n')
                else:
                    callback(
                        b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
                        b'data: [DONE]\n\n'
                    )
                return SimpleNamespace(returncode=0, stderr="__FLAYR_HTTP_STATUS__200\n", stdout="")

            with (
                mock.patch.object(
                    llm_api,
                    "validate_outbound_url",
                    return_value=SimpleNamespace(
                        hostname="example.test",
                        port=443,
                        resolved_addresses=("203.0.113.10", "2001:db8::10"),
                    ),
                ),
                mock.patch.object(llm_api, "run_command", side_effect=fake_run),
                mock.patch.object(llm_api.time, "sleep"),
                mock.patch("pathlib.Path.read_text", side_effect=AssertionError("payload was rebuilt as text")),
            ):
                raw = llm_api.call_llm_api(
                    "https://example.test/v1/chat/completions",
                    "secret",
                    payload_path,
                    raw_path,
                    response_meta=response_meta,
                )

            self.assertEqual(len(calls), 2)
            self.assertIn("--speed-limit", calls[0])
            self.assertIn("--speed-time", calls[0])
            self.assertEqual(calls[0][calls[0].index("--max-redirs") + 1], "0")
            self.assertIn("--resolve", calls[0])
            self.assertIn("example.test:443:203.0.113.10", calls[0])
            self.assertIn("example.test:443:[2001:db8::10]", calls[0])
            self.assertNotIn("-L", calls[0])
            self.assertEqual(calls[0][calls[0].index("--max-time") + 1], "1800")
            self.assertIn('"finish_reason": "stop"', raw)
            self.assertEqual(response_meta["transport_attempts"], 2)
            self.assertEqual(response_meta["transport_status"], "completed")
            self.assertTrue(response_meta["transport_retry_reasons"])
            self.assertFalse(raw_path.exists())
            self.assertEqual(stdin_values, ["Authorization: Bearer secret\n", "Authorization: Bearer secret\n"])
            self.assertNotIn("secret", " ".join(calls[0]))
            self.assertEqual(sorted(path.name for path in root.iterdir()), [])

    def test_fetch_json_completion_retries_provider_envelope_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text(json.dumps({"model": "test", "messages": []}), encoding="utf-8")
            responses = [
                json.dumps({"choices": []}),
                json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}),
            ]
            response_meta: dict[str, object] = {}
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            with mock.patch.object(pipeline, "call_llm_api", side_effect=responses), mock.patch.object(
                pipeline.time, "sleep"
            ):
                output = pipeline.fetch_json_completion(
                    args,
                    "secret",
                    payload_path,
                    response_path,
                    max_attempts=2,
                    response_meta=response_meta,
                )
            self.assertEqual(output, "{}")
            self.assertEqual(response_meta["completion_attempts"], 2)
            self.assertEqual(response_meta["status"], "completed")
            self.assertTrue(any("missing text output" in str(reason) for reason in response_meta["retry_reasons"]))

    def test_small_json_request_can_set_a_shorter_transport_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text("{}", encoding="utf-8")
            raw = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            response_meta: dict[str, object] = {}
            with mock.patch.object(pipeline, "call_llm_api", return_value=raw) as call:
                output = pipeline.fetch_json_completion(
                    args,
                    "secret",
                    payload_path,
                    response_path,
                    max_attempts=1,
                    request_max_time_seconds=240,
                    response_meta=response_meta,
                )
            self.assertEqual(output, "{}")
            self.assertEqual(response_meta["finish_reason"], "stop")
            self.assertEqual(call.call_args.kwargs["max_time_seconds"], 240)
            self.assertFalse(payload_path.exists())
            self.assertFalse(response_path.exists())

    def test_segmented_s1_validation_does_not_rewrite_hook_facts(self) -> None:
        result = {
            "video_understanding": {},
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_hook": {
                        "exists": True,
                        "landing_met": False,
                        "anchors_proposition": True,
                        "hook_boundary_seconds": 3.0,
                        "landing_reason": "原始理由",
                    },
                    "benchmark_hook": {
                        "exists": True,
                        "landing_met": True,
                        "anchors_proposition": True,
                        "hook_boundary_seconds": 2.0,
                        "landing_reason": "标杆理由",
                    },
                }
            ],
        }
        before = json.loads(json.dumps(result["stage_analysis"][0]))

        finalize_severity_after_repairs(result, {}, mutate_s1_facts=False)

        stage = result["stage_analysis"][0]
        self.assertEqual(stage["creator_hook"], before["creator_hook"])
        self.assertEqual(stage["benchmark_hook"], before["benchmark_hook"])
        self.assertEqual(stage["_postprocess_state"]["s1_hook_boundaries"]["status"], "validated")
        self.assertTrue(stage["_postprocess_state"]["s1_hook_boundaries"]["valid"])

    def test_fetch_json_completion_retries_a_failed_complete_transport_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text("{}", encoding="utf-8")
            raw = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            with (
                mock.patch.object(pipeline, "call_llm_api", side_effect=[SystemExit("incomplete stream"), raw]) as call,
                mock.patch.object(pipeline.time, "sleep"),
            ):
                output = pipeline.fetch_json_completion(args, "secret", payload_path, response_path, max_attempts=2)
            self.assertEqual(output, "{}")
            self.assertEqual(call.call_count, 2)
            self.assertFalse(payload_path.exists())
            self.assertFalse(response_path.exists())

    def test_fetch_json_completion_does_not_retry_after_shared_budget_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            with (
                mock.patch.object(
                    pipeline,
                    "call_llm_api",
                    side_effect=SystemExit("total wall time budget exceeded (1800s)"),
                ) as call,
                mock.patch.object(pipeline.time, "sleep") as sleep,
                self.assertRaises(SystemExit),
            ):
                pipeline.fetch_json_completion(args, "secret", payload_path, response_path, max_attempts=3)
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(payload_path.exists())
            self.assertFalse(response_path.exists())

    def test_fetch_json_completion_does_not_retry_after_http_403(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            with (
                mock.patch.object(
                    pipeline,
                    "call_llm_api",
                    side_effect=SystemExit(
                        "LLM streaming request failed: HTTP 403: Workspace endpoint access denied."
                    ),
                ) as call,
                mock.patch.object(pipeline.time, "sleep") as sleep,
                self.assertRaises(SystemExit),
            ):
                pipeline.fetch_json_completion(args, "secret", payload_path, response_path, max_attempts=3)
            self.assertEqual(call.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(payload_path.exists())
            self.assertFalse(response_path.exists())

    def test_reuse_preprocessing_reuses_existing_product_foundation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = {
                "category_profile": {"category": "散粉"},
                "product_profile": {"proof_contract": {"valid": True, "mode": "instant_visual"}},
            }
            args = SimpleNamespace(
                reuse_preprocessing=True,
                llm_model="test",
                llm_api_url="https://example.test/v1/chat/completions",
            )
            analysis = {"product": {"category": "散粉"}}
            cache_key = pipeline._product_foundation_cache_key(args, analysis)
            pipeline._write_cache_result(root / "product_foundation.json", {**cache_key, "foundation": cached})
            with mock.patch.object(pipeline, "fetch_json_completion") as request:
                foundation = pipeline.establish_product_foundation(args, analysis, root, "secret")
            self.assertEqual(foundation["category_profile"]["category"], "散粉")
            request.assert_not_called()

    def test_product_foundation_cache_key_tracks_request_not_unrelated_source_files(self) -> None:
        args = SimpleNamespace(
            llm_model="test",
            llm_api_url="https://example.test/v1/chat/completions",
        )
        analysis = {"product": {"category": "散粉", "target_market": "MY"}}
        with (
            mock.patch.object(pipeline, "_git_commit_sha", return_value="commit-a"),
            mock.patch.object(pipeline, "_cache_reference_digests", return_value={"payload.py": "digest-a"}),
        ):
            first = pipeline._product_foundation_cache_key(args, analysis)
        with (
            mock.patch.object(pipeline, "_git_commit_sha", return_value="commit-b"),
            mock.patch.object(pipeline, "_cache_reference_digests", return_value={"payload.py": "digest-b"}),
        ):
            second = pipeline._product_foundation_cache_key(args, analysis)

        self.assertEqual(first, second)
        self.assertIn("request_payload_sha256", first)
        self.assertNotIn("code_commit", first)
        self.assertNotIn("reference_digests", first)
        changed = pipeline._product_foundation_cache_key(
            args,
            {"product": {"category": "护脚霜", "target_market": "MY"}},
        )
        self.assertNotEqual(first["request_payload_sha256"], changed["request_payload_sha256"])

    def test_product_foundation_failure_is_explicit_not_a_silent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(
                reuse_preprocessing=False,
                llm_dry_run=False,
                llm_model="test",
                llm_api_url="https://example.test/v1/chat/completions",
                provider_replay_from=None,
                product_name="测试产品",
                product_category="散粉",
                product_notes="",
                target_user="",
                primary_selling_point="",
                core_selling_points="",
                product_price="",
                product_tier="mid",
                target_market="CN",
                proposition_key="",
                comparison_scope_override=None,
            )
            analysis = {"product": {"category": "散粉"}}
            with mock.patch.object(
                pipeline,
                "fetch_json_completion",
                side_effect=SystemExit("provider unavailable"),
            ):
                self.assertIsNone(pipeline.establish_product_foundation(args, analysis, root, "secret"))
            self.assertEqual(analysis["product_foundation_status"], "failed")
            self.assertTrue((root / "provider_product_foundation.json").is_file())

    def test_product_foundation_valid_response_is_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                reuse_preprocessing=False,
                llm_dry_run=False,
                llm_model="test",
                llm_api_url="https://example.test/v1/chat/completions",
                provider_replay_from=None,
                product_name="测试产品",
                product_category="散粉",
                product_notes="",
                target_user="",
                primary_selling_point="",
                core_selling_points="",
                product_price="",
                product_tier="mid",
                target_market="CN",
                proposition_key="",
                comparison_scope_override=None,
            )
            valid = {
                "category_profile": {"category": "散粉", "painpoints": ["油光"]},
                "product_profile": {
                    "physical_task": "把面部油光变成哑光定妆",
                    "core_selling_points": ["控油定妆"],
                    "short_video_proof_plan": {
                        "candidates": [
                            {
                                "id": "P1",
                                "selling_point": "控油定妆",
                                "visual_space": "high",
                                "functional_centrality": "high",
                                "comprehension_cost": "low",
                                "delivery_stage": "S4",
                                "proof_mode": "instant_visual",
                            }
                        ],
                        "s4_anchor_candidate_id": "P1",
                    },
                    "proof_contract": {
                        "anchor_candidate_id": "P1",
                        "mode": "instant_visual",
                        "consumer_outcome": "油光变哑光",
                        "signal_type": "state_change",
                        "observable_signal": "目标区域油光反光减弱",
                        "before_state": "油光明显",
                        "after_state": "反光减弱",
                        "proof_condition": "同一光线近景拍摄",
                    },
                },
            }
            analysis = {"product": {"category": "散粉"}}
            with mock.patch.object(
                pipeline,
                "provider_call_with_artifact",
                return_value=(valid, {}, "live"),
            ):
                foundation = pipeline.establish_product_foundation(args, analysis, Path(tmp), "secret")
            self.assertIsNotNone(foundation)
            self.assertEqual(analysis["product_foundation_status"], "completed")

    def test_product_foundation_semantic_failure_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                reuse_preprocessing=False,
                llm_dry_run=False,
                llm_model="test",
                llm_api_url="https://example.test/v1/chat/completions",
                provider_replay_from=None,
                product_name="测试产品",
                product_category="散粉",
                product_notes="",
                target_user="",
                primary_selling_point="",
                core_selling_points="",
                product_price="",
                product_tier="mid",
                target_market="CN",
                proposition_key="",
                comparison_scope_override=None,
            )
            invalid = {"category_profile": {"category": "散粉"}, "product_profile": {}}
            analysis = {"product": {"category": "散粉"}}
            with mock.patch.object(
                pipeline,
                "provider_call_with_artifact",
                side_effect=[(invalid, {}, "live"), (invalid, {}, "live")],
            ):
                foundation = pipeline.establish_product_foundation(args, analysis, Path(tmp), "secret")
            self.assertIsNotNone(foundation)
            self.assertEqual(analysis["product_foundation_status"], "degraded")

    def test_cache_record_rejects_stale_or_incomplete_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_path = root / "facts.json"
            key = {"cache_schema_version": 1, "source_sha256": "source"}
            fact = {"evidence_units": [{"id": "C1", "time_range": "0.0s - 1.0s"}]}
            pipeline._write_cache_result(cache_path, {**key, "fact_result": fact})
            self.assertEqual(pipeline._read_cache_result(cache_path, "fact_result", key), fact)

            corrupted = json.loads(cache_path.read_text(encoding="utf-8"))
            corrupted["fact_result"]["evidence_units"][0]["id"] = "C2"
            cache_path.write_text(json.dumps(corrupted), encoding="utf-8")
            self.assertIsNone(pipeline._read_cache_result(cache_path, "fact_result", key))

            pipeline._write_cache_result(cache_path, {**key, "fact_result": fact})
            failed = json.loads(cache_path.read_text(encoding="utf-8"))
            failed["completion_status"] = "failed"
            cache_path.write_text(json.dumps(failed), encoding="utf-8")
            self.assertIsNone(pipeline._read_cache_result(cache_path, "fact_result", key))

    def test_current_fact_cache_does_not_requalify_stage1(self) -> None:
        cached = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage1_qualification": {"status": "completed"},
            "stage1_acquisition": {"status": "complete"},
            "evidence_units": [],
        }
        cached_record = {
            "fact_result": cached,
            "stage_fact_artifacts": {
                "stage1_provider_creator_A.json": {
                    "schema_version": 1,
                    "status": "completed",
                    "provider_response": {},
                }
            },
        }
        analysis = {
            "videos": {
                "creator": {
                    "preprocess_fingerprint": {
                        "source_video": {"sha256": "source"},
                    },
                },
            },
        }
        args = SimpleNamespace(
            llm_dry_run=False,
            llm_model="test",
            llm_api_url="https://example.test/v1/chat/completions",
            llm_image_limit=8,
            _resource_budget=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(pipeline, "_read_cache_record", return_value=cached_record),
                mock.patch.object(pipeline, "_run_stage1_qualification") as qualify,
                mock.patch.object(pipeline, "_maybe_recover_video_facts", side_effect=lambda *values: values[-1]),
                mock.patch.object(pipeline, "freeze_stage_evidence"),
            ):
                result = pipeline.run_video_fact_extraction(
                    args,
                    analysis,
                    Path(tmp),
                    "secret",
                )
        qualify.assert_not_called()
        self.assertIs(result["creator"], cached)

    def test_video_fact_cache_key_binds_preprocess_file_content(self) -> None:
        args = SimpleNamespace(
            llm_model="test",
            llm_api_url="https://example.test/v1/chat/completions",
        )
        base = {
            "videos": {
                "creator": {
                    "preprocess_fingerprint": {"source_video": {"sha256": "source"}},
                    "preprocess_artifacts": {
                        "schema_version": 2,
                        "files": {
                            "transcript.srt": {
                                "size_bytes": 3,
                                "mtime_ns": 100,
                                "sha256": "old",
                            }
                        },
                    },
                }
            },
            "product_foundation": {},
        }
        changed = json.loads(json.dumps(base))
        changed["videos"]["creator"]["preprocess_artifacts"]["files"]["transcript.srt"]["sha256"] = "new"
        self.assertNotEqual(
            pipeline._video_fact_cache_key(args, base, "creator"),
            pipeline._video_fact_cache_key(args, changed, "creator"),
        )

    def test_stage_fact_artifacts_round_trip_through_cache_helpers(self) -> None:
        artifact = {"schema_version": 1, "status": "completed", "provider_response": {"ok": True}}
        with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
            source = Path(source_tmp)
            target = Path(target_tmp)
            (source / "stage1_provider_creator_A.json").write_text(
                json.dumps(artifact), encoding="utf-8"
            )
            snapshot = pipeline._stage_fact_artifacts_for_cache(source, "creator")
            self.assertEqual(snapshot["stage1_provider_creator_A.json"], artifact)
            self.assertTrue(
                pipeline._restore_stage_fact_artifacts_from_cache(
                    {"stage_fact_artifacts": snapshot}, target, "creator"
                )
            )
            self.assertEqual(
                json.loads((target / "stage1_provider_creator_A.json").read_text(encoding="utf-8")),
                artifact,
            )
            self.assertFalse(
                pipeline._restore_stage_fact_artifacts_from_cache(
                    {"stage_fact_artifacts": {}}, target, "creator"
                )
            )

    def test_ocr_uses_short_single_request_timeout_with_outer_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = root / "frame.jpg"
            # The media boundary now requires a real file signature; the test
            # only needs a minimally identifiable JPEG because the API call is mocked.
            frame.write_bytes(b"\xff\xd8\xff" + b"not-a-real-jpeg")
            with mock.patch.object(subtitle_track, "call_llm_api", side_effect=SystemExit("timeout")) as call:
                lines, status = subtitle_track.ocr_frame_with_retry(
                    frame,
                    "secret",
                    "https://example.test/v1/chat/completions",
                    "vision-test",
                    root,
                    0,
                )
            self.assertEqual(lines, [])
            self.assertTrue(status.startswith("ocr_request_failed:"))
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.kwargs["max_time_seconds"], 90)
            self.assertEqual(call.call_args.kwargs["low_speed_time_seconds"], 45)
            self.assertEqual(call.call_args.kwargs["retries"], 0)
            self.assertEqual(call.call_args.kwargs["request_id"], "ocr-000-2")
            self.assertIn("provider_meta", json.loads((root / "ocr_000_attempt2_meta.json").read_text(encoding="utf-8")))

    def test_ocr_payload_uses_provider_minimum_image_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frame = Path(tmp) / "frame.jpg"
            frame.write_bytes(b"\xff\xd8\xff" + b"not-a-real-jpeg")
            with mock.patch.object(
                subtitle_track, "image_to_data_url", return_value="data:image/jpeg;base64,AA=="
            ):
                payload = subtitle_track.build_ocr_payload(frame, "qwen3-vl-plus")
            image = payload["messages"][0]["content"][0]
            self.assertEqual(image["min_pixels"], 65536)
            self.assertEqual(image["max_pixels"], 1003520)

    def test_dashscope_qwen_capabilities_are_explicit_and_budget_is_provider_independent(self) -> None:
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        capabilities = llm_api.provider_capabilities(url, "qwen3-omni-flash")
        self.assertEqual(capabilities.profile, "dashscope_qwen_compatible")
        self.assertEqual(capabilities.confidence, "verified_matrix")
        self.assertTrue(llm_api.can_send_standalone_audio(url, "qwen3-omni-flash"))
        self.assertTrue(llm_api.can_analyze_native_audio(url, "qwen3-omni-flash"))
        self.assertFalse(llm_api.can_send_standalone_audio("https://example.test/v1/chat/completions", "vision-test"))
        self.assertEqual(full_analysis_output_budget("qwen3.6-plus"), 65536)
        self.assertEqual(full_analysis_output_budget("other-model"), 32768)
        self.assertEqual(full_analysis_output_fields("qwen3.6-plus"), {"max_completion_tokens": 65536})
        self.assertEqual(full_analysis_output_fields("other-model"), {"max_tokens": 32768})

    def test_cli_exposes_an_explicit_run_wall_time_budget(self) -> None:
        default_args = flayr.build_parser().parse_args(["compare", "--verification-stage", "production"])
        self.assertEqual(default_args.max_total_wall_time, 1800.0)
        self.assertEqual(default_args.asr_model, "fun-asr-flash-2026-06-15")
        extended_args = flayr.build_parser().parse_args(
            ["compare", "--max-total-wall-time", "3600", "--verification-stage", "production"]
        )
        self.assertEqual(extended_args.max_total_wall_time, 3600.0)

    def test_llm_transfer_closed_error_is_retryable(self) -> None:
        self.assertTrue(
            llm_api.is_retryable_error(
                "curl: (18) transfer closed with outstanding read data remaining"
            )
        )

    def test_imported_structured_s5_result_enables_source_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "analysis_result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "stage_analysis": [
                            {"stage": "S5 信任放大", "creator_s5": {}, "benchmark_s5": {}}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            analysis: dict[str, object] = {}
            captured: dict[str, object] = {}

            def fake_finalize(result: dict[str, object], incoming: dict[str, object], _: str) -> dict[str, object]:
                captured.update(incoming)
                return result

            with (
                mock.patch.object(pipeline, "finalize_analysis_result", side_effect=fake_finalize),
                mock.patch.object(pipeline, "apply_finalized_analysis_result"),
            ):
                pipeline.merge_analysis_result(analysis, result_path, "")

            self.assertIs(captured["s5_source_signals_required"], True)

    def test_fact_summary_falls_back_to_locked_multimodal_evidence(self) -> None:
        normalized = normalize_video_fact_result(
            "benchmark",
            {
                "evidence_units": [
                    {
                        "id": "B3",
                        "time_range": "3.0s - 5.0s",
                        "information": "",
                        "visual_fact": "镜头展示果汁流入杯中。",
                        "voiceover": "Mesin ini senang cuci.",
                        "voiceover_zh": "这台机器容易清洗。",
                        "subtitle_fact": "SENANG CUCI",
                        "audio_fact": "水流声。",
                    }
                ]
            },
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        information = normalized["evidence_units"][0]["information"]
        self.assertIn("果汁流入杯中", information)
        self.assertIn("这台机器容易清洗", information)
        self.assertIn("SENANG CUCI", information)

    @staticmethod
    def _proposition_foundation() -> dict[str, object]:
        return {
            "category_profile": {"painpoints": ["油光", "脱妆"]},
            "product_profile": {
                "physical_task": "把面部油光变成哑光定妆",
                "hook_proposition": "油光变哑光",
                "core_selling_points": ["控油定妆"],
                "short_video_proof_plan": {
                    "candidates": [
                        {"id": "P1", "selling_point": "控油定妆", "delivery_stage": "S4"}
                    ],
                    "s4_anchor_candidate_id": "P1",
                },
                "visual_proof_points": [
                    {
                        "priority": "primary",
                        "proof_target": "油光变哑光",
                        "related_selling_points": ["控油定妆"],
                    }
                ],
                "trust_multipliers": ["持妆记录"],
            },
        }

    def test_product_proposition_contract_is_stable_and_stage_scoped(self) -> None:
        foundation = self._proposition_foundation()
        brand = {"propositions": ["出油后快速哑光"], "painpoints": ["油光"]}
        first = build_product_proposition_contract(foundation, brand)
        second = build_product_proposition_contract(foundation, brand)

        self.assertEqual(first, second)
        ids = {item["id"] for item in first["propositions"]}
        self.assertTrue({"hook.1", "pain.1", "role.1", "selling.1", "proof.1"}.issubset(ids))
        self.assertIn("proof.1", first["stages"]["S4"]["allowed_ids"])
        self.assertIn("selling.1", first["stages"]["S4"]["allowed_ids"])
        self.assertIn("selling.1", first["stages"]["S2"]["allowed_ids"])
        self.assertNotIn("trust.1", first["stages"]["S5"]["allowed_ids"])
        self.assertIn("trust.1", first["stages"]["S5"]["trust_evidence_ids"])

    def test_product_proposition_contract_reaches_comparison_and_repair(self) -> None:
        analysis = {
            "product_foundation": self._proposition_foundation(),
            "brand_proposition": {"propositions": ["出油后快速哑光"], "painpoints": ["油光"]},
            "videos": {},
        }
        comparison = build_llm_comparison_payload("test", "input", {}, analysis)
        comparison_content = comparison["messages"][1]["content"]
        comparison_text = comparison_content[0]["text"] if isinstance(comparison_content, list) else comparison_content
        repair = build_llm_repair_payload("test", "{}", "error", "input", analysis=analysis)
        repair_text = repair["messages"][1]["content"]

        self.assertIn("本品命题引用合同", comparison_text)
        self.assertIn('"hook.1"', comparison_text)
        self.assertIn("本品命题引用合同", repair_text)
        self.assertIn('"proof.1"', repair_text)
        self.assertIn("具体未解问题", comparison_text)
        self.assertIn("答案可在 S2 承接", comparison_text)

    def test_repair_prompt_enforces_stage_evidence_ownership(self) -> None:
        repair = build_llm_repair_payload("test", "{}", "error", "input")
        repair_text = repair["messages"][0]["content"]

        self.assertIn("只能引用对应侧、时间与该阶段 time_range 相交", repair_text)
        self.assertIn("嵌套 flag 的 evidence_ids 必须是该阶段主 evidence_ids 的子集", repair_text)
        self.assertIn("S4 不得把 S5 的用户评论、认证或反馈引用成效果证据", repair_text)
        self.assertIn("effect_evidence_state(none/result_only/verified/uncertain)", repair_text)

    def test_brand_proposition_resolves_validation_run_names(self) -> None:
        self.assertEqual(resolve_brand_key("validation-are_xie"), "are_xie")
        self.assertEqual(resolve_brand_key("scope-probe-carslan-b0"), "carslan")
        self.assertEqual(resolve_brand_key("sample-youkoubo-c2"), "juicer")
        brand = load_brand_proposition(Path("/tmp/validation-are_xie"))
        self.assertIsNotNone(brand)
        self.assertIn("经期腹痛", brand["painpoints"])

    def test_explicit_proposition_key_does_not_depend_on_run_directory(self) -> None:
        online_run = Path("/tmp/tenant-42/run-019f1e50")
        brand = load_brand_proposition(online_run, "are_xie")
        self.assertIsNotNone(brand)
        self.assertIn("经期腹痛", brand["painpoints"])
        self.assertIsNone(load_brand_proposition(online_run))

    def test_product_skus_under_one_brand_keep_distinct_proposition_keys(self) -> None:
        lip = load_brand_proposition(Path("/tmp/tenant-42/run-019f1e50"), "colorkey_lip_mud")
        mask = load_brand_proposition(Path("/tmp/tenant-42/run-019f1e50"), "colorkey")
        self.assertIsNotNone(lip)
        self.assertIsNotNone(mask)
        self.assertIn("丝绒奶油哑光妆效", lip["propositions"])
        self.assertNotIn("敷后水润通透", lip["propositions"])
        self.assertIn("敷后水润通透", mask["propositions"])

    def test_new_validation_products_bind_explicit_proposition_keys(self) -> None:
        manifest = json.loads((ROOT / "references" / "validation-inputs.json").read_text(encoding="utf-8"))
        sample_items = manifest.get("samples", []) if isinstance(manifest, dict) else manifest
        samples = {item["id"]: item for item in sample_items if isinstance(item, dict) and item.get("id")}
        expected_keys = {
            "colorblu-c0": "colorblu_waterproof_sealant",
            "colorblu-c1": "colorblu_waterproof_sealant",
            "carslan-powder-c0": "carslan",
            "carslan-powder-c1": "carslan",
        }
        for sample_id, proposition_key in expected_keys.items():
            self.assertEqual(samples[sample_id].get("proposition_key"), proposition_key)
            self.assertIsNotNone(load_brand_proposition(Path("/tmp/online-run"), proposition_key))

    def test_invalid_new_proof_contract_cannot_fallback_to_legacy_visual_claim(self) -> None:
        foundation = self._proposition_foundation()
        profile = foundation["product_profile"]
        profile["visual_proof_points"] = []
        profile["core_visual_proposition"] = "旧字段视觉结果"
        profile["proof_contract"] = {"valid": False, "validation_reason": "invalid"}
        contract = build_product_proposition_contract(foundation)
        self.assertFalse(any(item["kind"] == "proof" for item in contract["propositions"]))

        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        for role in ("creator", "benchmark"):
            stages[3][f"{role}_s4"] = {
                "effect_visible": False if role == "benchmark" else True,
                "effect_salience": "strong",
                "effect_proposition_matched": True,
                "effect_attribution_supported": True,
            }
            stages[5][f"{role}_s6"] = {"module_type": "D", "depends_on_valid_s4": True}
        result = {
            "category_profile": foundation["category_profile"],
            "product_profile": profile,
            "stage_analysis": stages,
        }
        materialize_cross_stage_inputs(result, {})
        self.assertFalse(result["cross_stage_state"]["roles"]["creator"]["s4_output_available"])
        self.assertFalse(stages[5]["creator_s6"]["computed_depends_on_valid_s4"])

    def test_proof_contract_allows_natural_outcome_but_rejects_compound_signal(self) -> None:
        base = {
            "anchor_candidate_id": "P1",
            "mode": "instant_visual",
            "consumer_outcome": "原有状态被覆盖并呈现目标效果",
            "signal_type": "state_change",
            "observable_signal": "目标区域色彩从暗淡变为饱和",
            "before_state": "未覆盖",
            "after_state": "均匀覆盖",
            "proof_condition": "同一光线近景拍摄",
        }
        self.assertTrue(normalize_proof_contract(base)["valid"])

        compound = {**base, "observable_dimension": "色彩覆盖度与纹理平滑度"}
        normalized = normalize_proof_contract(compound)
        self.assertFalse(normalized["valid"])
        self.assertIn("一个可观察维度", normalized["validation_reason"])

        same_object_state = {**base, "observable_dimension": "刷头完整性与存在状态"}
        normalized = normalize_proof_contract(same_object_state)
        self.assertTrue(normalized["valid"])
        self.assertEqual(normalized["observable_dimension"], "刷头状态")

        process_evidence = {
            **base,
            "mode": "process_result",
            "signal_type": "process_event",
            "observable_dimension": "刷头卫生状态",
            "observable_signal": "旧刷头无需手触被新刷头替换，使用后直接丢弃",
        }
        self.assertTrue(normalize_proof_contract(process_evidence)["valid"])

        different_attributes = {**base, "observable_dimension": "刷头卫生状态与更换便捷性"}
        normalized = normalize_proof_contract(different_attributes)
        self.assertFalse(normalized["valid"])
        self.assertIn("一个可观察维度", normalized["validation_reason"])

    def test_step0_prompt_assigns_proof_contract_field_roles(self) -> None:
        analysis = {"product": {"name": "一次性刷头", "category": "清洁用品"}}
        prompt = build_product_foundation_payload("test-model", analysis)["messages"][1]["content"][0]["text"]
        self.assertIn("observable_dimension 只写一个名词性、可复核的测量轴", prompt)
        self.assertIn("刷头卫生状态", prompt)
        self.assertIn("过程动作写 observable_signal", prompt)
        self.assertIn("拍摄条件不能写进 observable_signal", prompt)
        self.assertIn("不表示 S5 必须出现、达人必须提供背书", prompt)

        repair_prompt = build_product_foundation_repair_payload(
            "test-model",
            analysis,
            {"proof_contract": {"observable_dimension": "刷头替换与丢弃的卫生状态"}},
            "observable_dimension 必须只保留一个可观察维度",
        )["messages"][1]["content"][0]["text"]
        self.assertIn("不要只把 dimension 中的‘替换’改成‘交接’", repair_prompt)
        self.assertIn("只修 proof_contract 及其直接派生的 visual_proof_points", repair_prompt)
        self.assertIn("旧刷头无需手触被新刷头替换，使用后直接丢弃", repair_prompt)

    def test_inferred_s4_contract_cannot_authoritatively_override_stage_analysis(self) -> None:
        profile = normalize_product_profile(
            {
                "proof_contract_source": "inferred",
                "proof_contract": {"valid": True, "mode": "instant_visual"},
            }
        )
        self.assertEqual(profile["proof_contract_source"], "inferred")
        reason = _visual_verifier_skip_reason({"product_profile": profile})
        self.assertIn("模型推断", reason)

    def test_operator_selling_points_do_not_upgrade_model_selected_proof_contract(self) -> None:
        """产品卖点来自运营，不等于运营确认了唯一 S4 视觉合同。"""
        foundation = {
            "product_profile": normalize_product_profile(
                {
                    "proof_contract_source": "curated",
                    "proof_contract": {"valid": True, "mode": "instant_visual"},
                }
            )
        }
        pipeline._stamp_proof_contract_source(
            foundation,
            {"product": {"core_selling_points": "显色、柔雾、持妆"}},
        )
        self.assertEqual(foundation["product_profile"]["proof_contract_source"], "inferred")

    def test_derived_execution_is_written_to_severity_trace(self) -> None:
        from flayr_core.postprocess.derive import derive_severity_from_facts

        stage = {
            "stage": "S2 产品引出",
            "severity": "small",
            "creator_execution": 0.5,
            "benchmark_execution": 1.0,
            "creator_s2": {"exists": True, "handoff_met": True, "s1_s2_compatible": True,
                           "product_identity_clear": True, "product_role_clear": False},
            "benchmark_s2": {"exists": True, "handoff_met": True, "s1_s2_compatible": True,
                              "product_identity_clear": True, "product_role_clear": True},
        }
        result = {"stage_analysis": [stage]}
        derive_severity_from_facts(result)
        trace = stage["severity_derivation"]
        self.assertEqual(trace["derived_creator_execution"], 1.0)
        self.assertEqual(trace["derived_benchmark_execution"], 2.0)

    def test_video_identity_and_comparison_scope_survive_normalization(self) -> None:
        facts = normalize_video_fact_result(
            "benchmark",
            {
                "product_identity": {
                    "brand_or_product_name": "Simplus",
                    "product_category": "榨汁机",
                    "form_factor": "慢速榨汁机",
                    "identity_basis": "visible",
                    "confidence": "high",
                },
                "evidence_units": [{"id": "B1", "time_range": "0.0s - 1.0s", "information": "展示产品"}],
            },
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        self.assertEqual(facts["product_identity"]["form_factor"], "慢速榨汁机")

        normalized = normalize_analysis_result(
            {
                "comparison_eligibility": {
                    "scope": "cross_product",
                    "direct_product_stages": ["S1", "S3", "bad"],
                    "reason": "产品形态不同",
                },
                "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
                "improvements": [{"title": "测试建议", "time_range": "0.0s - 1.0s"}],
            }
        )
        self.assertEqual(normalized["comparison_eligibility"]["scope"], "cross_product")
        self.assertEqual(normalized["comparison_eligibility"]["direct_product_stages"], [])
        self.assertEqual(normalized["comparison_contract"]["overall_status"], "not_comparable")

    def test_same_product_family_forces_all_stages_direct(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "same_product_family",
                "substitution_relation": "uncertain",
                "stage_eligibility": {"S3": {"status": "not_comparable"}},
                "reason": "同系列粉饼，仅黑色与银色包装不同。",
            }
        )
        self.assertEqual(contract["overall_status"], "full_direct")
        self.assertEqual(contract["comparable_stages"], ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertTrue(all(item["status"] == "direct" for item in contract["stage_eligibility"].values()))

    def test_scope_normalization_does_not_keep_provider_not_comparable_basis(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "same_product_family",
                "substitution_relation": "same_solution",
                "stage_eligibility": {
                    "S6": {
                        "status": "not_comparable",
                        "basis": "达人侧资格未知，无法比较 CTA。",
                    }
                },
            }
        )
        s6 = contract["stage_eligibility"]["S6"]
        self.assertEqual(s6["status"], "direct")
        self.assertIn("证据资格", s6["basis"])
        self.assertNotIn("无法比较", s6["basis"])

    def test_s5_not_applicable_requires_code_owned_bilateral_fact_marker(self) -> None:
        provider_scope = normalize_comparison_contract(
            {
                "identity_relation": "exact_product",
                "substitution_relation": "same_solution",
                "stage_eligibility": {
                    "S5": {"status": "not_applicable", "basis": "低决策品类通常不需要背书"}
                },
            }
        )
        self.assertEqual(provider_scope["stage_eligibility"]["S5"]["status"], "direct")
        self.assertNotIn("status_source", provider_scope["stage_eligibility"]["S5"])

        code_owned_scope = normalize_comparison_contract(
            {
                "identity_relation": "exact_product",
                "substitution_relation": "same_solution",
                "stage_eligibility": {
                    "S5": {
                        "status": "not_applicable",
                        "status_source": "bilateral_stage1_facts",
                        "basis": "双方 Stage1 完整且均 absent",
                    }
                },
            },
            allow_code_owned_s5_scope=True,
        )
        self.assertEqual(code_owned_scope["stage_eligibility"]["S5"]["status"], "not_applicable")
        self.assertEqual(
            code_owned_scope["stage_eligibility"]["S5"]["status_source"],
            "bilateral_stage1_facts",
        )

        spoofed_scope = normalize_comparison_contract(
            {
                "identity_relation": "exact_product",
                "substitution_relation": "same_solution",
                "stage_eligibility": {
                    "S5": {
                        "status": "not_applicable",
                        "status_source": "bilateral_stage1_facts",
                    }
                },
            }
        )
        self.assertEqual(spoofed_scope["stage_eligibility"]["S5"]["status"], "direct")
        self.assertNotIn("status_source", spoofed_scope["stage_eligibility"]["S5"])

    def test_strong_substitute_requires_all_shared_job_gates(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {
                    "same_consumer_job": True,
                    "same_target_object": True,
                    "same_desired_outcome": True,
                    "same_purchase_decision": False,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {"S3": {"status": "structural"}},
            }
        )
        self.assertEqual(contract["substitution_relation"], "partial_substitute")
        self.assertEqual(contract["overall_status"], "selective_structural")

    def test_unrelated_products_have_no_comparable_stages(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "different_product",
                "substitution_relation": "none",
                "stage_eligibility": {"S1": {"status": "structural"}, "S6": {"status": "structural"}},
                "reason": "防水胶与粉饼不共享任务。",
            }
        )
        self.assertEqual(contract["overall_status"], "not_comparable")
        self.assertEqual(contract["comparable_stages"], [])
        self.assertTrue(all(item["status"] == "not_comparable" for item in contract["stage_eligibility"].values()))

    def test_non_comparable_gate_stops_before_stage_analysis(self) -> None:
        contract = normalize_comparison_contract(
            {"identity_relation": "different_product", "substitution_relation": "none", "reason": "任务不同。"}
        )
        analysis = {"stage_analysis": [{"stage": "S1"}], "improvements": [{"target_stage": "S1"}]}
        with tempfile.TemporaryDirectory() as tmp:
            pipeline._apply_non_comparable_result(analysis, {"benchmark": {}, "creator": {}}, contract, Path(tmp))
            self.assertTrue((Path(tmp) / "comparison_rejection.json").is_file())
        self.assertEqual(analysis["analysis_status"], "not_comparable")
        self.assertEqual(analysis["stage_analysis"], [])
        self.assertEqual(analysis["improvements"], [])

    def test_stage1_event_checks_keep_only_catalog_events_and_real_evidence(self) -> None:
        facts = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [
                    {"id": "C1", "time_range": "0.0s - 2.0s", "information": "真实涂抹动作"},
                ],
                "structure_event_checks": [
                    {"module_id": "S3-A", "present": True, "evidence_ids": ["C1", "fake"]},
                    {"module_id": "S4-Z", "present": True, "evidence_ids": ["C1"]},
                ],
            },
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        self.assertEqual(
            facts["structure_event_checks"][0],
            {
                "module_id": "S3-A",
                "stage": "S3",
                "status": "present",
                "coverage": "complete",
                "present": True,
                "evidence_ids": ["C1"],
                "observed_signals": [],
                "missing_signals": [],
            },
        )
        self.assertEqual(len(facts["structure_event_checks"]), 11)
        self.assertNotIn("S4-Z", [item["module_id"] for item in facts["structure_event_checks"]])

    def test_final_video_understanding_preserves_locked_stage1_audit_fields(self) -> None:
        normalized = normalize_analysis_result(
            {
                "video_understanding": {
                    "benchmark": {
                        "evidence_units": [{"id": "B1", "time_range": "0.0s - 2.0s", "information": "涂抹裂缝"}],
                        "evidence_checklist": [
                            {"item": "proposition: 裂缝修补", "covered": True, "evidence_ids": ["B1", "missing"], "channels": ["visual"]},
                        ],
                        "structure_event_checks": [
                            {"module_id": "S3-A", "present": True, "evidence_ids": ["B1", "missing"]},
                        ],
                    },
                    "creator": {"evidence_units": []},
                },
                "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
                "improvements": [{"title": "测试建议", "time_range": "0.0s - 1.0s"}],
            }
        )
        benchmark = normalized["video_understanding"]["benchmark"]
        self.assertEqual(benchmark["evidence_checklist"][0]["evidence_ids"], ["B1"])
        self.assertEqual(
            benchmark["structure_event_checks"][0],
            {
                "module_id": "S3-A",
                "stage": "S3",
                "status": "present",
                "coverage": "complete",
                "present": True,
                "evidence_ids": ["B1"],
                "observed_signals": [],
                "missing_signals": [],
            },
        )
        self.assertEqual(len(benchmark["structure_event_checks"]), 11)

    def test_locked_comparison_scope_reaches_and_overrides_main_analysis(self) -> None:
        facts = {
            "benchmark": {
                "product_identity": {"product_category": "挂烫机", "form_factor": "手持挂烫机"},
                "evidence_units": [{"id": "B1", "time_range": "0-2s", "information": "展示熨烫过程"}],
            },
            "creator": {"product_identity": {"product_category": "地面清洁机", "form_factor": "吸尘清洁机"}},
        }
        scope_payload = build_comparison_eligibility_payload("test", facts)
        scope_text = scope_payload["messages"][1]["content"][0]["text"]
        self.assertIn("手持挂烫机", scope_text)
        self.assertIn("包装、颜色、色号", scope_text)
        self.assertIn("同一次购买决策可二选一", scope_text)
        self.assertIn("S5 按 structure_library_full.md 定义为可选的‘信任放大’", scope_text)
        self.assertIn("结构库的跳过条件只指导编排，不是事实先验", scope_text)
        self.assertIn("双方 Stage1 覆盖完整且 S5 都是 absent", scope_text)
        self.assertIn("一侧 present、另一侧 absent", scope_text)
        self.assertIn("展示熨烫过程", scope_text)

        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        result = {"stage_analysis": stages, "comparison_eligibility": {"scope": "same_product"}}
        analysis = {"comparison_eligibility": {"scope": "cross_product", "direct_product_stages": ["S1"], "reason": "形态不同"}}
        stamp_comparison_eligibility(result, analysis)
        self.assertEqual(result["comparison_eligibility"]["scope"], "cross_product")

    def test_stage2_prompt_prioritizes_bilateral_s5_scope_over_category_prior(self) -> None:
        payload = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        prompt = payload["messages"][1]["content"]
        self.assertIn("S5 范围优先级（代码合同，优先于商业框架中的品类判例）", prompt)
        self.assertIn("只有双方覆盖完整且均为 absent 才由代码标记 not_applicable", prompt)
        self.assertIn("品类和购买动机只能影响差距权重与解释", prompt)

        structure_library = (ROOT / "structure_library_full.md").read_text(encoding="utf-8")
        self.assertIn("编排模块与既有视频评估解耦", structure_library)
        self.assertIn("既有视频只有在画面/口播中实际出现且来源可核验时", structure_library)

    def test_scope_identity_payload_excludes_video_and_audio(self) -> None:
        analysis = {
            "product": {"name": "测试产品"},
            "videos": {"creator": {"work_dir": "", "duration_seconds": 10}},
        }
        payload = build_video_identity_payload(
            "test",
            "creator",
            analysis,
            [{"label": "creator frame", "data_url": "data:image/jpeg;base64,AA=="}],
        )
        content = payload["messages"][1]["content"]
        types = [item.get("type") for item in content if isinstance(item, dict)]
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn("video_url", types)
        self.assertNotIn("input_audio", types)
        self.assertIn("image_url", types)

    def test_unrelated_products_exclude_all_stages_and_improvements(self) -> None:
        stages = [{"stage": f"S{index}", "severity": "large"} for index in range(1, 7)]
        result = {
            "comparison_eligibility": {
                "scope": "cross_product",
                "direct_product_stages": ["S1", "S6"],
                "reason": "产品关键形态不同",
            },
            "stage_analysis": stages,
            "improvements": [
                {"target_stage": "S3", "title": "使用过程建议", "priority": 1},
                {"target_stage": "S6", "title": "CTA 建议", "priority": 2},
            ],
        }
        apply_comparison_eligibility(result)
        stabilize_improvement_priorities(result)

        self.assertEqual(stages[0]["comparison_status"], "not_directly_comparable")
        self.assertEqual(stages[1]["comparison_status"], "not_directly_comparable")
        self.assertEqual(stages[4]["comparison_status"], "not_directly_comparable")
        self.assertEqual(result["improvements"], [])
        self.assertEqual(result["comparison_contract"]["overall_status"], "not_comparable")
        skipped, reason = stage_skipped(stages[2])
        self.assertTrue(skipped)
        self.assertIn("不输出差距判断", reason)

    def test_strong_substitute_uses_stage_level_contract(self) -> None:
        eligibility = normalize_comparison_eligibility(
            {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {
                    "same_consumer_job": True,
                    "same_target_object": True,
                    "same_desired_outcome": True,
                    "same_purchase_decision": True,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {
                    **{stage: {"status": "structural", "basis": "共同任务"} for stage in ("S1", "S2", "S3", "S4", "S6")},
                    "S5": {"status": "not_applicable", "basis": "双方均无背书"},
                },
                "reason": "运营确认同类同任务。",
                "scope_origin": "operator_certified",
                "facts_scope": "cross_product",
                "facts_reason": "关键形态不同。",
            }
        )
        self.assertEqual(eligibility["direct_product_stages"], ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertEqual(eligibility["scope_origin"], "operator_certified")
        self.assertEqual(eligibility["facts_scope"], "cross_product")

        stages = [{"stage": f"S{index}", "severity": "large"} for index in range(1, 7)]
        result = {
            "comparison_eligibility": eligibility,
            "stage_analysis": stages,
            "improvements": [
                {"target_stage": "S3", "title": "使用过程建议", "priority": 1},
                {"target_stage": "S5", "title": "背书建议", "priority": 2},
            ],
        }
        apply_comparison_eligibility(result)
        stabilize_improvement_priorities(result)
        self.assertEqual(stages[2]["comparison_basis"], "structure_execution")
        self.assertEqual(stages[4]["comparison_status"], "structural_comparison")
        self.assertEqual([item["target_stage"] for item in result["improvements"]], ["S3", "S5"])
        self.assertIn("仅在共同消费者任务下比较内容执行", result["comparison_scope_note"])

    def test_s5_report_scope_is_not_inferred_from_hard_endorsement_text(self) -> None:
        stage = {
            "stage": "S5 信任放大",
            "severity": "small",
            "creator_summary": "双方均无硬背书。",
            "benchmark_summary": "双方均无硬背书。",
            "gap": "双方均无硬背书。",
            "severity_derivation": {"reason": "双方均无硬背书。"},
            "comparison_status": "structural_comparison",
            "stage_evidence_gate": {"status": "grounded"},
        }
        skipped, _ = stage_skipped(stage)
        self.assertFalse(skipped)

        stage["comparison_status"] = "not_applicable"
        stage["comparison_reason"] = "双方 Stage1 均已完整核验为 absent。"
        stage["stage_evidence_gate"] = {"status": "not_applicable"}
        skipped, reason = stage_skipped(stage)
        # A provider/model-shaped not_applicable is not enough to close S5.
        # The bilateral Stage1 fact marker must be present on the stage.
        self.assertFalse(skipped)
        self.assertEqual(reason, "")

        stage["comparison_contract"] = {
            "status": "not_applicable",
            "status_source": "bilateral_stage1_facts",
        }
        skipped, reason = stage_skipped(stage)
        self.assertTrue(skipped)
        self.assertIn("absent", reason)

    def test_s5_code_owned_closure_is_visible_in_scope_summary(self) -> None:
        summary = comparison_scope_summary(
            {
                "identity_relation": "exact_product",
                "substitution_relation": "same_solution",
                "stage_eligibility": {
                    **{
                        stage: {"status": "direct"}
                        for stage in ("S1", "S2", "S3", "S4", "S6")
                    },
                    "S5": {
                        "status": "not_applicable",
                        "status_source": "bilateral_stage1_facts",
                    },
                },
            },
            allow_code_owned_s5_scope=True,
        )
        self.assertIn("本轮不涉及：S5 信任放大", summary)

    def test_report_summary_requires_code_owned_s5_gate(self) -> None:
        base = {
            "analysis_run_state": "completed",
            "comparison_contract": {
                "identity_relation": "exact_product",
                "substitution_relation": "same_solution",
                "stage_eligibility": {
                    stage: {"status": "direct"}
                    for stage in ("S1", "S2", "S3", "S4", "S6")
                } | {
                    "S5": {
                        "status": "not_applicable",
                        "status_source": "bilateral_stage1_facts",
                    }
                },
            },
            "stage_analysis": [
                {
                    "stage": "S5 信任放大",
                    "comparison_status": "not_applicable",
                    "comparison_contract": {
                        "status": "not_applicable",
                        "status_source": "bilateral_stage1_facts",
                    },
                    "stage_evidence_gate": {
                        "status": "not_applicable",
                        "source": "code",
                    },
                }
            ],
        }
        self.assertIn("本轮不涉及：S5 信任放大", executive_summary(base))
        untrusted = json.loads(json.dumps(base))
        untrusted["stage_analysis"][0]["stage_evidence_gate"]["source"] = "model"
        self.assertNotIn("本轮不涉及：S5 信任放大", executive_summary(untrusted))

    def test_same_task_structure_override_preserves_facts_audit(self) -> None:
        eligibility = pipeline._apply_operator_scope_override(
            {"scope": "cross_product", "direct_product_stages": ["S1"], "reason": "关键形态不同。"},
            "same_task_structure",
        )
        self.assertEqual(eligibility["scope"], "same_task_structure")
        self.assertEqual(eligibility["direct_product_stages"], ["S1", "S2", "S3", "S4", "S6"])
        self.assertEqual(eligibility["scope_origin"], "operator_certified")
        self.assertEqual(eligibility["facts_scope"], "cross_product")

    def test_same_task_structure_override_makes_stage_contract_consistent(self) -> None:
        eligibility = pipeline._apply_operator_scope_override(
            {
                "identity_relation": "different_product",
                "substitution_relation": "none",
                "shared_job": {
                    "same_consumer_job": False,
                    "same_target_object": False,
                    "same_desired_outcome": False,
                    "same_purchase_decision": False,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {
                    **{
                        stage: {"status": "not_comparable", "basis": "产品身份不同"}
                        for stage in ("S1", "S2", "S3", "S4", "S6")
                    },
                    "S5": {"status": "not_applicable", "basis": "双方均无背书"},
                },
                "reason": "产品身份不同。",
            },
            "same_task_structure",
        )
        for stage in ("S1", "S2", "S3", "S4", "S6"):
            self.assertEqual(eligibility["stage_eligibility"][stage]["status"], "structural")
        self.assertEqual(eligibility["stage_eligibility"]["S5"]["status"], "structural")
        self.assertTrue(eligibility["shared_job"]["same_consumer_job"])
        self.assertIn("运营确认", eligibility["shared_job"]["reason"])

    def test_facts_eligibility_keeps_scope_and_audit_fields_consistent(self) -> None:
        eligibility = pipeline._stamp_facts_eligibility(
            {
                "scope": "same_product",
                "direct_product_stages": ["S1", "S2", "S3", "S4", "S5", "S6"],
                "reason": "品牌、品类及关键形态一致。",
                "scope_origin": "operator_certified",
                "facts_scope": "uncertain",
            }
        )
        self.assertEqual(eligibility["scope_origin"], "facts")
        self.assertEqual(eligibility["facts_scope"], "same_product")
        self.assertEqual(eligibility["facts_reason"], "品牌、品类及关键形态一致。")

    def test_improvement_placeholder_is_not_reportable(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}", "severity": "medium"} for index in range(1, 7)],
            "improvements": [
                {"target_stage": "S1", "title": "有效建议", "problem": "明确问题", "priority": 2},
                {"target_stage": "", "title": "（LLM 未填写 title，需人工补充）", "problem": "（LLM 未填写 problem，需人工补充）", "priority": 1},
            ],
        }
        stabilize_improvement_priorities(result)
        self.assertEqual([item["title"] for item in result["improvements"]], ["有效建议"])

    def test_report_summary_does_not_render_legacy_cross_product_claim(self) -> None:
        analysis = {
            "comparison_eligibility": {
                "scope": "cross_product",
                "direct_product_stages": ["S1", "S6"],
                "reason": "产品形态不同",
            },
            "one_line_summary": "达人效果弱于标杆。",
        }
        summary = executive_summary(analysis)
        self.assertIn("不输出 S1-S6 差距结论", summary)
        self.assertNotIn("达人效果弱于标杆", summary)

    def test_report_summary_marks_same_task_structure_limit_without_dropping_conclusion(self) -> None:
        analysis = {
            "comparison_eligibility": {
                "scope": "same_task_structure",
                "direct_product_stages": ["S1", "S2", "S3", "S4", "S6"],
                "reason": "运营确认同类同任务。",
            },
            "one_line_summary": "达人缺少完整的使用与效果证据。",
        }
        summary = executive_summary(analysis)
        self.assertIn("存在强替代关系", summary)
        self.assertIn("达人缺少完整的使用与效果证据", summary)

    def test_child_toothpaste_scope_ignores_prompt_examples(self) -> None:
        generic_input = """## 产品信息

- 产品名：MMX吸尘清洗机
- 品类：家电清洁

## 通用规则

儿童牙膏和 toothpaste 只是在这里作为示例出现。
"""
        child_input = """## 产品信息

- 产品名：儿童牙膏
- 品类：toothpaste

## 通用规则

无关示例。
"""
        self.assertFalse(is_child_toothpaste_context(generic_input))
        self.assertTrue(is_child_toothpaste_context(child_input))

    def test_s6_module_mismatch_is_not_counted_as_standard_delivery(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "creator_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "ending_position_met": True,
                    "module_fit_met": False,
                },
                "benchmark_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "ending_position_met": True,
                    "module_fit_met": True,
                },
            }
        )
        result = {"stage_analysis": stages}
        materialize_quality_audits(result, {})
        self.assertEqual(stages[5]["creator_absolute_status"], "weak")
        self.assertEqual(stages[5]["computed_stage_standard_delivery"], "benchmark_only")

    def test_structured_flags_replace_stale_model_stage_delivery(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "stage_standard_delivery": "benchmark_only",
                "creator_s6": {"exists": False},
                "benchmark_s6": {"exists": False},
            }
        )
        result = {"stage_analysis": stages}

        materialize_quality_audits(result, {})

        self.assertEqual(stages[5]["stage_standard_delivery"], "none")
        self.assertEqual(stages[5]["model_stage_standard_delivery"], "benchmark_only")
        self.assertFalse(any("[Q20] S6" in warning for warning in result.get("qa_warnings", [])))

    def test_s6_clean_path_does_not_require_offer_or_urgency_for_full_execution(self) -> None:
        strong = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "offer_or_incentive_clear": False,
            "urgency_met": False,
            "product_value_recalled": True,
            "module_fit_met": True,
            "compliance_risk": False,
            "module_type": "A",
        }
        weak = {
            **strong,
            "action_path_clear": False,
            "module_fit_met": False,
            "urgency_met": True,
        }
        scores = _s6_cta_exec({"creator_s6": weak, "benchmark_s6": strong})
        self.assertEqual(scores, {"creator_exec": 0.5, "bench_exec": 2.0})

    def test_explicit_absence_scores_zero_without_irrelevant_fields(self) -> None:
        """An explicit absence is complete; missing active-side facts remain unknown."""
        self.assertEqual(
            _s1_hook_exec({"creator_hook": {"exists": False}, "benchmark_hook": {"exists": False}}),
            {"redline": False, "creator_exec": 0.0, "bench_exec": 0.0},
        )
        self.assertEqual(
            _s2_contract_exec({"creator_s2": {"exists": False}, "benchmark_s2": {"exists": False}}),
            {"creator_exec": 0.0, "bench_exec": 0.0},
        )
        self.assertEqual(
            _s3_usage_exec({"creator_s3": {"exists": False}, "benchmark_s3": {"exists": False}}),
            {"creator_exec": 0.0, "bench_exec": 0.0},
        )
        self.assertEqual(
            _s4_effect_exec(
                {
                    "creator_s4": {"effect_evidence_state": "none"},
                    "benchmark_s4": {"effect_evidence_state": "none"},
                }
            ),
            {"creator_exec": 0.0, "bench_exec": 0.0},
        )
        self.assertEqual(
            _s5_trust_exec({"creator_s5": {"exists": False}, "benchmark_s5": {"exists": False}}),
            {"creator_exec": 0.0, "bench_exec": 0.0},
        )
        self.assertEqual(
            _s6_cta_exec({"creator_s6": {"exists": False}, "benchmark_s6": {"exists": False}}),
            {"creator_exec": 0.0, "bench_exec": 0.0},
        )

        incomplete_creator = {"exists": True}
        self.assertEqual(
            _s3_usage_exec({"creator_s3": incomplete_creator, "benchmark_s3": {"exists": False}}),
            {"creator_exec": None, "bench_exec": 0.0},
        )

    def test_s6_soft_invitation_with_offer_is_not_absent_cta(self) -> None:
        complete = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "soft_purchase_invitation_met": False,
            "offer_or_incentive_clear": True,
            "urgency_met": False,
            "product_value_recalled": True,
            "module_fit_met": True,
            "compliance_risk": False,
            "module_type": "B",
        }
        soft = {
            **complete,
            "direct_order_met": False,
            "action_path_clear": False,
            "soft_purchase_invitation_met": True,
        }
        self.assertEqual(
            _s6_cta_exec({"creator_s6": soft, "benchmark_s6": complete}),
            {"creator_exec": 1.5, "bench_exec": 2.0},
        )

    def test_s6_effect_summary_dependency_does_not_downgrade_completed_purchase_action(self) -> None:
        complete_cta = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "offer_or_incentive_clear": True,
            "urgency_met": False,
            "product_value_recalled": True,
            "module_fit_met": True,
            "compliance_risk": False,
            "module_type": "D",
            "computed_depends_on_valid_s4": False,
        }
        self.assertEqual(
            _s6_cta_exec({"creator_s6": complete_cta, "benchmark_s6": complete_cta}),
            {"creator_exec": 2.0, "bench_exec": 2.0},
        )

    def test_s3_contact_application_change_and_continuity_are_real_usage_hard_gates(self) -> None:
        complete = {
            "exists": True,
            "usage_process_visible": True,
            "result_only_without_process": False,
            "mouth_only_or_static": False,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "process_framing_met": True,
            "action_proof_met": True,
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
            "usage_context_fit": True,
            "continuity_met": True,
            "richness_met": False,
            "single_scene_continuity_met": True,
            "single_scene_variation_met": False,
            "multi_scene_logic_met": False,
            "multi_scene_transition_met": False,
            "multi_scene_role_adaptation_met": False,
            "role_design_met": False,
            "role_interaction_met": False,
            "missing_selling_points": [],
            "scene_mode": "single_scene",
            "fake_or_staged": False,
        }
        no_contact = {**complete, "action_target_contact_met": False}
        no_application_change = {**complete, "action_application_change_visible": False}
        jump_to_result = {**complete, "critical_action_continuity_met": False}
        self.assertEqual(
            _s3_usage_exec({"creator_s3": no_contact, "benchmark_s3": complete}),
            {"creator_exec": 0.0, "bench_exec": 1.0},
        )
        self.assertEqual(
            _s3_usage_exec({"creator_s3": no_application_change, "benchmark_s3": complete}),
            {"creator_exec": 0.0, "bench_exec": 1.0},
        )
        self.assertEqual(
            _s3_usage_exec({"creator_s3": jump_to_result, "benchmark_s3": complete}),
            {"creator_exec": 0.0, "bench_exec": 1.0},
        )

    def test_s3_visual_verifier_uses_stage_video_when_s4_contract_cannot_override(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[2].update(
            {
                "creator_time_range": "2.0s - 6.0s",
                "benchmark_time_range": "1.0s - 5.0s",
                "creator_s3": {"evidence_ids": ["C1"]},
                "benchmark_s3": {"evidence_ids": ["B1"]},
            }
        )
        stages[3].update(
            {
                "creator_s4": {"evidence_ids": ["C2"]},
                "benchmark_s4": {"evidence_ids": ["B2"]},
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1", "time_range": "2.0s - 6.0s"}]},
                "benchmark": {"evidence_units": [{"id": "B1", "time_range": "1.0s - 5.0s"}]},
            },
            "product_profile": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            creator = Path(tmp) / "creator.mp4"
            benchmark = Path(tmp) / "benchmark.mp4"
            creator.touch()
            benchmark.touch()
            analysis = {
                "videos": {
                    "creator": {"path": str(creator), "duration_seconds": 10.0, "frames": []},
                    "benchmark": {"path": str(benchmark), "duration_seconds": 10.0, "frames": []},
                }
            }
            with mock.patch(
                "flayr_core.llm.s4_visual_verifier.video_to_data_url",
                return_value="data:video/mp4;base64,AAAA",
            ):
                payload = build_s4_visual_verifier_payload("test", result, analysis, review_s4=False)
        self.assertIsNotNone(payload)
        content = payload["messages"][1]["content"]
        self.assertEqual(sum(item.get("type") == "video_url" for item in content), 2)
        text = "\n".join(str(item.get("text") or "") for item in content if item.get("type") == "text")
        self.assertIn("S3 原片短片", text)
        self.assertIn("两侧 s4 必须填 null", text)
        self.assertNotIn("S4 原片短片", text)

    def test_s3_s4_coherence_does_not_allow_result_to_backfill_missing_process(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[2].update(
            {
                "creator_s3": {
                    "usage_evidence_state": "complete",
                    "exists": True,
                    "usage_process_visible": True,
                    "real_usage_met": True,
                    "core_selling_point_visible": True,
                    "action_proof_met": True,
                    "action_target_contact_met": False,
                    "action_application_change_visible": False,
                    "critical_action_continuity_met": False,
                    "result_only_without_process": False,
                    "mouth_only_or_static": False,
                    "fake_or_staged": False,
                    "usage_reason": "空中比划后跳到完成态",
                },
                "benchmark_s3": {
                    "usage_evidence_state": "complete",
                    "exists": True,
                    "usage_process_visible": True,
                    "real_usage_met": True,
                    "core_selling_point_visible": True,
                    "action_proof_met": True,
                    "action_target_contact_met": True,
                    "action_application_change_visible": True,
                    "critical_action_continuity_met": True,
                    "result_only_without_process": False,
                    "mouth_only_or_static": False,
                    "fake_or_staged": False,
                    "usage_reason": "材料贴到裂缝后立即按压",
                },
            }
        )
        stages[3].update(
            {
                "creator_s4": {
                    "effect_evidence_state": "result_only",
                    "effect_visible": True,
                    "result_only_without_process": False,
                    "process_linked_effect": True,
                    "effect_proposition_matched": True,
                    "visual_difference_observed": True,
                    "module_constraints_met": True,
                    "effect_attribution_supported": True,
                    "requires_close_inspection": False,
                    "tamper_or_cut_risk": False,
                    "effect_reason": "盆底未漏水",
                },
                "benchmark_s4": {
                    "effect_evidence_state": "verified",
                    "effect_visible": True,
                    "result_only_without_process": False,
                    "process_linked_effect": True,
                    "effect_proposition_matched": True,
                    "visual_difference_observed": True,
                    "module_constraints_met": True,
                    "effect_attribution_supported": True,
                    "requires_close_inspection": False,
                    "tamper_or_cut_risk": False,
                    "effect_reason": "修补后承重",
                },
            }
        )
        result = {"stage_analysis": stages}
        reconcile_s3_s4_evidence_coherence(result)
        creator_s3 = stages[2]["creator_s3"]
        creator_s4 = stages[3]["creator_s4"]
        self.assertTrue(creator_s3["usage_process_visible"])
        self.assertTrue(creator_s3["real_usage_met"])
        self.assertTrue(creator_s3["action_proof_met"])
        self.assertNotIn("creator_has_usage_demo", stages[2])
        self.assertNotIn("benchmark_has_usage_demo", stages[2])
        self.assertFalse(creator_s4["result_only_without_process"])
        self.assertTrue(creator_s4["process_linked_effect"])
        self.assertTrue(stages[3]["benchmark_s4"]["process_linked_effect"])
        self.assertEqual(
            stages[2]["_postprocess_state"]["evidence_hard_fact_checks"]["creator_s3"]["status"],
            "state_conflict",
        )
        self.assertEqual(
            stages[3]["_postprocess_state"]["evidence_hard_fact_checks"]["creator_s4"]["status"],
            "state_conflict",
        )

    def test_s4_strong_benchmark_vs_result_only_creator_does_not_become_large(self) -> None:
        creator = {
            "effect_evidence_state": "result_only",
            "effect_visible": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_salience": "strong",
            "effect_proposition_matched": True,
            "effect_attribution_supported": True,
            "requires_close_inspection": False,
            "tamper_or_cut_risk": False,
            "result_only_without_process": True,
            "process_linked_effect": False,
            "evidence_ids": ["C4"],
        }
        benchmark = {
            **creator,
            "effect_evidence_state": "verified",
            "result_only_without_process": False,
            "process_linked_effect": True,
            "evidence_ids": ["B4"],
        }
        result = {"stage_analysis": [{}, {}, {}, {"creator_s4": creator, "benchmark_s4": benchmark}]}
        validate_s3_s4_hard_fact_consistency(result)
        trace = _derive_one(
            "S4",
            {**result["stage_analysis"][3], "model_severity": "medium"},
            {"S4": 1.0},
            [],
            facts={
                "video_understanding": {
                    "creator": {"evidence_units": [{"id": "C4", "evidence_strength": "direct"}]},
                    "benchmark": {"evidence_units": [{"id": "B4", "evidence_strength": "direct"}]},
                }
            },
        )
        self.assertEqual(trace["severity"], "medium")
        self.assertNotIn("效果证明", trace["reason"])

    def test_structural_scope_s4_visual_review_does_not_require_same_sku_contract(self) -> None:
        result = {
            "comparison_contract": {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {
                    "same_consumer_job": True,
                    "same_target_object": True,
                    "same_desired_outcome": True,
                    "same_purchase_decision": True,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {"S4": {"status": "structural"}},
            },
            "product_profile": {
                "proof_contract_source": "inferred",
                "proof_contract": {"valid": False, "mode": "trust_substituted"},
            },
        }
        self.assertEqual(_visual_verifier_skip_reason(result), "")

    def test_structural_scope_visual_rule_does_not_leak_product_contract(self) -> None:
        rule = _visual_verifier_scope_rule(
            {"proof_contract": {"anchor": "waterproof"}, "short_video_proof_plan": {"primary": "waterproof"}},
            True,
        )
        self.assertNotIn("waterproof", rule)
        self.assertIn("不要求两侧证明相同的具体功效", rule)

    def test_quantified_test_effect_can_be_complete_without_ab_comparison(self) -> None:
        quantified_test = {
            "effect_visible": True,
            "effect_salience": "strong",
            "effect_proposition_matched": True,
            "effect_attribution_supported": True,
            "process_linked_effect": True,
            "comparison_control_met": False,
            "closeup_or_focus_met": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_maximized": True,
            "requires_close_inspection": False,
            "tamper_or_cut_risk": False,
            "effect_type": "quantified_test",
        }
        self.assertEqual(
            _s4_effect_exec({"creator_s4": quantified_test, "benchmark_s4": quantified_test}),
            {"creator_exec": 2.0, "bench_exec": 2.0},
        )

    def test_s3_and_s4_complete_proof_vs_explicit_absence_are_large_gaps(self) -> None:
        complete_s3 = {
            "usage_evidence_state": "complete",
            "exists": True,
            "usage_process_visible": True,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "action_proof_met": True,
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
            "result_only_without_process": False,
            "mouth_only_or_static": False,
            "fake_or_staged": False,
            "evidence_ids": ["B3"],
        }
        missing_s3 = {**complete_s3, "exists": False, "usage_process_visible": False, "real_usage_met": False,
                      "usage_evidence_state": "none", "core_selling_point_visible": False,
                      "action_proof_met": False,
                      "action_target_contact_met": False, "action_application_change_visible": False,
                      "critical_action_continuity_met": False, "evidence_ids": ["C3"]}
        s3_facts = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C3", "evidence_strength": "direct"}]},
                "benchmark": {"evidence_units": [{"id": "B3", "evidence_strength": "direct"}]},
            }
        }
        s3_result = {"stage_analysis": [{}, {}, {
            "creator_s3": missing_s3,
            "benchmark_s3": complete_s3,
        }, {}]}
        validate_s3_s4_hard_fact_consistency(s3_result)
        s3_trace = _derive_one(
            "S3",
            {"model_severity": "medium", "creator_execution": 0.0, "benchmark_execution": 1.0,
             "creator_s3": missing_s3, "benchmark_s3": complete_s3,
             "_postprocess_state": s3_result["stage_analysis"][2]["_postprocess_state"]},
            {"S3": 1.0},
            [],
            facts=s3_facts,
        )
        self.assertEqual(s3_trace["severity"], "large")
        self.assertNotIn("E", s3_trace)
        self.assertIn("真实使用", s3_trace["reason"])
        complete_s4 = {
            "effect_evidence_state": "verified",
            "effect_visible": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_salience": "strong",
            "effect_proposition_matched": True,
            "effect_attribution_supported": True,
            "process_linked_effect": True,
            "result_only_without_process": False,
            "requires_close_inspection": False,
            "tamper_or_cut_risk": False,
            "evidence_ids": ["B4"],
        }
        missing_s4 = {**complete_s4, "effect_visible": False, "visual_difference_observed": False,
                      "effect_evidence_state": "none", "effect_salience": "none",
                      "effect_proposition_matched": False, "module_constraints_met": False,
                      "effect_attribution_supported": False, "process_linked_effect": False,
                      "evidence_ids": ["C4"]}
        s4_facts = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C4", "evidence_strength": "direct"}]},
                "benchmark": {"evidence_units": [{"id": "B4", "evidence_strength": "direct"}]},
            }
        }
        s4_result = {"stage_analysis": [{}, {}, {}, {
            "creator_s4": missing_s4,
            "benchmark_s4": complete_s4,
        }]}
        validate_s3_s4_hard_fact_consistency(s4_result)
        s4_trace = _derive_one(
            "S4",
            {"model_severity": "medium", "creator_execution": 0.0, "benchmark_execution": 2.0,
             "creator_s4": missing_s4, "benchmark_s4": complete_s4,
             "_postprocess_state": s4_result["stage_analysis"][3]["_postprocess_state"]},
            {"S4": 1.0},
            [],
            facts=s4_facts,
        )
        self.assertEqual(s4_trace["severity"], "medium")
        self.assertNotIn("E", s4_trace)
        self.assertEqual(
            next(item for item in s4_trace["constraint_evaluations"] if item["rule"] == "S4_visible_effect_floor")["status"],
            "audit_only",
        )

    def test_s3_s4_visual_verifier_applies_nested_usage_review_without_breaking_old_s4_fields(self) -> None:
        stages = [{"stage": f"S{index}", "severity": "small"} for index in range(1, 7)]
        for role in ("creator", "benchmark"):
            stages[2][f"{role}_s3"] = {
                "usage_process_visible": True,
                "real_usage_met": True,
                "core_selling_point_visible": True,
                "action_proof_met": True,
                "action_target_contact_met": True,
                "action_application_change_visible": True,
                "critical_action_continuity_met": True,
                "usage_reason": "原始判断",
            }
            stages[3][f"{role}_s4"] = {
                "effect_visible": True,
                "effect_proposition_matched": True,
                "effect_salience": "strong",
                "comparison_control_met": True,
                "closeup_or_focus_met": True,
                "visual_difference_observed": True,
                "module_constraints_met": True,
                "effect_maximized": True,
                "requires_close_inspection": False,
                "effect_attribution_supported": True,
                "result_only_without_process": False,
                "process_linked_effect": True,
                "tamper_or_cut_risk": False,
                "effect_reason": "原始判断",
            }
        result = {"stage_analysis": stages, "improvements": []}
        applied = apply_s4_visual_verifier_result(
            result,
            {
                "creator": {
                    "s3": {
                        "evidence_sufficient": True,
                        "action_target_contact_met": False,
                        "action_application_change_visible": False,
                        "critical_action_continuity_met": False,
                        "reason": "只看到准备和完成态。",
                    },
                    "s4": {
                        "evidence_sufficient": True,
                        "effect_proposition_matched": True,
                        "visual_difference_observed": True,
                        "module_constraints_met": True,
                        "effect_salience": "clear",
                        "requires_close_inspection": False,
                        "effect_maximized": False,
                        "reason": "只见结果，未见关键作用过程。",
                    },
                },
                "benchmark": {
                    "s3": {
                        "evidence_sufficient": True,
                        "action_target_contact_met": True,
                        "action_application_change_visible": True,
                        "critical_action_continuity_met": True,
                        "reason": "关键动作和目标状态均可见。",
                    },
                    "s4": {
                        "evidence_sufficient": True,
                        "effect_proposition_matched": True,
                        "visual_difference_observed": True,
                        "module_constraints_met": True,
                        "effect_salience": "strong",
                        "requires_close_inspection": False,
                        "effect_maximized": True,
                        "reason": "效果与过程均清楚。",
                    },
                },
            },
            {},
        )
        self.assertTrue(applied)
        self.assertTrue(stages[2]["creator_s3"]["real_usage_met"])
        self.assertFalse(stages[3]["creator_s4"]["result_only_without_process"])
        self.assertTrue(stages[3]["creator_s4"]["process_linked_effect"])
        self.assertTrue(stages[2]["benchmark_s3"]["real_usage_met"])
        self.assertTrue(stages[3]["benchmark_s4"]["effect_visible"])

    def test_visual_verifier_does_not_overwrite_positive_facts_when_static_coverage_is_insufficient(self) -> None:
        stages = [{"stage": f"S{index}", "severity": "small"} for index in range(1, 7)]
        stages[2]["creator_s3"] = {
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
        }
        stages[3]["creator_s4"] = {
            "effect_visible": True,
            "effect_proposition_matched": True,
            "effect_salience": "clear",
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_maximized": False,
        }
        stages[3]["benchmark_s4"] = dict(stages[3]["creator_s4"])
        result = {"stage_analysis": stages, "improvements": []}

        apply_s4_visual_verifier_result(
            result,
            {
                "creator": {
                    "s3": {
                        "evidence_sufficient": False,
                        "action_target_contact_met": False,
                        "action_application_change_visible": False,
                        "critical_action_continuity_met": False,
                    },
                    "s4": {
                        "evidence_sufficient": False,
                        "visual_difference_observed": False,
                        "effect_proposition_matched": False,
                        "effect_salience": "none",
                    },
                },
                "benchmark": {"s4": {"evidence_sufficient": True, "visual_difference_observed": True}},
            },
            {},
        )

        self.assertTrue(stages[2]["creator_s3"]["action_target_contact_met"])
        self.assertTrue(stages[2]["creator_s3"]["action_application_change_visible"])
        self.assertTrue(stages[3]["creator_s4"]["effect_visible"])
        self.assertTrue(stages[3]["creator_s4"]["visual_difference_observed"])
        self.assertEqual(stages[2]["creator_s3"]["visual_verifier_coverage"], "insufficient_for_negative_override")
        self.assertEqual(stages[3]["creator_s4"]["visual_verifier_coverage"], "insufficient_for_negative_override")

    def test_pending_s3_s6_observations_do_not_change_execution(self) -> None:
        base_s3 = {
            "exists": True,
            "usage_process_visible": True,
            "result_only_without_process": False,
            "mouth_only_or_static": False,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "process_framing_met": True,
            "action_proof_met": True,
            "usage_context_fit": True,
            "continuity_met": True,
            "richness_met": True,
            "single_scene_continuity_met": True,
            "single_scene_variation_met": True,
            "multi_scene_logic_met": False,
            "multi_scene_transition_met": False,
            "multi_scene_role_adaptation_met": False,
            "role_design_met": True,
            "role_interaction_met": True,
            "missing_selling_points": [],
            "scene_mode": "multi_person",
            "fake_or_staged": False,
        }
        with_observations = {
            **base_s3,
            "distinct_personas_met": True,
            "steps_clear_met": True,
            "pov_immersive_met": True,
        }
        without_observations = {
            **base_s3,
            "distinct_personas_met": False,
            "steps_clear_met": False,
            "pov_immersive_met": False,
            "process_framing_met": False,
        }
        with_trace = _derive_one("S3", {"creator_s3": with_observations, "benchmark_s3": with_observations}, {"S3": 1.0}, [])
        without_trace = _derive_one("S3", {"creator_s3": without_observations, "benchmark_s3": without_observations}, {"S3": 1.0}, [])
        self.assertEqual(with_trace["derived_creator_execution"], without_trace["derived_creator_execution"])
        self.assertEqual(with_trace["severity"], without_trace["severity"])
        self.assertIn("s3_presentation_observations", with_trace)

        base_s6 = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "offer_or_incentive_clear": True,
            "urgency_met": True,
            "product_value_recalled": True,
            "module_fit_met": True,
            "depends_on_valid_s4": True,
            "compliance_risk": False,
            "module_type": "B",
        }
        strong_observation = {**base_s6, "urgency_evidence_met": True}
        weak_observation = {**base_s6, "urgency_evidence_met": False}
        self.assertEqual(
            _s6_cta_exec({"creator_s6": weak_observation, "benchmark_s6": strong_observation}),
            {"creator_exec": 2.0, "bench_exec": 2.0},
        )

    def test_s5_source_reconciliation_requires_locked_source_evidence(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[4] = {
            "stage": "S5 信任放大",
            "creator_s5": {
                "exists": True,
                "trust_basis": "offer_or_spec",
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "evidence_ids": ["C5"],
            },
            "benchmark_s5": {
                "exists": True,
                "trust_basis": "authority",
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "trust_claim_specific": True,
                "product_relevance_met": True,
                "evidence_ids": ["B5"],
                "trust_source_evidence_ids": [],
            },
            "creator_summary": "达人提到套装数量。",
            "benchmark_summary": "标杆展示可核验认证。",
            "gap": "标杆背书更强。",
        }
        result = {
            "stage_analysis": stages,
            "improvements": [
                {"target_stage": "S5 信任放大", "title": "补强信任"},
                {"target_stage": "S1 Hook", "title": "保留项"},
            ],
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C5", "trust_source_signals": [], "trust_source_reference": ""}]},
                "benchmark": {"evidence_units": [{"id": "B5", "trust_source_signals": ["authority"], "trust_source_reference": "KKM 页面"}]},
            },
        }
        reconcile_s5_trust_sources(result, True)
        self.assertTrue(stages[4]["creator_s5"]["exists"])
        self.assertEqual(stages[4]["creator_s5"]["_s5_source_status"], "unknown")
        self.assertTrue(stages[4]["benchmark_s5"]["exists"])
        self.assertEqual(stages[4]["benchmark_s5"]["trust_source_evidence_ids"], ["B5"])
        self.assertEqual(stages[4]["gap"], "标杆背书更强。")
        self.assertEqual([item["target_stage"] for item in result["improvements"]], ["S5 信任放大", "S1 Hook"])

        stages[4]["benchmark_s5"]["trust_source_evidence_ids"] = []
        result["video_understanding"]["benchmark"]["evidence_units"][0]["trust_source_signals"] = []
        reconcile_s5_trust_sources(result, True)
        self.assertTrue(stages[4]["benchmark_s5"]["exists"])
        self.assertEqual(stages[4]["benchmark_s5"]["_s5_source_status"], "unknown")
        self.assertEqual([item["target_stage"] for item in result["improvements"]], ["S5 信任放大", "S1 Hook"])
        self.assertEqual(stages[4]["gap"], "标杆背书更强。")

    def test_s5_source_reconciliation_rejects_vague_or_irrelevant_testimonial(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[4] = {
            "stage": "S5 信任放大",
            "creator_s5": {
                "exists": False,
                "trust_basis": "none",
                "trust_evidence_type": "none",
                "independent_trust_purpose": False,
                "duplicates_other_stage": False,
            },
            "benchmark_s5": {
                "exists": True,
                "trust_basis": "independent_user",
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "trust_claim_specific": False,
                "product_relevance_met": True,
                "evidence_ids": ["B3"],
            },
            "creator_summary": "达人没有独立背书。",
            "benchmark_summary": "评论询问产品是什么。",
            "gap": "标杆有评论背书。",
        }
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "creator": {"evidence_units": []},
                "benchmark": {
                    "evidence_units": [
                        {
                            "id": "B3",
                            "trust_source_signals": ["independent_user"],
                            "trust_source_reference": "评论：这是牙膏吗？",
                        }
                    ]
                },
            },
        }

        reconcile_s5_trust_sources(result, True)

        self.assertTrue(stages[4]["benchmark_s5"]["exists"])
        self.assertEqual(stages[4]["benchmark_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stages[4]["gap"], "标杆有评论背书。")

    def test_subtitle_driven_cta_is_not_overwritten_by_no_cta_placeholder(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "benchmark_evidence_ids": ["B_NO_CTA"],
                "benchmark_quote": "",
                "benchmark_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "evidence_ids": ["B8"],
                },
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "benchmark": {
                    "evidence_units": [
                        {"id": "B8", "information": "提示点击下方购物车下单", "subtitle_fact": "กดตะกร้าด้านล่าง"},
                        {"id": "B_NO_CTA", "information": "结尾未识别到明确购买指令"},
                    ]
                },
                "creator": {"evidence_units": []},
            },
        }
        reconcile_unsupported_cta(result)
        self.assertEqual(stages[5]["benchmark_evidence_ids"], ["B8"])
        ids = [unit["id"] for unit in result["video_understanding"]["benchmark"]["evidence_units"]]
        self.assertNotIn("B_NO_CTA", ids)

    def test_effect_summary_cannot_become_soft_cta_without_invitation_and_offer(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "benchmark_s6": {
                    "exists": True,
                    "direct_order_met": False,
                    "action_path_clear": False,
                    "soft_purchase_invitation_met": True,
                    "offer_or_incentive_clear": False,
                    "module_fit_met": True,
                    "ending_position_met": True,
                    "cta_reason": "结尾总结妆效，暗示值得购买。",
                    "evidence_ids": ["B8"],
                },
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "benchmark": {
                    "evidence_units": [
                        {"id": "B8", "information": "展示上妆后的柔雾效果", "voiceover_zh": "妆效很轻薄。"},
                    ]
                },
                "creator": {"evidence_units": []},
            },
        }

        reconcile_unsupported_cta(result)

        flag = stages[5]["benchmark_s6"]
        self.assertFalse(flag["exists"])
        self.assertFalse(flag["soft_purchase_invitation_met"])
        self.assertEqual(flag["evidence_ids"], [])
        self.assertEqual(stages[5]["benchmark_evidence_ids"], ["B_NO_CTA"])
        self.assertIn("按无 CTA 处理", flag["cta_reason"])
        self.assertIn("双方结尾均未出现", stages[5]["gap"])

    def test_stage_flag_evidence_uses_same_stage_validated_references(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[1].update(
            {
                "benchmark_evidence_ids": ["B2"],
                "benchmark_s2": {"exists": True, "evidence_ids": []},
            }
        )
        result = {"stage_analysis": stages}

        align_stage_flag_evidence(result)

        self.assertEqual(stages[1]["benchmark_s2"]["evidence_ids"], ["B2"])

    def test_stage_flag_evidence_restores_primary_reference_before_placeholder(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {
                            "id": "C3",
                            "time_range": "10.0s - 20.0s",
                            "information": "半脸前后对比展示哑光效果",
                            "visual_fact": "同侧脸颊前后对比",
                            "voiceover": "",
                            "voiceover_zh": "",
                        }
                    ]
                },
                "benchmark": {"evidence_units": []},
            },
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 4)] + [
                {
                    "stage": "S4 效果呈现",
                    "creator_time_range": "10.0s - 20.0s",
                    "creator_evidence_ids": ["C_NO_STAGE_4"],
                    "creator_summary": "（LLM 未填写 creator_summary，需人工补充）",
                    "creator_s4": {"evidence_ids": ["C3"]},
                }
            ] + [{"stage": f"S{index}"} for index in range(5, 7)],
        }

        align_stage_flag_evidence(result)

        stage = result["stage_analysis"][3]
        self.assertEqual(stage["creator_evidence_ids"], ["C3"])
        self.assertEqual(stage["creator_summary"], "半脸前后对比展示哑光效果")
        self.assertEqual(stage["creator_support_status"], "visual_only")

    def test_missing_stage_range_uses_locked_neighbor_window_for_placeholder(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {
                            "id": "C5",
                            "time_range": "42.0s - 48.0s",
                            "voiceover": "",
                            "functions": ["S3_usage", "S5_trust"],
                        },
                        {
                            "id": "C6",
                            "time_range": "48.0s - 64.0s",
                            "voiceover": "",
                            "functions": ["S5_trust"],
                        },
                    ]
                },
                "benchmark": {
                    "evidence_units": [
                        {"id": f"B{index}", "time_range": f"{index}s - {index + 1}s"}
                        for index in range(1, 7)
                    ]
                },
            },
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "creator_time_range": creator_range,
                    "creator_evidence_ids": creator_ids,
                    "benchmark_time_range": f"{index}s - {index + 1}s",
                    "benchmark_evidence_ids": [f"B{index}"],
                }
                for index, (creator_range, creator_ids) in enumerate(
                    [
                        ("0s - 1s", ["C5"]),
                        ("1s - 2s", ["C5"]),
                        ("42s - 48s", ["C5"]),
                        ("", []),
                        ("48s - 64s", ["C6"]),
                        ("64s - 84s", ["C6"]),
                    ],
                    start=1,
                )
            ],
        }

        fill_missing_evidence_references(result)

        stage = result["stage_analysis"][3]
        self.assertEqual(stage["creator_time_range"], "48.0s - 48.5s")
        self.assertEqual(stage["creator_evidence_ids"], ["C_NO_STAGE_4"])
        placeholder = next(
            unit
            for unit in result["video_understanding"]["creator"]["evidence_units"]
            if unit["id"] == "C_NO_STAGE_4"
        )
        self.assertEqual(placeholder["time_range"], "48.0s - 48.5s")
        validate_evidence_alignment(result)

    def test_repair_preserves_original_improvements_when_model_drops_them(self) -> None:
        original = {"improvements": [{"title": "保留的合法建议"}], "stage_analysis": [{"stage": "旧"}]}
        repaired = {"improvements": [], "stage_analysis": [{"stage": "修复后"}]}

        merged = pipeline.preserve_valid_repair_sections(original, repaired)

        self.assertEqual(merged["improvements"], original["improvements"])
        self.assertEqual(merged["stage_analysis"], repaired["stage_analysis"])

    def test_large_stage_without_improvement_is_reported(self) -> None:
        result = {
            "one_line_verdict": "结论",
            "holistic_assessment": {},
            "product_visibility": {"first_appearance_sec": 0, "ratio": 0.5},
            "loop_closure": {"note": "已完成"},
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "severity": "large" if index == 6 else "small",
                    "gap_summary": ["差距"],
                    "module_fit_reason": "已判断",
                }
                for index in range(1, 7)
            ],
            "improvements": [{"target_stage": "S4"}],
        }
        validate_analysis_dimensions(result)
        self.assertTrue(any("[Q13] S6" in warning for warning in result.get("qa_warnings", [])))

    def test_improvement_reconciliation_only_targets_uncovered_large_stage(self) -> None:
        result = {
            "product_profile": {"hook_proposition": "本品命题"},
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C6", "time_range": "20s - 25s"}]},
                "benchmark": {"evidence_units": [{"id": "B6", "time_range": "18s - 22s"}]},
            },
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "severity": "large" if index == 6 else "small",
                    "model_gap_magnitude": "large" if index == 6 else "small",
                    "creator_evidence_ids": ["C6"] if index == 6 else [],
                    "benchmark_evidence_ids": ["B6"] if index == 6 else [],
                    "creator_time_range": "20s - 25s" if index == 6 else "",
                    "benchmark_time_range": "18s - 22s" if index == 6 else "",
                    "gap_type": "execution" if index == 6 else "",
                    "benchmark_summary": "锁定的标杆证据" if index == 6 else "",
                    "evidence": ["锁定的阶段证据"] if index == 6 else [],
                }
                for index in range(1, 7)
            ],
            "improvements": [{"target_stage": "S4", "title": "原建议", "priority": 1}],
        }
        missing = pipeline.uncovered_large_stage_codes(result)
        self.assertEqual(missing, ["S6"])

        payload = build_improvement_reconciliation_payload("test", result, missing, {"product": {"name": "测试品"}})
        payload_text = payload["messages"][1]["content"][0]["text"]
        self.assertIn('"missing_large_stages": [\n    "S6"', payload_text)
        self.assertIn('"id": "C6"', payload_text)

        merged = pipeline.merge_reconciled_improvements(
            result,
            [{
                "target_stage": "S6",
                "title": "补购买路径",
                "priority": 99,
                "gap_type": "forged",
                "creator_time_range": "0s - 999s",
                "benchmark_evidence_ids": ["FAKE"],
            }],
            missing,
        )
        self.assertEqual(pipeline.uncovered_large_stage_codes(merged), [])
        self.assertEqual(merged["improvements"][0]["target_stage"], "S6")
        self.assertEqual(merged["improvements"][0]["gap_type"], "execution")
        self.assertEqual(merged["improvements"][0]["creator_time_range"], "20s - 25s")
        self.assertEqual(merged["improvements"][0]["benchmark_evidence_ids"], ["B6"])
        self.assertEqual(merged["improvements"][0]["priority"], 1)

    def test_finalized_analysis_writes_canonical_trace_back_to_main_analysis(self) -> None:
        normalized = {
            "executive_summary": "结论",
            "one_line_summary": "结论",
            "one_line_verdict": "结论",
            "holistic_assessment": {},
            "key_conclusions": [],
            "product_visibility": {},
            "loop_closure": {"source": "proposition_trace"},
            "video_understanding": {},
            "stage_analysis": [],
            "improvements": [],
            "product_profile": {"proof_contract": {"valid": True}},
            "proposition_trace": {"version": "1.0"},
            "absolute_quality": {"S1": {"creator": {"status": "weak"}}},
            "absolute_execution_shadow": {"status": "completed", "roles": {}},
            "computed_loop_closure": {"audit_status": "closed"},
            "qa_warnings": ["warning"],
        }
        analysis = {}
        pipeline.apply_finalized_analysis_result(analysis, normalized, Path("result.json"))
        self.assertEqual(analysis["proposition_trace"], {"version": "1.0"})
        self.assertEqual(analysis["computed_loop_closure"]["audit_status"], "closed")
        self.assertEqual(analysis["product_profile"]["proof_contract"]["valid"], True)
        self.assertEqual(analysis["absolute_quality"]["S1"]["creator"]["status"], "weak")
        self.assertEqual(analysis["absolute_execution_shadow"]["status"], "completed")

    def test_stage_flag_normalization_preserves_proposition_ids(self) -> None:
        normalized = normalize_s3_flags({"proposition_ids": ["selling.1", "selling.1"], "evidence_ids": ["C1"]})
        self.assertEqual(normalized["proposition_ids"], ["selling.1"])

    def test_s3_normalization_defaults_only_non_applicable_mode_flags(self) -> None:
        normalized = normalize_s3_flags(
            {
                "scene_mode": "single_scene",
                "presentation_overlays": [],
                "multi_scene_logic_met": True,
                "steps_clear_met": True,
            }
        )
        self.assertIsNone(normalized["single_scene_continuity_met"])
        self.assertIsNone(normalized["single_scene_variation_met"])
        self.assertFalse(normalized["multi_scene_logic_met"])
        self.assertFalse(normalized["role_design_met"])
        self.assertFalse(normalized["steps_clear_met"])
        self.assertFalse(normalized["pov_immersive_met"])

    def test_multimodal_evidence_is_pruned_after_stage_specific_repair(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {"id": "C3", "functions": ["S4_effect"]},
                        {"id": "C4", "functions": ["S5_trust"]},
                    ]
                }
            },
            "stage_analysis": [
                {}, {}, {}, {},
                {
                    "creator_evidence_ids": ["C4"],
                    "creator_multimodal": {
                        "channel_evidence_ids": {
                            "visual": ["C4"],
                            "speech": ["C3", "C4"],
                        }
                    },
                },
            ],
        }
        prune_multimodal_evidence_to_stage(result)
        refs = result["stage_analysis"][4]["creator_multimodal"]["channel_evidence_ids"]
        self.assertEqual(refs["visual"], ["C4"])
        self.assertEqual(refs["speech"], ["C4"])

    def test_proposition_trace_links_s3_s4_and_does_not_change_severity(self) -> None:
        foundation = self._proposition_foundation()
        stages = [
            {"stage": "S1 Hook", "severity": "small", "creator_hook": {"anchors_proposition": True, "proposition_ids": ["hook.1"]}, "benchmark_hook": {"anchors_proposition": True, "proposition_ids": ["hook.1"]}},
            {"stage": "S2 产品引出", "severity": "small", "creator_s2": {"handoff_met": True, "product_identity_clear": True, "product_role_clear": True, "proposition_ids": ["hook.1"]}, "benchmark_s2": {"handoff_met": True, "product_identity_clear": True, "product_role_clear": True, "proposition_ids": ["hook.1"]}},
            {"stage": "S3 使用过程", "severity": "medium", "creator_s3": {"usage_process_visible": True, "core_selling_point_visible": True, "process_framing_met": True, "proposition_ids": ["selling.1"]}, "benchmark_s3": {"usage_process_visible": True, "core_selling_point_visible": True, "process_framing_met": True, "proposition_ids": ["selling.1"]}},
            {"stage": "S4 效果呈现", "severity": "large", "creator_s4": {"effect_visible": True, "effect_salience": "strong", "effect_proposition_matched": True, "effect_attribution_supported": True, "process_linked_effect": True, "proposition_ids": ["proof.1"]}, "benchmark_s4": {"effect_visible": True, "effect_salience": "strong", "effect_proposition_matched": True, "effect_attribution_supported": True, "process_linked_effect": True, "proposition_ids": ["proof.1"]}},
            {"stage": "S5 信任放大", "severity": "small", "creator_s5": {"exists": True, "product_relevance_met": True, "proposition_ids": ["selling.1"]}, "benchmark_s5": {"exists": True, "product_relevance_met": True, "proposition_ids": ["selling.1"]}},
            {"stage": "S6 CTA", "severity": "medium", "creator_s6": {"exists": True, "direct_order_met": True, "ending_position_met": True, "product_value_recalled": True, "proposition_ids": ["selling.1"]}, "benchmark_s6": {"exists": True, "direct_order_met": True, "ending_position_met": True, "product_value_recalled": True, "proposition_ids": ["selling.1"]}},
        ]
        result = {
            "category_profile": foundation["category_profile"],
            "product_profile": foundation["product_profile"],
            "stage_analysis": stages,
            "s3_s4_relationship": {
                "creator_relationship": "process_creates_effect",
                "benchmark_relationship": "process_creates_effect",
            },
        }
        before = [stage["severity"] for stage in stages]
        materialize_cross_stage_inputs(result, {"brand_proposition": {}})
        shadow = {
            "status": "completed",
            "errors": [],
            "roles": {
                "creator": {
                    "stages": {
                        "S1": {"score": 0.5, "status": "weak", "reason": "shadow", "evidence_ids": ["C1"]},
                    },
                },
            },
        }
        materialize_quality_audits(result, {"absolute_execution_shadow": shadow})

        self.assertEqual([stage["severity"] for stage in stages], before)
        self.assertEqual(stages[0]["creator_absolute_execution_shadow"]["score"], 0.5)
        self.assertEqual(result["absolute_execution_shadow"]["status"], "completed")
        creator_edges = result["proposition_trace"]["roles"]["creator"]["edges"]
        self.assertEqual(creator_edges["S3_to_S4"]["status"], "same_claim_proven")
        self.assertEqual(creator_edges["S6_recall"]["status"], "value_recalled")
        self.assertEqual(result["computed_loop_closure"]["audit_status"], "closed")
        self.assertEqual(result["loop_closure"]["source"], "proposition_trace")
        validate_analysis_dimensions(result)
        self.assertFalse(any("缺少第二步槽位间闭环校验" in warning for warning in result.get("qa_warnings", [])))
        self.assertEqual(stages[5]["computed_stage_standard_delivery"], "both")

    def test_stage_time_coherence_allows_functional_overlap(self) -> None:
        ranges = ["0s - 4s", "3s - 7s", "5s - 12s", "6s - 12s", "1s - 2s", "11s - 13s"]
        result = {
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "creator_time_range": value,
                    "benchmark_time_range": value,
                    **(
                        {
                            "creator_s2": {"merged_with_s3": True},
                            "benchmark_s2": {"merged_with_s3": True},
                        }
                        if index == 2
                        else {}
                    ),
                }
                for index, value in enumerate(ranges, start=1)
            ]
        }
        validate_stage_time_coherence(result)
        self.assertFalse(result.get("qa_warnings"))

        result["stage_analysis"][1]["creator_s2"]["merged_with_s3"] = False
        validate_stage_time_coherence(result)
        warnings = result.get("qa_warnings") or []
        self.assertEqual(len(warnings), 1)
        self.assertIn("creator S2/S3", warnings[0])

    def test_stage_time_coherence_does_not_require_ranges_for_blocked_evidence(self) -> None:
        result = {
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "creator_time_range": "0.0s - 0.0s",
                    "benchmark_time_range": "0.0s - 0.0s",
                }
                for index in range(1, 7)
            ]
        }
        with mock.patch(
            "flayr_core.postprocess.validate._stage_evidence_unresolved",
            return_value=True,
        ):
            validate_stage_time_coherence(result)

    def test_stage_time_coherence_does_not_require_ranges_for_explicit_absence(self) -> None:
        result = {
            "video_understanding": {
                role: {
                    "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
                    "stage_evidence_checks": [
                        {
                            "stage": "S1",
                            "status": "absent",
                            "coverage": "complete",
                            "evidence_ids": [],
                            "observed_signals": [],
                            "missing_signals": ["stop_trigger", "cold_audience_relevance"],
                            "signal_bindings": {},
                        }
                    ],
                    "evidence_units": [],
                    "stage1_recovery": {"source": "pipeline", "status": "not_needed"},
                }
                for role in ("creator", "benchmark")
            },
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_time_range": "",
                    "benchmark_time_range": "",
                }
            ],
        }
        validate_stage_time_coherence(result)

    def test_stage_links_are_validated_after_deterministic_postprocess(self) -> None:
        source = inspect.getsource(pipeline.finalize_canonical_analysis_result)
        self.assertGreater(
            source.index("stage_evidence_link_issues(normalized)"),
            source.index("apply_postprocess_chain(normalized"),
        )

    def test_json_codec_stays_compatible_with_parse_facade(self) -> None:
        from flayr_core.llm.parse import parse_json_text as parse_facade

        malformed = '{"value":"他说 "好"",}'
        self.assertEqual(parse_json_text(malformed), {"value": '他说 "好"'})
        self.assertEqual(parse_facade(malformed), {"value": '他说 "好"'})

    def test_product_profile_stays_compatible_with_parse_facade(self) -> None:
        from flayr_core.llm.parse import normalize_product_profile as parse_facade
        from flayr_core.llm.product_profile import normalize_product_profile as direct

        self.assertIs(parse_facade, direct)

    def test_command_timeout_is_returned_as_normal_failure(self) -> None:
        completed = utils.run_command(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            timeout_seconds=1,
        )
        self.assertEqual(completed.returncode, 124)
        self.assertIn("timed out after 1s", completed.stderr)

    def test_run_command_can_send_stdin_without_putting_secret_in_command(self) -> None:
        completed = utils.run_command(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            stdin_text="secret-header",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "secret-header")

    def test_time_parsing_rejects_repairable_or_nonfinite_ranges(self) -> None:
        self.assertEqual(parse_time_range_seconds("3s", 20), (3.0, 3.0))
        self.assertEqual(parse_time_range_seconds("最后 5 秒", 20), (15.0, 20.0))
        self.assertIsNone(parse_time_range_seconds("", 20))
        self.assertIsNone(parse_time_range_seconds("8s - 3s", 20))
        self.assertIsNone(parse_time_range_seconds("0s - 25s", 20))
        self.assertIsNone(parse_time_range_seconds("NaN", 20))
        self.assertIsNone(parse_time_range_seconds("最后 5 秒", None))
        self.assertEqual(parse_time_range_seconds([0.0, 3.0], 20), (0.0, 3.0))
        self.assertIsNone(parse_time_range_seconds("标杆 0s - 3s / 达人 0s - 4s", 20))
        self.assertIsNone(parse_time_range_seconds("备注 3s", 20))
        self.assertEqual(normalize_time_range_value([0.0, 3.0]), "0.0s - 3.0s")
        self.assertIsNone(parse_timestamp_seconds("-1s"))
        self.assertIsNone(parse_timestamp_seconds("inf"))
        self.assertIsNone(parse_timestamp_seconds("0s - 3s"))
        self.assertIsNone(parse_timestamp_seconds("时间点 3s"))
        self.assertIsNone(parse_timestamp_seconds(24 * 60 * 60 + 1))

    def test_time_consumers_do_not_repair_invalid_values(self) -> None:
        self.assertEqual(normalize_time_range_value("0s - 3s"), "0.0s - 3.0s")
        self.assertEqual(normalize_time_range_value("8s - 3s"), "")
        self.assertEqual(normalize_time_range_value("标杆 0s - 3s / 达人 0s - 4s"), "")
        self.assertIsNone(parse_srt_timestamp("not-a-timestamp"))
        self.assertIsNone(parse_srt_timestamp("00:61:00,000"))
        self.assertEqual(parse_srt_time_range("00:00:01,000 --> 00:00:03,000"), (1.0, 3.0))
        self.assertEqual(parse_srt_time_range("00:00:03,000 --> 00:00:01,000"), (None, None))

    def test_srt_reader_skips_malformed_and_reversed_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript.srt"
            path.write_text(
                "1\n00:00:03,000 --> 00:00:01,000\nreversed\n\n"
                "2\n00:00:01,000 --> broken\nmalformed\n\n"
                "3\n00:00:01,000 --> 00:00:02,000\nvalid\n",
                encoding="utf-8",
            )
            segments = read_srt_segments({"transcript_segments_path": str(path)})
        self.assertEqual(segments, [{"start": 1.0, "end": 2.0, "text": "valid"}])

    def test_frame_manifest_never_falls_back_to_output_index(self) -> None:
        frames = [Path("frame_0001.jpg"), Path("frame_0002.jpg")]
        self.assertEqual([item["timestamp_seconds"] for item in build_frame_manifest(frames)], [None, None])
        manifest = build_frame_manifest(frames, ["1.25s", "NaN"])
        self.assertEqual([item["timestamp_seconds"] for item in manifest], [1.25, None])
        self.assertEqual(video._showinfo_timestamps("[Parsed_showinfo] pts_time:1.25\n", 1), [1.25])
        self.assertEqual(video._showinfo_timestamps("[Parsed_showinfo] pts_time:1.25\n", 2), [None, None])

    def test_invalid_duration_does_not_become_subtitle_sampling_fallback(self) -> None:
        frames = [{"path": "frame.jpg", "timestamp_seconds": 0.0}]
        self.assertEqual(subtitle_track.sample_frames_by_interval(frames, "NaN", 2.5), [])
        self.assertEqual(subtitle_track.sample_frames_by_interval(frames, None, 2.5), frames)

    def test_invalid_time_range_does_not_select_a_frame(self) -> None:
        info = {
            "frames": [
                {"path": "frame.jpg", "timestamp_seconds": 0.0},
                {"path": "frame2.jpg", "timestamp_seconds": 5.0},
            ],
            "duration_seconds": 10.0,
        }
        self.assertEqual(select_frames_for_time_range(info, "9s - 2s"), [])

    def test_atomic_write_preserves_existing_artifact_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text("old", encoding="utf-8")
            with mock.patch.object(utils.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    utils.write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertFalse(list(path.parent.glob(".analysis.json.*.tmp")))

    def test_frame_extraction_clears_stale_frames_before_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            focus_dir = root / "focus"
            frames_dir.mkdir()
            focus_dir.mkdir()
            (frames_dir / "frame_9999.jpg").write_bytes(b"old")
            (focus_dir / "hook_9999.jpg").write_bytes(b"old")
            result = {"errors": [], "duration_seconds": 0.0}
            with mock.patch.object(video, "run_command", return_value=SimpleNamespace(returncode=1, stderr="ffmpeg failed")):
                video.extract_frames(root / "source.mp4", frames_dir, focus_dir, result)
            self.assertFalse((frames_dir / "frame_9999.jpg").exists())
            self.assertFalse((focus_dir / "hook_9999.jpg").exists())

    def test_analysis_contract_rejects_invalid_raw_envelope(self) -> None:
        with self.assertRaises(AnalysisContractError):
            validate_raw_analysis_envelope({"stage_analysis": [], "improvements": []})

        complete_stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        with self.assertRaisesRegex(AnalysisContractError, "1 to 5 improvements"):
            validate_raw_analysis_envelope(
                {"stage_analysis": complete_stages, "improvements": []}
            )

        accepted = validate_raw_analysis_envelope(
            {"stage_analysis": complete_stages, "improvements": [{"title": "one"}]}
        )
        self.assertEqual(len(accepted["stage_analysis"]), 6)

    def test_segmented_result_allows_no_grounded_improvements_at_publish_boundary(self) -> None:
        stages = [{"stage": f"S{index} stage"} for index in range(1, 7)]
        result = {
            "one_line_summary": "summary",
            "executive_summary": "summary",
            "holistic_assessment": {},
            "product_visibility": {},
            "loop_closure": {},
            "video_understanding": {},
            "stage_analysis": stages,
            "improvements": [],
            "stage2_pipeline_version": "segmented_stage_v1",
        }

        self.assertIs(validate_raw_analysis_envelope(result), result)
        validate_normalized_analysis_contract(result)

    def test_stage1_recovery_contract_uses_role_specific_evidence_prefix(self) -> None:
        analysis = {"videos": {"benchmark": {}, "creator": {}}}
        for role, expected, forbidden in (
            ("benchmark", "B9", "C9"),
            ("creator", "C9", "B9"),
        ):
            payload = build_video_fact_recovery_payload(
                "qwen3.6-plus",
                role,
                analysis,
                [],
                {"evidence_units": []},
                ["S3"],
            )
            text = payload["messages"][1]["content"][0]["text"]
            self.assertIn(expected, text)
            self.assertNotIn(forbidden, text)

    def test_analysis_contract_rejects_normalized_stage_order_drift(self) -> None:
        result = {
            "one_line_summary": "summary",
            "executive_summary": "summary",
            "holistic_assessment": {},
            "product_visibility": {},
            "loop_closure": {},
            "video_understanding": {},
            "stage_analysis": [{"stage": f"S{index} stage"} for index in range(1, 7)],
            "improvements": [{"title": "one"}],
        }
        validate_normalized_analysis_contract(result)
        result["stage_analysis"][2]["stage"] = "S4 stage"
        with self.assertRaises(AnalysisContractError):
            validate_normalized_analysis_contract(result)

    def test_quality_contract_rejects_missing_stage_narrative(self) -> None:
        incomplete = {
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "benchmark_summary": "标杆表现",
                    "creator_summary": "达人表现",
                    "gap": "（LLM 未填写 gap，需人工补充）",
                }
            ]
        }
        with self.assertRaises(SystemExit):
            validate_required_stage_narratives(incomplete)

        complete = json.loads(json.dumps(incomplete, ensure_ascii=False))
        complete["stage_analysis"][0]["gap"] = "达人未承接标杆的核心痛点。"
        validate_required_stage_narratives(complete)

    def test_stage_catalog_is_shared_by_parse_and_artifact_fallback(self) -> None:
        from flayr_core.llm.parse import STAGES

        ranges = fallback_artifact_ranges(20.0)
        self.assertEqual(STAGES, stage_tuples())
        self.assertEqual([item[0] for item in ranges], [stage.name for stage in DEFAULT_STAGES])
        self.assertEqual(ranges[-1][2:], (15.0, 20.0))

    def test_certification_policy_reaches_all_analysis_prompts(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        comparison_content = comparison["messages"][1]["content"]
        comparison_text = comparison_content[0]["text"] if isinstance(comparison_content, list) else comparison_content
        repair_text = build_llm_repair_payload("test", "{}", "error", "input")["messages"][0]["content"]
        review = build_stage_review_payload(
            "test",
            {"videos": {}},
            {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
            {"stage_analysis": [{"stage": "S1 Hook", "creator_time_range": "0s - 3s", "benchmark_time_range": "0s - 3s"}]},
            ["S1"],
        )
        review_text = review["messages"][1]["content"][0]["text"]

        with tempfile.TemporaryDirectory() as tmp:
            analysis_input = write_analysis_input(
                Path(tmp),
                {
                    "analysis_scope": {"label": "视频证据分析", "missing_context": [], "boundary": "仅按视频事实判断"},
                    "product": {"name": "测试品", "category": "", "price": "", "target_market": "auto", "core_selling_points": "", "target_user": "", "purchase_motivation": "", "creator_profile": "", "notes": ""},
                    "videos": {},
                },
            ).read_text(encoding="utf-8")

        self.assertIn(CERTIFICATION_OWNERSHIP_PROMPT, comparison_text)
        self.assertIn(CERTIFICATION_OWNERSHIP_PROMPT, repair_text)
        self.assertIn("不得保留 authority/traceable_data/independent_user", repair_text)
        self.assertIn(CERTIFICATION_OWNERSHIP_PROMPT, analysis_input)
        self.assertNotIn("开头的背书/认证类内容按钩子算", comparison_text)
        self.assertNotIn("只归入 S2", repair_text)
        self.assertNotIn("产品名/卖点/认证", review_text)

    def test_analysis_prompts_do_not_impose_uncontracted_evidence_count_caps(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        comparison_text = json.dumps(comparison, ensure_ascii=False)
        repair_text = json.dumps(build_llm_repair_payload("test", "{}", "error", "input"), ensure_ascii=False)
        fallback_text = json.dumps(build_llm_payload("test", "input", []), ensure_ascii=False)

        for text in (comparison_text, repair_text, fallback_text):
            self.assertNotIn("3 到 6 个关键 evidence_units", text)
            self.assertNotIn("任何列表最多 3 条", text)
        self.assertIn("不得用固定条数截断 Stage1 evidence_units", comparison_text)

    def test_comparison_input_excludes_raw_transcript_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_dir = root / "creator"
            role_dir.mkdir()
            (role_dir / "video.mp4").write_bytes(b"video")
            (role_dir / "transcript.txt").write_text("RAW_FULL_TRANSCRIPT", encoding="utf-8")
            (role_dir / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:20,000\nRAW_SRT_TRANSCRIPT\n",
                encoding="utf-8",
            )
            (role_dir / "transcript.words.json").write_text(
                '{"text":"RAW_WORD_INDEX"}', encoding="utf-8"
            )
            windowed = role_dir / "transcript_windowed.md"
            windowed.write_text("[0.0-2.0] SAFE_WINDOW_TRANSCRIPT", encoding="utf-8")
            raw_pack = role_dir / "transcript_packed.md"
            raw_pack.write_text("RAW_PACK_TRANSCRIPT", encoding="utf-8")
            analysis = {
                "analysis_scope": {
                    "label": "视频证据分析",
                    "missing_context": [],
                    "boundary": "仅按视频事实判断",
                },
                "product": {
                    "name": "测试品",
                    "category": "测试品类",
                    "price": "",
                    "target_market": "auto",
                    "core_selling_points": [],
                    "target_user": "",
                    "purchase_motivation": "",
                    "creator_profile": "",
                    "notes": "",
                },
                "videos": {
                    "creator": {
                        "path": str(role_dir / "video.mp4"),
                        "work_dir": str(role_dir),
                        "duration_seconds": 20.0,
                        "detected_language": "zh",
                        "frames_dir": str(role_dir / "frames"),
                        "focus_frames_dir": str(role_dir / "focus_frames"),
                        "frame_count": 0,
                        "focus_frame_count": 0,
                        "video_evidence": {
                            "transcript_pack_path": str(raw_pack),
                            "transcript_windowed_path": str(windowed),
                        },
                    }
                },
            }

            text = write_analysis_input(root, analysis).read_text(encoding="utf-8")
            payload_text = json.dumps(
                build_llm_comparison_payload("test", text, {}, {"videos": {}}),
                ensure_ascii=False,
            )

        self.assertIn("SAFE_WINDOW_TRANSCRIPT", text)
        self.assertIn("不随本请求发送", text)
        for marker in (
            "RAW_FULL_TRANSCRIPT",
            "RAW_SRT_TRANSCRIPT",
            "RAW_WORD_INDEX",
            "RAW_PACK_TRANSCRIPT",
        ):
            self.assertNotIn(marker, text)
            self.assertNotIn(marker, payload_text)

    def test_multimodal_contract_reaches_all_analysis_prompts(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        repair = build_llm_repair_payload("test", "{}", "error", "input")
        review = build_stage_review_payload(
            "test",
            {"videos": {}},
            {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
            {"stage_analysis": [{"stage": "S1 Hook", "creator_time_range": "0s - 3s", "benchmark_time_range": "0s - 3s"}]},
            ["S1"],
        )
        for payload in (comparison, repair):
            payload_text = json.dumps(payload, ensure_ascii=False)
            self.assertIn("S1-S6 跨模态综合合同", payload_text)
            self.assertIn("禁止按最弱渠道一票否决", payload_text)
            self.assertIn("S3", payload_text)
            self.assertIn("真实使用过程与关键动作可见是硬条件", payload_text)
        review_text = json.dumps(review, ensure_ascii=False)
        self.assertIn("不得输出或改写 severity", review_text)
        self.assertNotIn("creator_multimodal", review_text)

    def test_phase_c_does_not_reuse_stale_multimodal_assessment(self) -> None:
        current = {
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_multimodal": {"integrated_effect": "strong"},
                    "benchmark_multimodal": {"integrated_effect": "strong"},
                }
            ],
            "improvements": [],
        }
        review = {
            "stage_patches": [{
                "stage": "S1",
                "fields": {
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                    "creator_hook": {"exists": True, "evidence_ids": ["C1"]},
                    "benchmark_hook": {"exists": True, "evidence_ids": ["B1"]},
                },
            }]
        }
        facts = {
            "creator": {"evidence_units": [{"id": "C1"}]},
            "benchmark": {"evidence_units": [{"id": "B1"}]},
        }
        with mock.patch.object(pipeline, "_process_llm_result", side_effect=lambda result, *_: result):
            merged = pipeline.apply_stage_review_updates(
                current,
                review,
                {},
                "",
                facts,
                allowed_stage_codes=["S1"],
            )
        stage = merged["stage_analysis"][0]
        self.assertNotIn("creator_multimodal", stage)
        self.assertNotIn("benchmark_multimodal", stage)

    def test_pending_s3_s6_fields_reach_all_contract_surfaces(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        comparison_text = json.dumps(comparison, ensure_ascii=False)
        repair_text = json.dumps(build_llm_repair_payload("test", "{}", "error", "input"), ensure_ascii=False)
        review = build_stage_review_payload(
            "test",
            {"videos": {}},
            {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
            {
                "stage_analysis": [
                    {"stage": "S3 使用过程", "creator_time_range": "8s - 18s", "benchmark_time_range": "8s - 18s"},
                    {"stage": "S6 CTA", "creator_time_range": "25s - 30s", "benchmark_time_range": "25s - 30s"},
                ]
            },
            ["S3", "S6"],
        )
        review_text = json.dumps(review, ensure_ascii=False)
        schema_text = (ROOT / "references" / "analysis-output-schema.json").read_text(encoding="utf-8")
        for field in (
            "action_application_change_visible",
            "soft_purchase_invitation_met",
            "distinct_personas_met",
            "steps_clear_met",
            "pov_immersive_met",
            "price_anchor_met",
            "urgency_evidence_met",
            "gift_stack_met",
            "guarantee_clear_met",
        ):
            self.assertIn(field, comparison_text)
            self.assertIn(field, repair_text)
            self.assertIn(field, review_text)
            self.assertIn(field, schema_text)

        self.assertIn("usage_evidence_state", comparison_text)
        self.assertIn("usage_evidence_state", repair_text)
        self.assertIn("distinct_personas_met", inspect.getsource(validate_s3_usage_flags))
        self.assertIn("action_application_change_visible", inspect.getsource(validate_s3_usage_flags))
        self.assertIn("soft_purchase_invitation_met", inspect.getsource(validate_s6_cta_flags))
        self.assertIn("price_anchor_met", inspect.getsource(validate_s6_cta_flags))

    def test_all_result_entries_delegate_to_one_finalizer(self) -> None:
        self.assertIn("finalize_analysis_result", inspect.getsource(pipeline.merge_analysis_result))
        self.assertIn("finalize_analysis_result", inspect.getsource(pipeline._process_llm_result))
        self.assertIn(
            "finalize_canonical_analysis_result",
            inspect.getsource(pipeline.finalize_analysis_result),
        )
        self.assertEqual(
            inspect.getsource(pipeline.finalize_canonical_analysis_result).count("validate_analysis_dimensions"),
            1,
        )

    def test_preprocess_cache_requires_matching_video_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            video.write_bytes(b"first-video")
            role_dir = root / "creator"
            frames = role_dir / "frames"
            frames.mkdir(parents=True)
            transcript = role_dir / "transcript.txt"
            transcript.write_text("cached transcript", encoding="utf-8")
            transcript_segments = role_dir / "transcript.srt"
            transcript_segments.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\ncurrent segment\n",
                encoding="utf-8",
            )
            args = self._cache_args()
            deps = self._cache_deps()
            fingerprint = flayr.build_preprocess_fingerprint(video, deps, args)
            self.assertNotIn("path", fingerprint["source_video"])
            moved_copy = root / "moved-copy.mp4"
            moved_copy.write_bytes(video.read_bytes())
            self.assertEqual(
                fingerprint,
                flayr.build_preprocess_fingerprint(moved_copy, deps, args),
            )
            video.touch()
            self.assertEqual(
                fingerprint,
                flayr.build_preprocess_fingerprint(video, deps, args),
            )
            (role_dir / "_preprocess.json").write_text(
                json.dumps(
                    {
                        "frames_dir": str(frames),
                        "transcript_path": str(transcript),
                        "transcript_segments_path": str(transcript_segments),
                        "transcript_segments_available": True,
                        "transcription_status": "completed",
                        "preprocess_fingerprint": fingerprint,
                        "preprocess_completed": True,
                        "preprocess_artifacts": flayr._build_preprocess_artifact_manifest(role_dir),
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(flayr.load_existing_video_result(role_dir, fingerprint))

            cached_info = json.loads((role_dir / "_preprocess.json").read_text(encoding="utf-8"))
            cached_info["transcription_status"] = "failed"
            (role_dir / "_preprocess.json").write_text(json.dumps(cached_info), encoding="utf-8")
            self.assertIsNone(flayr.load_existing_video_result(role_dir, fingerprint))

            cached_info["transcription_status"] = "completed"
            transcript.write_text("Online ASR failed; no transcript is available.\n", encoding="utf-8")
            cached_info["preprocess_artifacts"] = flayr._build_preprocess_artifact_manifest(role_dir)
            (role_dir / "_preprocess.json").write_text(json.dumps(cached_info), encoding="utf-8")
            self.assertIsNone(flayr.load_existing_video_result(role_dir, fingerprint))

            transcript.write_text("mutated transcript", encoding="utf-8")
            self.assertIsNone(flayr.load_existing_video_result(role_dir, fingerprint))

            video.write_bytes(b"changed-video")
            self.assertIsNone(flayr.load_existing_video_result(role_dir, flayr.build_preprocess_fingerprint(video, deps, args)))

            args.asr_language = "th"
            self.assertIsNone(flayr.load_existing_video_result(role_dir, flayr.build_preprocess_fingerprint(video, deps, args)))

    def test_secondary_evidence_rebuild_refreshes_preprocess_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            frames_dir = role_dir / "frames"
            frames_dir.mkdir()
            (frames_dir / "frame_001.jpg").write_bytes(b"frame")
            info: dict[str, object] = {
                "audio_quality": {"status": "ready"},
                "speech_mode": {"mode": "spoken"},
                "video_evidence": {},
                "preprocess_artifacts": flayr._build_preprocess_artifact_manifest(role_dir),
            }

            def build_secondary(root: Path, _info: dict[str, object]) -> dict[str, object]:
                (root / "timeline_views").mkdir()
                (root / "timeline_views" / "timeline.json").write_text("{}", encoding="utf-8")
                (root / "frames" / "selection_report.json").write_text("{}", encoding="utf-8")
                (root / "video_evidence_audit.json").write_text("{}", encoding="utf-8")
                return {
                    "timeline_views_dir": str(root / "timeline_views"),
                    "frame_selection_report_path": str(root / "frames" / "selection_report.json"),
                    "audit_path": str(root / "video_evidence_audit.json"),
                }

            with mock.patch.object(flayr, "build_video_evidence_artifacts", side_effect=build_secondary):
                flayr.ensure_video_evidence_artifacts(role_dir, info)
            self.assertTrue(flayr._preprocess_artifacts_match(role_dir, info["preprocess_artifacts"]))
            self.assertIn("timeline_views/timeline.json", info["preprocess_artifacts"]["files"])

    def test_transcript_consumers_never_fallback_to_role_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            (role_dir / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nstale segment\n",
                encoding="utf-8",
            )
            info = {"work_dir": str(role_dir)}
            self.assertEqual(read_srt_segments(info), [])
            self.assertEqual(build_transcript_pack(role_dir, info), {})

    def test_preprocess_rebuild_publishes_isolated_generation_without_old_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            source = root / "source.mp4"
            source.write_bytes(b"video")
            role_dir = run_dir / "creator"
            role_dir.mkdir()
            (role_dir / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nstale segment\n",
                encoding="utf-8",
            )
            args = self._cache_args()
            deps = {
                "ffmpeg": None,
                "ffprobe": None,
                "asr": {
                    "provider": "dashscope",
                    "api_url": args.asr_api_url,
                    "model": args.asr_model,
                    "language": args.asr_language,
                },
            }
            result = flayr.process_video("creator", source, run_dir, deps, args)
            self.assertEqual(result["transcript_segments_path"], None)
            self.assertFalse((role_dir / "transcript.srt").exists())
            self.assertTrue((role_dir / "_preprocess.json").is_file())
            self.assertEqual(list(run_dir.glob(".creator.generation-*")), [])

    def test_legacy_import_preprocessing_never_calls_current_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            source = root / "source.mp4"
            source.write_bytes(b"video")
            args = self._cache_args()
            args.legacy_import = True
            args.translate_with_llm = True
            args.with_ocr = True
            args.llm_model = "vision-model"
            args.llm_api_url = "https://example.test/v1"
            deps = self._cache_deps()

            def fake_extract_audio(_video: Path, audio_path: Path, result: dict[str, object]) -> None:
                audio_path.write_bytes(b"audio")
                result["audio_path"] = str(audio_path)

            with (
                mock.patch.object(flayr, "extract_frames"),
                mock.patch.object(flayr, "extract_audio", side_effect=fake_extract_audio),
                mock.patch.object(flayr, "analyze_audio_quality", return_value={}),
                mock.patch.object(flayr, "run_online_asr") as asr_call,
                mock.patch.object(flayr, "translate_transcript_with_llm") as translation_call,
                mock.patch.object(flayr, "build_subtitle_track") as ocr_call,
                mock.patch.object(flayr, "compute_shake_metric", return_value={}),
                mock.patch.object(flayr, "build_shot_track", return_value={"status": "skipped", "shots": []}),
                mock.patch.object(flayr, "extract_anchor_frames"),
                mock.patch.object(flayr, "classify_speech_mode", return_value={}),
                mock.patch.object(flayr, "build_video_evidence_artifacts", return_value={}),
            ):
                result = flayr.process_video("creator", source, run_dir, deps, args)

            asr_call.assert_not_called()
            translation_call.assert_not_called()
            ocr_call.assert_not_called()
            self.assertEqual(result["transcription_status"], "not_requested_legacy_import")
            self.assertEqual(result["translation_status"], "not_requested_legacy_import")
            self.assertEqual(result["subtitle_track_status"], "not_requested_legacy_import")

    def test_preprocess_promotion_rewrites_nested_json_paths_and_refreshes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / ".creator.generation-staging"
            role_dir = root / "creator"
            nested = staging / "frames"
            nested.mkdir(parents=True)
            frame = nested / "frame_0001.jpg"
            frame.write_bytes(b"frame")
            nested_manifest = nested / "manifest.json"
            nested_manifest.write_text(
                json.dumps({"path": str(frame), "nested": [{"path": str(frame)}]}),
                encoding="utf-8",
            )
            result = {
                "work_dir": str(staging),
                "frames_dir": str(nested),
                "frame_manifest_path": str(nested_manifest),
                "preprocess_artifacts": flayr._build_preprocess_artifact_manifest(staging),
            }

            published = flayr._promote_preprocess_generation(staging, role_dir, result)

            final_frame = role_dir / "frames" / "frame_0001.jpg"
            final_manifest = role_dir / "frames" / "manifest.json"
            self.assertEqual(published["work_dir"], str(role_dir.resolve()))
            self.assertEqual(published["frame_manifest_path"], str(final_manifest.resolve()))
            self.assertEqual(json.loads(final_manifest.read_text(encoding="utf-8"))["path"], str(final_frame.resolve()))
            final_manifest_text = final_manifest.read_text(encoding="utf-8")
            self.assertNotIn(str(staging.absolute()), final_manifest_text)
            self.assertNotIn(str(staging.resolve()), final_manifest_text)
            self.assertTrue(flayr._preprocess_artifacts_match(role_dir, published["preprocess_artifacts"]))

    def test_anchor_times_leave_seek_margin_at_video_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shot_track = root / "shot_track.json"
            shot_track.write_text(
                json.dumps({"shots": [{"start_sec": 0.0, "end_sec": 9.99}]}),
                encoding="utf-8",
            )
            anchors = video._collect_anchor_times(
                {
                    "duration_seconds": 10.0,
                    "shot_track_path": str(shot_track),
                },
                10.0,
            )
            times = [timestamp for timestamp, _reason in anchors]
            self.assertIn(9.9, times)
            self.assertLessEqual(max(times), 9.9)

    def test_online_asr_clears_previous_segments_and_publishes_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            audio = role_dir / "audio.wav"
            audio.write_bytes(b"audio")
            transcript = role_dir / "transcript.txt"
            (role_dir / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nstale segment\n",
                encoding="utf-8",
            )
            (role_dir / "transcript.words.json").write_text("stale", encoding="utf-8")
            response = {
                "output": {
                    "sentence": {
                        "begin_time": 100,
                        "end_time": 900,
                        "text": "new transcript",
                        "words": [{"text": "new", "begin_time": 100, "end_time": 500}],
                    }
                }
            }
            with mock.patch.object(asr, "audio_to_mp3_data_url", return_value="data:audio/mpeg;base64,AA=="), mock.patch.object(
                asr, "_call_asr_endpoint", return_value=response
            ):
                result = {"errors": []}
                asr.run_online_asr(
                    "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                    "fun-asr-flash-2026-06-15",
                    "test-key",
                    "en",
                    audio,
                    role_dir,
                    transcript,
                    result,
                )
            self.assertEqual(result["transcription_status"], "completed")
            self.assertTrue(result["transcript_segments_available"])
            self.assertTrue(result["transcript_words_available"])
            self.assertEqual(transcript.read_text(encoding="utf-8").strip(), "new transcript")
            self.assertIn("new transcript", (role_dir / "transcript.srt").read_text(encoding="utf-8"))

    def test_online_asr_persists_and_strictly_replays_provider_artifact(self) -> None:
        response = {
            "output": {
                "sentence": {
                    "begin_time": 100,
                    "end_time": 900,
                    "text": "replay transcript",
                    "words": [{"text": "replay", "begin_time": 100, "end_time": 500}],
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_role = root / "first" / "benchmark"
            second_role = root / "second" / "benchmark"
            first_role.mkdir(parents=True)
            second_role.mkdir(parents=True)
            first_audio = first_role / "audio.wav"
            second_audio = second_role / "audio.wav"
            first_audio.write_bytes(b"same-audio")
            second_audio.write_bytes(b"same-audio")
            with mock.patch.object(asr, "audio_to_mp3_data_url", return_value="data:audio/mpeg;base64,AA=="), mock.patch.object(
                asr, "_call_asr_endpoint", return_value=response
            ) as call:
                first_result = {"errors": []}
                asr.run_online_asr(
                    asr.DEFAULT_FUN_ASR_API_URL,
                    asr.DEFAULT_FUN_ASR_MODEL,
                    "test-key",
                    "en",
                    first_audio,
                    first_role,
                    first_role / "transcript.txt",
                    first_result,
                )
            self.assertEqual(first_result["transcription_status"], "completed")
            self.assertEqual(call.call_count, 1)
            saved = json.loads((first_role / "provider_asr.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["request_identity"]["call_kind"], "asr")
            self.assertEqual(saved["response_meta"]["execution_source"], "live")

            with (
                mock.patch.object(asr, "audio_to_mp3_data_url", return_value="data:audio/mpeg;base64,AA=="),
                mock.patch.object(
                    asr,
                    "_call_asr_endpoint",
                    side_effect=AssertionError("strict replay must not call ASR provider"),
                ) as replay_call,
            ):
                replay_result = {"errors": []}
                asr.run_online_asr(
                    asr.DEFAULT_FUN_ASR_API_URL,
                    asr.DEFAULT_FUN_ASR_MODEL,
                    "",
                    "en",
                    second_audio,
                    second_role,
                    second_role / "transcript.txt",
                    replay_result,
                    provider_replay_from=root / "first",
                )
            self.assertEqual(replay_result["transcription_status"], "completed")
            self.assertEqual(replay_result["transcription_execution_source"], "technical_replay")
            replay_call.assert_not_called()
            replay_saved = json.loads((second_role / "provider_asr.json").read_text(encoding="utf-8"))
            self.assertEqual(replay_saved["response_meta"]["execution_source"], "technical_replay")

            mismatch_role = root / "mismatch" / "benchmark"
            mismatch_role.mkdir(parents=True)
            mismatch_audio = mismatch_role / "audio.wav"
            mismatch_audio.write_bytes(b"same-audio")
            with (
                mock.patch.object(asr, "audio_to_mp3_data_url", return_value="data:audio/mpeg;base64,AA=="),
                mock.patch.object(
                    asr,
                    "_call_asr_endpoint",
                    side_effect=AssertionError("identity mismatch must not call ASR provider"),
                ) as mismatch_call,
                self.assertRaisesRegex(ProviderReplayError, "identity mismatch"),
            ):
                mismatch_result = {"errors": []}
                asr.run_online_asr(
                    asr.DEFAULT_FUN_ASR_API_URL,
                    "different-asr-model",
                    "",
                    "en",
                    mismatch_audio,
                    mismatch_role,
                    mismatch_role / "transcript.txt",
                    mismatch_result,
                    provider_replay_from=root / "first",
                )
            mismatch_call.assert_not_called()
            mismatch_artifact = json.loads((mismatch_role / "provider_asr.json").read_text(encoding="utf-8"))
            self.assertEqual(mismatch_artifact["status"], "failed")
            self.assertIn("identity mismatch", mismatch_artifact["error"])

    def test_online_asr_replay_uses_stable_role_name_for_staging_directory(self) -> None:
        response = {
            "output": {
                "sentence": {
                    "begin_time": 0,
                    "end_time": 500,
                    "text": "stable replay",
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_role = root / "source" / "benchmark"
            staging_role = root / "target" / ".benchmark.generation-random"
            source_role.mkdir(parents=True)
            staging_role.mkdir(parents=True)
            for role in (source_role, staging_role):
                (role / "audio.wav").write_bytes(b"same-audio")

            with mock.patch.object(asr, "audio_to_mp3_data_url", return_value="data:audio/mpeg;base64,AA=="), mock.patch.object(
                asr, "_call_asr_endpoint", return_value=response
            ):
                asr.run_online_asr(
                    asr.DEFAULT_FUN_ASR_API_URL,
                    asr.DEFAULT_FUN_ASR_MODEL,
                    "test-key",
                    "en",
                    source_role / "audio.wav",
                    source_role,
                    source_role / "transcript.txt",
                    {"errors": []},
                )

            with mock.patch.object(asr, "audio_to_mp3_data_url", return_value="data:audio/mpeg;base64,AA=="), mock.patch.object(
                asr,
                "_call_asr_endpoint",
                side_effect=AssertionError("strict replay must not call ASR provider"),
            ) as replay_call:
                result = {"errors": []}
                asr.run_online_asr(
                    asr.DEFAULT_FUN_ASR_API_URL,
                    asr.DEFAULT_FUN_ASR_MODEL,
                    "",
                    "en",
                    staging_role / "audio.wav",
                    staging_role,
                    staging_role / "transcript.txt",
                    result,
                    provider_replay_from=root / "source",
                    replay_role_name="benchmark",
                )

            self.assertEqual(result["transcription_execution_source"], "technical_replay")
            replay_call.assert_not_called()

            with self.assertRaisesRegex(ValueError, "invalid provider replay role"):
                asr.run_online_asr(
                    asr.DEFAULT_FUN_ASR_API_URL,
                    asr.DEFAULT_FUN_ASR_MODEL,
                    "",
                    "en",
                    staging_role / "audio.wav",
                    staging_role,
                    staging_role / "transcript.txt",
                    {"errors": []},
                    provider_replay_from=root / "source",
                    replay_role_name="../benchmark",
                )

    def test_ocr_and_translation_replay_use_published_role_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay_root = root / "source"
            staging_role = root / "target" / ".creator.generation-random"
            frame = staging_role / "frame.jpg"
            staging_role.mkdir(parents=True)
            frame.write_bytes(b"jpeg")
            info = {
                "duration_seconds": 1.0,
                "frames": [{"path": str(frame), "timestamp_seconds": 0.0}],
                "focus_frames": [],
            }
            with mock.patch.object(
                subtitle_track,
                "ocr_frame_with_retry",
                return_value=(["subtitle"], "ok"),
            ) as ocr_call:
                subtitle_track.build_subtitle_track(
                    staging_role,
                    info,
                    "",
                    api_url="https://example.test/v1/chat/completions",
                    model="qwen3-vl-plus",
                    provider_replay_from=replay_root,
                    replay_role_name="creator",
                )
            self.assertEqual(
                ocr_call.call_args.kwargs["provider_replay_from"],
                (replay_root / "creator" / "ocr_raw").resolve(),
            )

            (staging_role / "transcript.txt").write_text("source", encoding="utf-8")
            args = SimpleNamespace(
                translation_model="qwen3.7-plus",
                llm_model="qwen3.7-plus",
                product_name="",
                product_notes="",
                llm_dry_run=False,
                llm_api_url="https://example.test/v1/chat/completions",
                provider_replay_from=replay_root,
            )
            provider_response = {
                "choices": [{"message": {"content": "translation"}, "finish_reason": "stop"}]
            }
            with mock.patch.object(
                translation,
                "provider_call_with_artifact",
                return_value=(provider_response, {}, "technical_replay"),
            ) as translation_call:
                translation.translate_transcript_with_llm(args, "creator", staging_role, {"errors": []})
            self.assertEqual(
                translation_call.call_args.kwargs["replay_root"],
                (replay_root / "creator").resolve(),
            )

    def test_online_asr_replay_does_not_clear_in_place_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_dir = root / "benchmark"
            role_dir.mkdir(parents=True)
            transcript = role_dir / "transcript.txt"
            transcript.write_text("source transcript\n", encoding="utf-8")
            result = {"errors": []}
            asr.run_online_asr(
                asr.DEFAULT_FUN_ASR_API_URL,
                asr.DEFAULT_FUN_ASR_MODEL,
                "",
                "en",
                role_dir / "missing.wav",
                role_dir,
                transcript,
                result,
                provider_replay_from=root,
            )
            self.assertEqual(transcript.read_text(encoding="utf-8"), "source transcript\n")
            self.assertEqual(result["transcription_status"], "failed")
            self.assertIn("output must differ", result["errors"][-1])

    def test_online_asr_does_not_reuse_unrelated_llm_key(self) -> None:
        args = SimpleNamespace(
            asr_api_key_env="FLAYR_TEST_MISSING_ASR_KEY",
            llm_api_url="https://api.openai.com/v1/chat/completions",
            llm_api_key_env="OPENAI_API_KEY",
            llm_api_key_keychain_service="",
            llm_api_key_keychain_account="",
        )
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "openai-key"}, clear=False), mock.patch.object(
            asr, "read_llm_api_key", return_value="openai-key"
        ):
            self.assertEqual(asr.read_asr_api_key(args), "")

    def test_online_asr_payload_matches_realtime_and_flash_contracts(self) -> None:
        realtime = asr._build_asr_payload("fun-asr-realtime", "data:audio/mp3;base64,AA==", "auto")
        realtime_content = realtime["input"]["messages"][0]["content"][0]
        self.assertEqual(realtime_content, {"audio": "data:audio/mp3;base64,AA=="})
        self.assertNotIn("language_hints", realtime["parameters"])

        flash = asr._build_asr_payload("fun-asr-flash-2026-06-15", "data:audio/mp3;base64,AA==", "ms")
        flash_content = flash["input"]["messages"][0]["content"][0]
        self.assertEqual(
            flash_content,
            {
                "type": "input_audio",
                "input_audio": {"data": "data:audio/mp3;base64,AA=="},
            },
        )
        self.assertEqual(flash["parameters"]["language_hints"], ["ms"])

    def test_segmented_pipeline_keeps_unknown_core_stages_degraded(self) -> None:
        unresolved = pipeline._segmented_stage_unresolved(
            [
                {
                    "stage": "S1 Hook",
                    "analysis_status": "grounded",
                    "stage_state": "unknown",
                    "model_gap_magnitude": "medium",
                },
                {
                    "stage": "S4 效果呈现",
                    "analysis_status": "evidence_blocked",
                    "stage_state": "unknown",
                    "model_gap_magnitude": "uncertain",
                },
            ]
        )
        self.assertEqual(unresolved, ["S1", "S4"])
        self.assertEqual(pipeline._segmented_text_items("一条完整理由"), ["一条完整理由"])
        self.assertEqual(pipeline._segmented_text_items(["a", "b"]), ["a", "b"])

    def test_segmented_stage_projection_does_not_create_unknown_severity(self) -> None:
        projected = pipeline._normalize_segmented_stage(
            {
                "stage": "S4 效果呈现",
                "stage_state": "unknown",
                "analysis_status": "evidence_blocked",
                "relation": "uncertain",
                "model_gap_magnitude": "uncertain",
                "judgment_reason": "Stage1 证据未闭合",
            },
            "S4",
            {"benchmark": {}, "creator": {}},
        )

        self.assertIsNone(projected["model_severity"])
        self.assertIsNone(projected["severity"])

        unclosed = pipeline._normalize_segmented_stage(
            {
                "stage": "S1 Hook",
                "stage_state": "unknown",
                "relation": "benchmark_better",
                "model_gap_magnitude": "large",
            },
            "S1",
            {
                "benchmark": {"stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION},
                "creator": {"stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION},
            },
        )

        self.assertEqual(unclosed["relation"], "uncertain")
        self.assertEqual(unclosed["model_gap_magnitude"], "uncertain")
        self.assertEqual(unclosed["stage_handoff_status"], "evidence_blocked")

    def test_stage_group_payload_requires_stage_state_and_excludes_whole_report_contract(self) -> None:
        payload = build_stage_group_judgment_payload(
            "test-model",
            "input",
            {},
            {"videos": {}},
            ["S1", "S2"],
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn("stage_state", text)
        self.assertIn("stage_state 是必填语义字段", text)
        strict_output = text.split("## 输出严格 JSON", 1)[1]
        self.assertIn('"benchmark_evidence_ids"', strict_output)
        self.assertIn('"creator_evidence_ids"', strict_output)
        self.assertNotIn('"improvements":', text)
        self.assertNotIn('"commercial_priority":', text)
        self.assertNotIn('"benchmark_summary":', text)
        self.assertNotIn('"creator_summary":', text)
        self.assertIn('"projection"', text)
        self.assertEqual(
            [item.get("type") for item in payload["messages"][1]["content"] if isinstance(item, dict)],
            ["text"],
        )

    def test_model_uncertainty_alone_cannot_trigger_phase_c(self) -> None:
        args = SimpleNamespace(
            llm_model="test-model",
            llm_api_url="https://example.invalid/api",
            provider_replay_from=None,
            _resource_budget=None,
        )
        raw_result = {"low_confidence_stages": ["S4"]}
        result = {"stage_analysis": [{"stage": "S4", "severity": "small"}]}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(pipeline, "detect_low_confidence_stages", return_value=[]),
            mock.patch.object(pipeline, "detect_visual_coverage_gap_stages", return_value=[]),
            mock.patch.object(pipeline, "detect_unreferenced_visual_event_stages", return_value=[]),
            mock.patch.object(pipeline, "critical_severity_stages", return_value=[]),
            mock.patch.object(pipeline, "build_stage_review_payload") as payload_builder,
        ):
            refined = pipeline.maybe_refine_low_confidence_stages(
                args,
                "secret",
                raw_result,
                result,
                "input",
                Path(tmp),
                {"videos": {}},
                {"creator": {}, "benchmark": {}},
            )
        self.assertIs(refined, result)
        payload_builder.assert_not_called()

    def test_stage_synthesis_uses_provider_compatible_budget_field(self) -> None:
        qwen_payload = build_stage_synthesis_payload("qwen3.6-plus", "input", {}, [], {})
        generic_payload = build_stage_synthesis_payload("qwen3.7-max", "input", {}, [], {})
        self.assertEqual(qwen_payload["max_completion_tokens"], 8192)
        self.assertNotIn("max_tokens", qwen_payload)
        self.assertEqual(generic_payload["max_tokens"], 8192)
        self.assertNotIn("max_completion_tokens", generic_payload)

    def test_stage1_a_and_stage1_b_are_separate_contracts(self) -> None:
        analysis = {
            "product": {"name": "测试品"},
            "videos": {"creator": {"duration_seconds": 12.0}},
        }
        primary = build_video_fact_payload(
            "test-model",
            "creator",
            analysis,
            [],
            api_url="",
        )
        primary_text = json.dumps(primary, ensure_ascii=False)
        self.assertIn("Stage1-A 原子事实合同", primary_text)
        self.assertIn("fact_quality", primary_text)
        self.assertIn("causal_link", primary_text)
        self.assertNotIn('"stage_evidence_checks": [', primary_text)
        self.assertNotIn('"structure_event_checks": [', primary_text)

        qualification = build_stage_evidence_qualification_payload(
            "test-model",
            "creator",
            analysis,
            {
                "stage1_acquisition": {"status": "ready"},
                "evidence_units": [
                    {
                        "id": "C1",
                        "time_range": "0s - 2s",
                        "information": "看到产品被拿起",
                        "visual_fact": "手拿产品",
                        "evidence_strength": "direct",
                        "fact_quality": {
                            "subject": "correct",
                            "visibility": "clear",
                            "composition": "central",
                            "completion": "complete",
                            "proof": "none",
                            "causal_link": "not_applicable",
                        },
                    }
                ],
            },
        )
        qualification_text = qualification["messages"][1]["content"][0]["text"]
        self.assertIn("Stage1-B 阶段资格投影", qualification_text)
        self.assertIn("fact_quality", qualification_text)
        self.assertIn('"stage_evidence_checks"', qualification_text)
        self.assertEqual(
            [item.get("type") for item in qualification["messages"][1]["content"] if isinstance(item, dict)],
            ["text"],
        )

    def test_stage1_qualification_prompt_has_one_exact_template_per_stage(self) -> None:
        analysis = {
            "product": {"name": "测试品"},
            "videos": {"creator": {"duration_seconds": 12.0}},
        }
        payload = build_stage_evidence_qualification_payload(
            "test-model",
            "creator",
            analysis,
            {"evidence_units": []},
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertEqual(text.count('"stage": "S1"'), 3)
        for stage, signal in (
            ("S1", "stop_trigger"),
            ("S2", "problem_to_product_bridge"),
            ("S3", "target_contact"),
            ("S4", "effect_attribution"),
            ("S5", "source_basis"),
            ("S6", "purchase_path"),
        ):
            self.assertIn(f'"stage": "{stage}"', text)
            self.assertIn(f'"{signal}"', text)
        self.assertNotIn('"stage": "S1",\n                            "status": "present|absent|unknown|conflict|not_applicable",\n                            "coverage":', text)

        scoped = build_stage_evidence_qualification_payload(
            "test-model",
            "creator",
            analysis,
            {"evidence_units": []},
            ["S4"],
        )
        scoped_text = scoped["messages"][1]["content"][0]["text"]
        self.assertIn("目标阶段 S4", scoped_text)
        self.assertIn('"stage": "S4"', scoped_text)
        self.assertNotIn('"stop_trigger"', scoped_text)

    def test_stage1_qualification_groups_cover_each_stage_once(self) -> None:
        flattened = [stage for group in STAGE1_QUALIFICATION_GROUPS for stage in group]
        self.assertEqual(flattened, list(stage_codes()))
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(STAGE_JUDGMENT_GROUPS, STAGE1_QUALIFICATION_GROUPS)

    def test_stage1_recovery_prompt_is_target_scoped(self) -> None:
        analysis = {
            "product": {"name": "测试品"},
            "videos": {"creator": {"duration_seconds": 12.0}},
        }
        payload = build_video_fact_recovery_payload(
            "test-model",
            "creator",
            analysis,
            [],
            {"evidence_units": []},
            ["S4"],
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn('"stage": "S4"', text)
        self.assertIn('"result_difference"', text)
        self.assertIn('"effect_attribution"', text)
        self.assertNotIn('"stop_trigger"', text)
        self.assertNotIn('"product_identity"', text)

    def test_stage1_recovery_exposes_candidates_and_reviews_unclosed_s6_tail(self) -> None:
        analysis = {
            "product": {"name": "测试品"},
            "videos": {"creator": {"duration_seconds": 60.0}},
        }
        facts = {
            "evidence_units": [],
            "stage_evidence_checks": [
                {
                    "stage": "S6",
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                }
            ],
            "candidate_evidence_ids_by_stage": {"S6": ["C7"]},
            "candidate_observations_by_stage": {
                "S6": [
                    {
                        "id": "C7",
                        "time_range": "51.3s - 59.1s",
                        "voiceover": "bek kuning",
                        "functions": ["S6_cta"],
                    }
                ]
            },
        }
        payload = build_video_fact_recovery_payload(
            "test-model",
            "creator",
            analysis,
            [],
            facts,
            ["S6"],
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertIn("未资格化恢复线索", text)
        self.assertIn('"candidate_observations_by_stage"', text)
        self.assertIn('"id": "C7"', text)
        self.assertIn("beg kuning", text)
        self.assertIn("S6 尾段 CTA 定向复核", text)
        self.assertNotIn("当前 Stage1 明确把 S6 判为 absent", text)

    def test_stage1_recovery_does_not_tail_review_closed_s6(self) -> None:
        analysis = {
            "product": {"name": "测试品"},
            "videos": {"creator": {"duration_seconds": 60.0}},
        }
        facts = {
            "evidence_units": [],
            "stage_evidence_checks": [
                {
                    "stage": "S6",
                    "status": "present",
                    "coverage": "complete",
                    "evidence_ids": ["C7"],
                    "observed_signals": ["explicit_action", "purchase_path"],
                    "missing_signals": [],
                }
            ],
        }
        payload = build_video_fact_recovery_payload(
            "test-model",
            "creator",
            analysis,
            [],
            facts,
            ["S6"],
        )
        text = payload["messages"][1]["content"][0]["text"]
        self.assertNotIn("S6 尾段 CTA 定向复核", text)

    def test_stage1_recovery_prompt_excludes_execution_provenance(self) -> None:
        analysis = {
            "product": {"name": "测试品"},
            "videos": {"creator": {"duration_seconds": 12.0}},
        }
        facts = {
            "evidence_units": [{"id": "C1", "information": "已有事实"}],
            "stage_evidence_checks": [
                {"stage": "S3", "status": "unknown"},
                {"stage": "S4", "status": "unknown"},
            ],
            "stage1_acquisition": {
                "provider_artifacts": [
                    {"execution_source": "provider", "response_sha256": "audit-only"}
                ]
            },
            "stage1_qualification": {"status": "completed"},
            "stage1_coverage_audit": {"status": "partial"},
        }

        payload = build_video_fact_recovery_payload(
            "test-model",
            "creator",
            analysis,
            [],
            facts,
            ["S3"],
        )
        text = payload["messages"][1]["content"][0]["text"]

        self.assertIn('"id": "C1"', text)
        self.assertIn('"stage": "S3"', text)
        self.assertNotIn('"stage": "S4"', text)
        self.assertNotIn("execution_source", text)
        self.assertNotIn("provider_artifacts", text)
        self.assertNotIn("stage1_qualification", text)
        self.assertNotIn("stage1_coverage_audit", text)

    def test_segmented_finalizer_recomputes_status_and_removes_unknown_severity(self) -> None:
        result = {
            "stage2_pipeline_version": "segmented_stage_v1",
            "stage2_pipeline_status": "completed",
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "analysis_status": "grounded" if index != 4 else "evidence_blocked",
                    "stage_state": "completed" if index != 4 else "unknown",
                    "model_gap_magnitude": "medium" if index != 4 else "uncertain",
                    "severity": "medium",
                    "model_severity": "medium",
                }
                for index in range(1, 7)
            ],
            "segmented_pipeline": {
                "stage_groups": [
                    {"group": ["S1", "S2"], "status": "completed"},
                    {"group": ["S3", "S4"], "status": "completed"},
                    {"group": ["S5"], "status": "completed"},
                    {"group": ["S6"], "status": "completed"},
                ],
                "synthesis_status": "completed",
            },
        }
        status, unresolved = pipeline._refresh_segmented_pipeline_status(result)
        pipeline._clear_segmented_unresolved_severity(result, unresolved)
        self.assertEqual(status, "degraded")
        self.assertEqual(unresolved, ["S4"])
        self.assertEqual(result["stage2_pipeline_status"], "degraded")
        self.assertIsNone(result["stage_analysis"][3]["severity"])
        self.assertIsNone(result["stage_analysis"][3]["model_severity"])
        self.assertEqual(result["stage_analysis"][0]["severity"], "medium")

    def test_segmented_finalizer_can_close_pre_finalization_degraded_status(self) -> None:
        result = {
            "stage2_pipeline_version": "segmented_stage_v1",
            # The live runner may mark this degraded before the final evidence
            # gate turns intentionally closed comparison stages terminal.
            "stage2_pipeline_status": "degraded",
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "analysis_status": "grounded" if index <= 3 else "not_comparable",
                    "comparison_status": None if index <= 3 else "not_directly_comparable",
                    "stage_state": "completed" if index <= 3 else "unknown",
                    "model_gap_magnitude": "small" if index <= 3 else "uncertain",
                }
                for index in range(1, 7)
            ],
            "segmented_pipeline": {
                "stage_groups": [
                    {"group": ["S1", "S2"], "status": "completed"},
                    {"group": ["S3", "S4"], "status": "completed"},
                    {"group": ["S5"], "status": "completed"},
                    {"group": ["S6"], "status": "completed"},
                ],
                "synthesis_status": "completed",
            },
        }

        status, unresolved = pipeline._refresh_segmented_pipeline_status(result)

        self.assertEqual(status, "completed")
        self.assertEqual(unresolved, [])
        self.assertEqual(result["stage2_pipeline_status"], "completed")

    def test_segmented_resolver_does_not_create_medium_for_unresolved_stage(self) -> None:
        from flayr_core.postprocess.derive import derive_severity_from_facts

        result = {
            "stage2_pipeline_version": "segmented_stage_v1",
            "stage_analysis": [{
                "stage": "S4 效果呈现",
                "analysis_status": "evidence_blocked",
                "model_gap_magnitude": "uncertain",
                "severity": "medium",
                "model_severity": "medium",
            }],
        }
        derive_severity_from_facts(result)
        stage = result["stage_analysis"][0]
        self.assertIsNone(stage["severity"])

        grounded_but_unclosed = {
            "stage2_pipeline_version": "segmented_stage_v1",
            "stage_analysis": [{
                "stage": "S1 Hook",
                "analysis_status": "grounded",
                "stage_state": "unknown",
                "model_gap_magnitude": "medium",
                "severity": "medium",
                "model_severity": "medium",
            }],
        }
        derive_severity_from_facts(grounded_but_unclosed)
        self.assertIsNone(grounded_but_unclosed["stage_analysis"][0]["severity"])
        self.assertIsNone(stage["model_severity"])
        self.assertEqual(stage["severity_derivation"]["status"], "evidence_blocked")

    def test_online_asr_can_fallback_to_same_qwen_endpoint_key(self) -> None:
        args = SimpleNamespace(
            asr_api_key_env="FLAYR_TEST_MISSING_ASR_KEY",
            llm_api_url="https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/v1/chat/completions",
            llm_api_key_env="QWEN_API_KEY",
            llm_api_key_keychain_service="",
            llm_api_key_keychain_account="",
        )
        with mock.patch.object(asr, "read_llm_api_key", return_value="qwen-key"):
            self.assertEqual(asr.read_asr_api_key(args), "qwen-key")

    def test_default_run_dir_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(flayr, "DEFAULT_RUNS_DIR", Path(tmp)):
            args = SimpleNamespace(output_dir=None, mode="improve")
            first = flayr.create_run_dir(args)
            second = flayr.create_run_dir(args)
        self.assertNotEqual(first, second)

    def test_translation_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            (role_dir / "transcript.txt").write_text("Ini contoh", encoding="utf-8")
            result = {"errors": []}
            args = SimpleNamespace(
                translation_model="test-model", llm_model="", product_name="", product_notes="",
                llm_dry_run=False, llm_api_url="https://example.invalid",
            )
            with mock.patch.object(translation, "read_llm_api_key", return_value="test-key"), mock.patch.object(
                translation, "call_llm_api", side_effect=SystemExit("network failed")
            ) as call:
                translation.translate_transcript_with_llm(args, "creator", role_dir, result)
            self.assertEqual(result["translation_status"], "failed")
            self.assertTrue(any("network failed" in str(item) for item in result["errors"]))
            self.assertIn("response_meta", call.call_args.kwargs)
            self.assertTrue((role_dir / "translation_provider_meta.json").is_file())

    @staticmethod
    def _global_result() -> dict[str, object]:
        stages = []
        for index in range(1, 7):
            stages.append(
                {
                    "stage": f"S{index}",
                    "severity": "small",
                    "comparison_status": "direct",
                    "gap": f"S{index} gap",
                    "creator_evidence_ids": ["C1"],
                    "creator_absolute_status": "complete",
                }
            )
        return {
            "product_profile": {},
            "video_understanding": {
                "creator": {
                    "temporal_evidence_mode": "full_temporal",
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "selling_point_observations": [],
                    "variant_decision_rule": {},
                    "attention_competitors": [],
                    "evidence_units": [
                        {
                            "id": "C1",
                            "time_range": "0.0s - 10.0s",
                            "variant_ids": [],
                            "variant_visual_shares": {},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "none",
                            "comparison_purpose_explicit": False,
                            "variant_attribution_confident": False,
                            "variant_data_valid": True,
                            "functions": ["S3_usage", "S4_effect"],
                        }
                    ],
                },
                "benchmark": {
                    "temporal_evidence_mode": "full_temporal",
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "selling_point_observations": [],
                    "variant_decision_rule": {},
                    "attention_competitors": [],
                    "evidence_units": [{"id": "B1", "time_range": "0.0s - 10.0s", "variant_ids": [], "variant_data_valid": True}],
                },
            },
            "stage_analysis": stages,
            "improvements": [{"target_stage": "S4", "title": "强化效果证明"}],
        }

    @staticmethod
    def _cache_args() -> SimpleNamespace:
        return SimpleNamespace(
            asr_language="auto",
            asr_api_url="https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            asr_model="fun-asr-flash-2026-06-15",
            asr_api_key_env="DASHSCOPE_API_KEY",
            translate_with_llm=False,
            translation_model="", llm_model="", llm_api_url="", product_name="", product_notes="",
            ocr_mode="off", with_ocr=False, no_ocr=False, llm_dry_run=True,
        )

    @staticmethod
    def _cache_deps() -> dict[str, object]:
        return {
            "ffmpeg": "ffmpeg",
            "ffprobe": "ffprobe",
            "asr": {
                "provider": "dashscope",
                "api_url": "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
                "model": "fun-asr-flash-2026-06-15",
                "language": "auto",
            },
        }

if __name__ == "__main__":
    unittest.main()
