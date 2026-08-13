from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm import pipeline as pipeline_module  # noqa: E402
from flayr_core.llm.pipeline import (  # noqa: E402
    _build_stage1_to_stage2_handoff,
    _normalize_segmented_stage,
    _project_synthesis_improvements,
    _stage1_to_stage2_handoff_issues,
    _mark_legacy_import_result,
    _full_provider_replay_requested,
    merge_analysis_result,
    run_large_model_analysis,
)
from flayr_core.llm.parse import normalize_severity  # noqa: E402
from flayr_core.llm.provider_artifacts import (  # noqa: E402
    ProviderArtifactError,
    provider_call_with_artifact,
)
from scripts.audit_result_field_ownership import inventory, ownership_violations  # noqa: E402
from flayr_core.llm.stage_fact_artifacts import (  # noqa: E402
    completed_stage_fact_artifact,
    failed_stage_fact_artifact,
    reusable_stage_fact_response,
)
from scripts.check_change_scope import EMPTY_TREE_SHA, _changed_paths, check_scope  # noqa: E402
from scripts.verify_bitter_lesson_contract import (  # noqa: E402
    FrozenContractError,
    _sha256,
    load_spec,
    validate_spec,
)
from flayr_core.verification_order import (  # noqa: E402
    VerificationOrderError,
    assert_verification_order,
    run_verification_stage,
)
from flayr_core.run_manifest import write_success_manifest  # noqa: E402
from flayr_core.run_state import (  # noqa: E402
    ANALYSIS_COMPLETED,
    COMPLETED,
    PROCESSING,
    REPORT_GENERATING,
    initialize_run_state,
    transition_run_state,
)


def _provider_meta(request_id: str = "fixture-request") -> dict[str, object]:
    return {
        "logical_request_id": request_id,
        "completion_attempts": 1,
        "retry_reasons": [],
        "usage": {},
    }


def _run_verifier(root: Path, stage: str, *, content: str = "passed") -> Path:
    evidence = root / f"{stage}.evidence.txt"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; "
        f"Path({str(evidence)!r}).write_text({content!r}, encoding='utf-8')",
    ]
    completed_run_dir = None
    if stage in {"ordinary_sample", "boundary_sample"}:
        completed_run_dir = root / f"{stage}.run"
        completed_run_dir.mkdir(parents=True, exist_ok=True)
        command[-1] += (
            "; "
            f"Path({str(completed_run_dir / '_SUCCESS.json')!r}).write_text({content!r}, encoding='utf-8')"
        )
    return run_verification_stage(
        root,
        stage,
        command=command,
        evidence_paths=[evidence],
        completed_run_dir=completed_run_dir,
        repo_root=ROOT,
    )


