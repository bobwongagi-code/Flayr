from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.batch_analyze import _run_jobs, _success_manifest_valid, acquire_lock, build_command, validate_spec
from scripts.flayr_core.run_manifest import write_success_manifest, validate_success_manifest
from scripts.flayr_core.run_manifest import command_digest
from scripts.flayr_core.run_state import (
    ANALYSIS_COMPLETED,
    COMPLETED,
    PROCESSING,
    REPORT_GENERATING,
    initialize_run_state,
    transition_run_state,
)


class BatchAnalyzeValidationTests(unittest.TestCase):
    def _job(self, name: str = "sample") -> dict[str, object]:
        return {"name": name, "creator": "/tmp/creator.mp4", "benchmark": "/tmp/benchmark.mp4"}

    @staticmethod
    def _audience_report(template_version: str, report_schema_version: int = 2) -> str:
        payload = {
            "metadata": {
                "report_schema_version": report_schema_version,
                "template_version": template_version,
                "generated_by": "test-commit",
                "generator": "tests",
            }
        }
        return f"<html><script>var report = {json.dumps(payload, separators=(',', ':'))};\n</script></html>"

    def test_rejects_duplicate_names_and_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "重复 job name"):
                validate_spec({"jobs": [self._job(), self._job()]}, runs_dir, 1)

            first = self._job("first")
            second = self._job("second")
            first["output_dir"] = str(runs_dir / "same")
            second["output_dir"] = str(runs_dir / "same")
            with self.assertRaisesRegex(ValueError, "同一 output_dir"):
                validate_spec({"jobs": [first, second]}, runs_dir, 1)

    def test_rejects_output_dir_outside_runs_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            outside = Path(tmp) / "outside"
            job = self._job()
            job["output_dir"] = str(outside)
            with self.assertRaisesRegex(ValueError, "必须位于 runs 根目录内"):
                validate_spec({"jobs": [job]}, runs_dir, 1)

    def test_rejects_unsafe_names_and_runner_owned_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "name 非法"):
                validate_spec({"jobs": [self._job("../escape")]}, runs_dir, 1)
            with self.assertRaisesRegex(ValueError, "不得覆盖 runner 参数"):
                validate_spec(
                    {"common_args": ["--output-dir=/tmp/override"], "jobs": [self._job()]},
                    runs_dir,
                    1,
                )
            with self.assertRaisesRegex(ValueError, "不得覆盖 runner 参数"):
                validate_spec(
                    {"common_args": ["--llm-api-u", "https://attacker.invalid"], "jobs": [self._job()]},
                    runs_dir,
                    1,
                )

    def test_rejects_non_positive_concurrency(self) -> None:
        with self.assertRaisesRegex(ValueError, "concurrency"):
            validate_spec({"jobs": [self._job()]}, Path("/tmp/runs"), 0)

    def test_lock_creation_is_atomic_and_rejects_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "runner.lock"
            acquire_lock(lock_path)
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["schema_version"], 1)
            self.assertEqual(lock["pid"], os.getpid())
            self.assertIn("process_start", lock)
            self.assertIn("host", lock)
            self.assertIn("boot_id", lock)
            with self.assertRaisesRegex(RuntimeError, "已有 runner"):
                acquire_lock(lock_path)

    def test_corrupt_lock_is_not_deleted_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "runner.lock"
            lock_path.write_text("partial lock", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "无法读取身份"):
                acquire_lock(lock_path)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "partial lock")

    def test_launch_failure_closes_log_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "_batch" / "status.json"
            status_path.parent.mkdir()
            opened = mock.mock_open()
            with mock.patch("builtins.open", opened), mock.patch(
                "scripts.batch_analyze.subprocess.Popen", side_effect=OSError("launch failed")
            ):
                with self.assertRaisesRegex(OSError, "launch failed"):
                    _run_jobs([self._job()], [], root, status_path, 1)
            opened().close.assert_called_once()

    def test_only_valid_success_manifest_counts_as_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            creator = root / "creator.mp4"
            benchmark = root / "benchmark.mp4"
            creator.write_bytes(b"creator")
            benchmark.write_bytes(b"benchmark")
            out = root / "run"
            out.mkdir()
            initialize_run_state(out)
            transition_run_state(out, PROCESSING)
            transition_run_state(out, ANALYSIS_COMPLETED)
            transition_run_state(out, REPORT_GENERATING)
            transition_run_state(out, COMPLETED)
            (out / "analysis.json").write_text(
                '{"analysis_run_state":"completed"}', encoding="utf-8"
            )
            (out / "report.html").write_text("<html></html>", encoding="utf-8")
            for artifact in (
                "raw_model_response.json",
                "validated_normalized_result.json",
                "postprocess_change_log.json",
            ):
                (out / artifact).write_text("{}", encoding="utf-8")
            (out / "final_derived_result.json").write_text(
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
            job = {"name": "sample", "creator": str(creator), "benchmark": str(benchmark)}
            self.assertFalse(validate_success_manifest(out, {"creator_video": creator, "benchmark_video": benchmark}))
            (out / "final_derived_result.json").write_text(
                json.dumps(
                    {
                        "postprocess_provenance": {
                            "field_sources": {
                                "coverage": "partial",
                                "unresolved_paths": ["/stage_analysis/0/severity"],
                                "truncated": False,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "complete field source coverage"):
                write_success_manifest(
                    out,
                    {"creator_video": creator, "benchmark_video": benchmark},
                    {"analysis_run_state": "completed"},
                )
            (out / "final_derived_result.json").write_text(
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
                out,
                {"creator_video": creator, "benchmark_video": benchmark},
                {"analysis_run_state": "completed"},
                {"argv_sha256": command_digest(build_command(job, out, [])[2:])},
            )
            self.assertTrue(validate_success_manifest(out, {"creator_video": creator, "benchmark_video": benchmark}))
            self.assertTrue(_success_manifest_valid(job, out, []))
            lifecycle = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
            lifecycle["state"] = REPORT_GENERATING
            (out / "run_state.json").write_text(json.dumps(lifecycle), encoding="utf-8")
            self.assertFalse(validate_success_manifest(out, {"creator_video": creator, "benchmark_video": benchmark}))
            lifecycle["state"] = COMPLETED
            (out / "run_state.json").write_text(json.dumps(lifecycle), encoding="utf-8")
            creator.write_bytes(b"changed")
            self.assertFalse(validate_success_manifest(out, {"creator_video": creator, "benchmark_video": benchmark}))

    def test_nonzero_child_with_old_analysis_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = self._job()
            out = root / "sample-sample"
            out.mkdir()
            (root / "_batch").mkdir()
            (out / "analysis.json").write_text('{"analysis_run_state":"completed"}', encoding="utf-8")

            class FailedProcess:
                returncode = 7

                def poll(self) -> int:
                    return self.returncode

            with (
                mock.patch("scripts.batch_analyze.subprocess.Popen", return_value=FailedProcess()),
                mock.patch("scripts.batch_analyze.time.sleep"),
            ):
                result = _run_jobs([job], [], root, root / "_batch" / "status.json", 1)
            self.assertEqual(result, 1)
            status = json.loads((root / "_batch" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["jobs"]["sample"]["state"], "failed")

    def test_improve_success_manifest_requires_both_report_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            creator = root / "creator.mp4"
            benchmark = root / "benchmark.mp4"
            creator.write_bytes(b"creator")
            benchmark.write_bytes(b"benchmark")
            out = root / "run"
            out.mkdir()
            initialize_run_state(out)
            transition_run_state(out, PROCESSING)
            transition_run_state(out, ANALYSIS_COMPLETED)
            transition_run_state(out, REPORT_GENERATING)
            transition_run_state(out, COMPLETED)
            (out / "analysis.json").write_text(
                '{"analysis_run_state":"completed","mode":"improve"}', encoding="utf-8"
            )
            for artifact in (
                "report.html",
                "raw_model_response.json",
                "validated_normalized_result.json",
                "postprocess_change_log.json",
            ):
                (out / artifact).write_text("{}", encoding="utf-8")
            (out / "final_derived_result.json").write_text(
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
            inputs = {"creator_video": creator, "benchmark_video": benchmark}
            analysis = {"analysis_run_state": "completed", "mode": "improve"}
            with self.assertRaises(FileNotFoundError):
                write_success_manifest(out, inputs, analysis)

            (out / "bd_report.html").write_text(self._audience_report("bd-internal-v2"), encoding="utf-8")
            (out / "creator_report.html").write_text(self._audience_report("creator-v2"), encoding="utf-8")
            write_success_manifest(out, inputs, analysis, {"mode": "improve"})
            self.assertTrue(validate_success_manifest(out, inputs))
            manifest = json.loads((out / "_SUCCESS.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(
                manifest["report_metadata"]["bd_report.html"]["template_version"],
                "bd-internal-v2",
            )

            (out / "bd_report.html").write_text(
                self._audience_report("bd-internal-v2", report_schema_version=1),
                encoding="utf-8",
            )
            self.assertFalse(validate_success_manifest(out, inputs))
            (out / "bd_report.html").write_text(
                self._audience_report("bd-internal-v2"), encoding="utf-8"
            )

            legacy_manifest = dict(manifest)
            legacy_manifest["schema_version"] = 2
            legacy_manifest.pop("report_metadata", None)
            (out / "_SUCCESS.json").write_text(
                json.dumps(legacy_manifest), encoding="utf-8"
            )
            self.assertFalse(validate_success_manifest(out, inputs))

            write_success_manifest(out, inputs, analysis, {"mode": "improve"})
            (out / "bd_report.html").write_text(self._audience_report("bd-internal-v3"), encoding="utf-8")
            self.assertFalse(validate_success_manifest(out, inputs))

            (out / "creator_report.html").unlink()
            self.assertFalse(validate_success_manifest(out, inputs))


if __name__ == "__main__":
    unittest.main()