def _write_completed_run(run_dir: Path, code_commit: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    initialize_run_state(run_dir)
    for state in (PROCESSING, ANALYSIS_COMPLETED, REPORT_GENERATING, COMPLETED):
        transition_run_state(run_dir, state)
    (run_dir / "analysis.json").write_text(
        json.dumps({"analysis_run_state": "completed", "mode": "analyze"}),
        encoding="utf-8",
    )
    (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")
    for artifact in (
        "raw_model_response.json",
        "validated_normalized_result.json",
        "postprocess_change_log.json",
    ):
        (run_dir / artifact).write_text("{}\n", encoding="utf-8")
    (run_dir / "final_derived_result.json").write_text(
        json.dumps(
            {
                "postprocess_provenance": {
                    "field_sources": {
                        "coverage": "complete",
                        "unresolved_paths": [],
                        "truncated": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    write_success_manifest(
        run_dir,
        {},
        {"analysis_run_state": "completed", "mode": "analyze"},
        {"code_commit": code_commit},
    )


class BitterLessonContractTests(unittest.TestCase):
    def test_frozen_contract_hash_is_checkout_newline_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf.py"
            crlf = root / "crlf.py"
            lf.write_bytes(b"VALUE = 1\nVALUE = 2\n")
            crlf.write_bytes(b"VALUE = 1\r\nVALUE = 2\r\n")
            self.assertEqual(_sha256(lf), _sha256(crlf))

    def test_layer_ownership_is_unique(self) -> None:
        spec = load_spec()
        validate_spec(spec)
        self.assertEqual(
            [layer["id"] for layer in spec["layers"]],
            ["provider", "canonical", "finalizer", "report"],
        )
        self.assertEqual(
            len({layer["owner"] for layer in spec["layers"]}),
            len(spec["layers"]),
        )

    def test_frozen_spec_rejects_semantic_drift(self) -> None:
        spec = load_spec()
        spec["types"]["evidence_state"]["values"][0] = "medium"
        with self.assertRaises(FrozenContractError):
            validate_spec(spec)

        spec = load_spec()
        spec["invariants"] = spec["invariants"][:-1]
        with self.assertRaises(FrozenContractError):
            validate_spec(spec)

    def test_verification_order_is_frozen(self) -> None:
        spec = load_spec()
        self.assertEqual(
            spec["verification_order"],
            ["fixture", "offline_replay", "fake_provider", "ordinary_sample", "boundary_sample"],
        )

    def test_stage1_handoff_is_hash_bound_and_lossless(self) -> None:
        side = {
            "evidence_set_version": "evidence_snapshot_v1",
            "evidence_set_sha256": "ledger-hash",
            "evidence_units": [{"id": "C1", "visual_fact": "真实观察"}],
            "stage_evidence_checks": [
                {
                    "stage": f"S{index}",
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "signal_bindings": {},
                }
                for index in range(1, 7)
            ],
            "stage1_coverage_audit": {"version": "coverage_audit_v1", "stages": {}},
            "stage1_acquisition": {"version": "stage1_acquisition_v1", "channels": {}},
        }
        facts = {"benchmark": copy.deepcopy(side), "creator": copy.deepcopy(side)}
        analysis = {"videos": {"benchmark": {}, "creator": {}}}
        handoff = _build_stage1_to_stage2_handoff(facts, analysis)
        self.assertEqual(_stage1_to_stage2_handoff_issues(handoff, facts, analysis), [])
        handoff["roles"]["creator"]["ledger_manifest"]["units"].clear()
        self.assertIn(
            "creator:handoff_field_mismatch:ledger_manifest",
            _stage1_to_stage2_handoff_issues(handoff, facts, analysis),
        )

    def test_unknown_stage_never_becomes_publishable_severity(self) -> None:
        stage = _normalize_segmented_stage(
            {
                "stage": "S4",
                "relation": "creator_better",
                "model_gap_magnitude": "large",
                "judgment_reason": "provider supplied a conclusion",
            },
            "S4",
            {"creator": {}, "benchmark": {}},
            {"overall_status": "comparable"},
        )
        self.assertEqual(stage["stage_state"], "unknown")
        self.assertEqual(stage["model_gap_magnitude"], "uncertain")

    def test_provider_artifact_replay_requires_exact_identity(self) -> None:
        payload = {"model": "test", "messages": [{"role": "user", "content": "fixture"}]}
        artifact = completed_stage_fact_artifact(
            role="creator",
            phase="A",
            payload=payload,
            response={"evidence_units": [{"id": "C1"}]},
            model="test-model",
            api_url="https://example.test/v1",
            response_meta=_provider_meta("fixture-1"),
        )
        response, meta = reusable_stage_fact_response(
            artifact,
            role="creator",
            phase="A",
            payload=payload,
            model="test-model",
            api_url="https://example.test/v1",
        )
        self.assertEqual(response["evidence_units"][0]["id"], "C1")
        self.assertEqual(meta["logical_request_id"], "fixture-1")
        with self.assertRaises(ValueError):
            reusable_stage_fact_response(
                artifact,
                role="creator",
                phase="A",
                payload={**payload, "messages": [{"role": "user", "content": "changed"}]},
                model="test-model",
                api_url="https://example.test/v1",
            )

    def test_stage3_cannot_author_mechanical_fields(self) -> None:
        stage = {
            "stage": "S6",
            "model_gap_magnitude": "large",
            "gap_type": "execution",
            "creator_time_range": "20s - 25s",
            "benchmark_time_range": "18s - 22s",
            "benchmark_evidence_ids": ["B6"],
            "evidence": ["locked evidence"],
        }
        projected = _project_synthesis_improvements(
            [
                {
                    "target_stage": "S6",
                    "title": "合法 prose",
                    "suggestion": "合法建议",
                    "gap_type": "forged",
                    "creator_time_range": "0s - 999s",
                    "benchmark_evidence_ids": ["FAKE"],
                    "priority": 99,
                }
            ],
            [stage],
        )[0]
        self.assertEqual(projected["gap_type"], "execution")
        self.assertEqual(projected["creator_time_range"], "20s - 25s")
        self.assertEqual(projected["benchmark_evidence_ids"], ["B6"])
        self.assertEqual(projected["priority"], 1)

    def test_provider_artifact_keeps_retry_metadata(self) -> None:
        artifact = completed_stage_fact_artifact(
            role="benchmark",
            phase="A",
            payload={"model": "test"},
            response={"evidence_units": []},
            model="test-model",
            api_url="https://example.test/v1",
            response_meta={
                "logical_request_id": "request-1",
                "completion_attempts": 2,
                "retry_reasons": ["invalid JSON"],
                "usage": {"total_tokens": 10},
            },
        )
        self.assertEqual(artifact["response_meta"]["completion_attempts"], 2)
        self.assertEqual(artifact["response_meta"]["retry_reasons"], ["invalid JSON"])
        failed = failed_stage_fact_artifact(
            role="benchmark",
            phase="B",
            payload={"model": "test"},
            model="test-model",
            api_url="https://example.test/v1",
            error="provider timeout",
            response_meta={
                **_provider_meta("request-2"),
                "completion_attempts": 3,
            },
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "provider timeout")
        self.assertEqual(failed["response_meta"]["completion_attempts"], 3)
        with self.assertRaisesRegex(ProviderArtifactError, "missing required fields"):
            completed_stage_fact_artifact(
                role="benchmark",
                phase="A",
                payload={"model": "test"},
                response={"evidence_units": []},
                model="test-model",
                api_url="https://example.test/v1",
                response_meta={"logical_request_id": "incomplete"},
            )

    def test_auxiliary_provider_artifact_replay_is_strict_and_durable(self) -> None:
        payload = {"messages": [{"role": "user", "content": "fixture"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_path = root / "provider_phase_c.json"
            calls = {"count": 0}

            live_meta = _provider_meta("live-request")

            def live_call() -> tuple[dict[str, object], dict[str, object]]:
                calls["count"] += 1
                return {"choices": [{"message": {"content": "{}"}}]}, live_meta

            response, metadata, source = provider_call_with_artifact(
                artifact_path=artifact_path,
                replay_root=None,
                call_kind="phase_c_review",
                payload=payload,
                model="test-model",
                api_url="https://example.test/v1",
                call=live_call,
                response_meta=live_meta,
            )
            self.assertEqual(source, "live")
            self.assertEqual(calls["count"], 1)
            self.assertIn("choices", response)
            self.assertEqual(metadata["completion_attempts"], 1)

            replay_dir = root / "replay"
            replay_dir.mkdir()
            replay_response, replay_metadata, replay_source = provider_call_with_artifact(
                artifact_path=replay_dir / artifact_path.name,
                replay_root=root,
                call_kind="phase_c_review",
                payload=payload,
                model="test-model",
                api_url="https://example.test/v1",
                call=lambda: (_ for _ in ()).throw(AssertionError("replay called provider")),
                response_meta={},
            )
            self.assertEqual(replay_source, "technical_replay")
            self.assertEqual(replay_response, response)
            self.assertEqual(replay_metadata["execution_source"], "technical_replay")
            with self.assertRaises(ValueError):
                provider_call_with_artifact(
                    artifact_path=replay_dir / artifact_path.name,
                    replay_root=root,
                    call_kind="phase_c_review",
                    payload={"changed": True},
                    model="test-model",
                    api_url="https://example.test/v1",
                    call=lambda: (_ for _ in ()).throw(AssertionError("mismatch called provider")),
                    response_meta={},
                )
            mismatch_artifact = json.loads((replay_dir / artifact_path.name).read_text(encoding="utf-8"))
            self.assertEqual(mismatch_artifact["status"], "failed")
            self.assertIn("identity mismatch", mismatch_artifact["error"])

            source_before = artifact_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "output must differ"):
                provider_call_with_artifact(
                    artifact_path=artifact_path,
                    replay_root=root,
                    call_kind="phase_c_review",
                    payload=payload,
                    response_meta={},
                    model="test-model",
                    api_url="https://example.test/v1",
                    call=lambda: (_ for _ in ()).throw(AssertionError("in-place replay called provider")),
                )
            self.assertEqual(artifact_path.read_bytes(), source_before)

    def test_failed_provider_artifact_keeps_failure_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = {"logical_request_id": "req-1", "completion_attempts": 2}
            artifact = Path(directory) / "provider_failed.json"
            with self.assertRaisesRegex(RuntimeError, "provider down"):
                provider_call_with_artifact(
                    artifact_path=artifact,
                    replay_root=None,
                    call_kind="comparison_eligibility",
                    payload={"fixture": True},
                    model="test-model",
                    api_url="https://example.test/v1",
                    response_meta=metadata,
                    call=lambda: (_ for _ in ()).throw(RuntimeError("provider down")),
                )
            saved = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["response_meta"]["logical_request_id"], metadata["logical_request_id"])
            self.assertEqual(saved["response_meta"]["completion_attempts"], metadata["completion_attempts"])
            self.assertEqual(saved["response_meta"]["execution_source"], "live")

            empty_error_artifact = Path(directory) / "provider_failed_empty_error.json"
            with self.assertRaises(RuntimeError):
                provider_call_with_artifact(
                    artifact_path=empty_error_artifact,
                    replay_root=None,
                    call_kind="comparison_eligibility",
                    payload={"fixture": True},
                    model="test-model",
                    api_url="https://example.test/v1",
                    response_meta={},
                    call=lambda: (_ for _ in ()).throw(SystemExit()),
                )
            saved_empty_error = json.loads(empty_error_artifact.read_text(encoding="utf-8"))
            self.assertEqual(saved_empty_error["error"], "SystemExit")

    def test_unknown_severity_stays_unknown(self) -> None:
        self.assertIsNone(normalize_severity(None))
        self.assertIsNone(normalize_severity("not-a-severity"))

    def test_legacy_import_is_audit_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "analysis_result.json"
            source.write_text(
                json.dumps({"stage_analysis": [{"stage": "S1", "severity": "large"}]}),
                encoding="utf-8",
            )
            normalized = _mark_legacy_import_result(
                {"stage_analysis": [{"stage": "S1", "severity": "large"}]},
                source_path=source,
            )
            stage = normalized["stage_analysis"][0]
            self.assertEqual(normalized["analysis_import_mode"], "legacy")
            self.assertEqual(normalized["analysis_run_state"], "degraded")
            self.assertEqual(stage["stage_evidence_gate"]["status"], "legacy")
            self.assertIsNone(stage["severity"])

    def test_legacy_import_accepts_old_minimal_shape_without_current_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "analysis_result.json"
            source.write_text(
                json.dumps({"stage_analysis": [{"stage": "S1", "severity": "large"}]}),
                encoding="utf-8",
            )
            analysis = {"run_dir": str(root), "mode": "compare", "videos": {}}
            merge_analysis_result(analysis, source, "", legacy_import=True)
            self.assertEqual(analysis["analysis_import_mode"], "legacy")
            self.assertEqual(analysis["analysis_run_state"], "degraded")
            self.assertEqual(analysis["stage_analysis"][0]["stage_evidence_gate"]["status"], "legacy")
            self.assertIsNone(analysis["stage_analysis"][0]["severity"])

    def test_legacy_import_rejects_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "analysis_result.json"
            source.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "必须是对象"):
                merge_analysis_result(
                    {"run_dir": directory, "mode": "compare", "videos": {}},
                    source,
                    "",
                    legacy_import=True,
                )

    def test_verification_order_blocks_boundary_until_prerequisites_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(VerificationOrderError):
                assert_verification_order(root, "boundary_sample")
            with mock.patch(
                "flayr_core.verification_order.validate_success_manifest",
                return_value=True,
            ):
                for stage in ("fixture", "offline_replay", "fake_provider", "ordinary_sample"):
                    _run_verifier(root, stage)
                assert_verification_order(root, "boundary_sample")
                (root / "ordinary_sample.evidence.txt").write_text("tampered", encoding="utf-8")
                with self.assertRaises(VerificationOrderError):
                    assert_verification_order(root, "boundary_sample")
                _run_verifier(root, "ordinary_sample", content="passed-again")
                assert_verification_order(root, "boundary_sample")

    def test_ordinary_marker_requires_semantically_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage in ("fixture", "offline_replay", "fake_provider"):
                _run_verifier(root, stage)
            evidence = root / "ordinary.evidence.txt"
            command = [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                f"Path({str(evidence)!r}).write_text('process exited zero', encoding='utf-8')",
            ]
            with self.assertRaisesRegex(VerificationOrderError, "requires --run-dir"):
                run_verification_stage(
                    root,
                    "ordinary_sample",
                    command=command,
                    evidence_paths=[evidence],
                    repo_root=ROOT,
                )
            degraded_run = root / "degraded-run"
            degraded_run.mkdir()
            (degraded_run / "analysis.json").write_text(
                json.dumps({"analysis_run_state": "degraded"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(VerificationOrderError, "run is not completed"):
                run_verification_stage(
                    root,
                    "ordinary_sample",
                    command=command,
                    evidence_paths=[evidence],
                    completed_run_dir=degraded_run,
                    repo_root=ROOT,
                )

    def test_ordinary_marker_rejects_unrelated_preexisting_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage in ("fixture", "offline_replay", "fake_provider"):
                _run_verifier(root, stage)
            completed_run = root / "old-completed-run"
            completed_run.mkdir()
            (completed_run / "_SUCCESS.json").write_text("{}\n", encoding="utf-8")
            evidence = root / "ordinary.evidence.txt"
            with mock.patch(
                "flayr_core.verification_order.validate_success_manifest",
                return_value=True,
            ), self.assertRaisesRegex(VerificationOrderError, "not produced by this command"):
                run_verification_stage(
                    root,
                    "ordinary_sample",
                    command=[
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        f"Path({str(evidence)!r}).write_text('unrelated command', encoding='utf-8')",
                    ],
                    evidence_paths=[evidence],
                    completed_run_dir=completed_run,
                    repo_root=ROOT,
                )

    def test_ordinary_marker_accepts_new_real_success_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage in ("fixture", "offline_replay", "fake_provider"):
                _run_verifier(root, stage)
            completed_run = root / "completed-run"
            evidence = root / "ordinary.evidence.txt"
            code_commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                text=True,
            ).strip().lower()
            marker = run_verification_stage(
                root,
                "ordinary_sample",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "from tests.test_bitter_lesson_contract import _write_completed_run; "
                    f"Path({str(evidence)!r}).write_text('semantic completion\\n', encoding='utf-8'); "
                    f"_write_completed_run(Path({str(completed_run)!r}), {code_commit!r})",
                ],
                evidence_paths=[evidence],
                completed_run_dir=completed_run,
                repo_root=ROOT,
            )
            self.assertTrue(marker.is_file())
            assert_verification_order(root, "boundary_sample", repo_root=ROOT)

    def test_verification_marker_is_stale_after_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            root = base / "verification"
            repo.mkdir()
            source = repo / "source.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Flayr Test",
                    "-c",
                    "user.email=flayr@example.test",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            evidence = root / "fixture.txt"
            run_verification_stage(
                root,
                "fixture",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    f"Path({str(evidence)!r}).write_text('passed', encoding='utf-8')",
                ],
                evidence_paths=[evidence],
                repo_root=repo,
            )
            source.write_text("VALUE = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(VerificationOrderError, "stale for current source"):
                assert_verification_order(root, "offline_replay", repo_root=repo)

    def test_runtime_field_ownership_gate_rejects_unauthorized_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "scripts" / "flayr_core" / "report.py"
            path.parent.mkdir(parents=True)
            path.write_text("result['severity'] = 'large'\n", encoding="utf-8")
            violations = ownership_violations(inventory(root, ("severity",)))
            self.assertTrue(any("unauthorized production writer" in item for item in violations))

    def test_pipeline_failure_preserves_phase_and_kind(self) -> None:
        with self.assertRaises(Exception) as caught:
            pipeline_module._run_pipeline_phase(
                "finalization",
                "finalizer",
                lambda: (_ for _ in ()).throw(ValueError("invalid canonical result")),
            )
        error = caught.exception
        self.assertEqual(getattr(error, "phase", None), "finalization")
        self.assertEqual(getattr(error, "failure_kind", None), "finalizer")

        with self.assertRaises(Exception) as parse_caught:
            pipeline_module._run_pipeline_phase(
                "stage2_judgment",
                "judgment",
                lambda: (_ for _ in ()).throw(
                    pipeline_module.ResponseParseError("invalid provider JSON")
                ),
            )
        self.assertEqual(parse_caught.exception.failure_kind, "response_parse")

    def test_default_text_entrypoint_is_rejected(self) -> None:
        args = argparse.Namespace(llm_include_images=False, llm_dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("flayr_core.llm.pipeline.read_llm_api_key", return_value=""):
                with self.assertRaisesRegex(SystemExit, "text-only LLM"):
                    run_large_model_analysis(
                        args,
                        {},
                        Path(tmp) / "analysis_input.md",
                        Path(tmp),
                    )

    def test_change_scope_rejects_unrelated_paths(self) -> None:
        spec = load_spec()
        with mock.patch("scripts.check_change_scope._changed_paths", return_value={"scripts/flayr.py"}):
            with mock.patch("scripts.check_change_scope._line_counts", return_value=(1, 0)):
                issues = check_scope(spec, "HEAD")
        self.assertTrue(any("forbidden path" in issue for issue in issues))

    def test_zero_base_scope_compares_complete_tree(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_git(*args: str) -> str:
            calls.append(args)
            return ""

        with mock.patch("scripts.check_change_scope._git", side_effect=fake_git):
            _changed_paths("0" * 40)
        self.assertEqual(calls[0][:4], ("diff", "--name-only", "--no-renames", EMPTY_TREE_SHA))
        self.assertEqual(calls[0][4:], ("HEAD", "--"))

    def test_full_provider_replay_can_run_without_live_key(self) -> None:
        args = argparse.Namespace(
            provider_replay_from=Path("/replay/auxiliary"),
            stage1_replay_from=Path("/replay/stage1"),
            stage2_replay_from=Path("/replay/stage2"),
        )
        self.assertTrue(_full_provider_replay_requested(args))
        args.stage2_replay_from = None
        self.assertFalse(_full_provider_replay_requested(args))


if __name__ == "__main__":
    unittest.main()
