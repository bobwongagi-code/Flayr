from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

from scripts.web_app import (
    FlayrServer,
    JobStore,
    _signed_client_cookie,
    parse_multipart,
    progress_for_run,
    safe_asset_path,
)
from scripts.flayr_core.run_state import (
    ANALYSIS_COMPLETED,
    DEGRADED,
    PROCESSING,
    REPORT_GENERATING,
    initialize_run_state,
    read_run_state,
    transition_run_state,
)


class WebAppHelpersTests(unittest.TestCase):
    def test_parse_multipart_keeps_uploads_in_temp_files(self) -> None:
        boundary = b"----FlayrTestBoundary"
        body = (
            b"--" + boundary + b"\r\n"
            b"Content-Disposition: form-data; name=\"product_name\"\r\n\r\n"
            + "儿童牙膏".encode("utf-8")
            + b"\r\n"
            b"--" + boundary + b"\r\n"
            b"Content-Disposition: form-data; name=\"benchmark_video\"; filename=\"benchmark.mp4\"\r\n"
            b"Content-Type: video/mp4\r\n\r\n"
            b"benchmark-bytes\r\n"
            b"--" + boundary + b"\r\n"
            b"Content-Disposition: form-data; name=\"creator_video\"; filename=\"creator.mp4\"\r\n"
            b"Content-Type: video/mp4\r\n\r\n"
            b"creator-bytes\r\n"
            b"--" + boundary + b"--\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "body"
            body_path.write_bytes(body)
            fields, files = parse_multipart(body_path, f'multipart/form-data; boundary="{boundary.decode()}"')
            self.assertEqual(fields["product_name"], "儿童牙膏")
            self.assertEqual(Path(files["benchmark_video"]["path"]).read_bytes(), b"benchmark-bytes")
            self.assertEqual(Path(files["creator_video"]["path"]).read_bytes(), b"creator-bytes")
            for item in files.values():
                Path(item["path"]).unlink(missing_ok=True)

    def test_safe_asset_path_rejects_traversal_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            root.mkdir()
            (root / "frames").mkdir()
            (root / "frames" / "one.jpg").write_bytes(b"image")
            outside = Path(tmp) / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            link = root / "frames" / "link.txt"
            try:
                link.symlink_to(outside)
            except OSError:
                link = None
            self.assertEqual(safe_asset_path(root, "frames/one.jpg"), (root / "frames/one.jpg").resolve())
            self.assertIsNone(safe_asset_path(root, "../outside.txt"))
            self.assertIsNone(safe_asset_path(root, "/etc/passwd"))
            if link is not None:
                self.assertIsNone(safe_asset_path(root, "frames/link.txt"))

    def test_progress_exposes_only_coarse_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.assertEqual(progress_for_run(run_dir), (0, "素材处理与转写"))
            (run_dir / "raw_model_response.json").write_text("{}", encoding="utf-8")
            self.assertEqual(progress_for_run(run_dir), (72, "模型对比分析"))
            (run_dir / "analysis.json").write_text(
                '{"analysis_run_state":"completed"}', encoding="utf-8"
            )
            (run_dir / "postprocess_change_log.json").write_text("[]", encoding="utf-8")
            (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(progress_for_run(run_dir), (92, "报告生成"))
            (run_dir / "_SUCCESS.json").write_text("{}", encoding="utf-8")
            self.assertEqual(progress_for_run(run_dir), (100, "报告生成"))

    def test_degraded_progress_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            initialize_run_state(run_dir)
            transition_run_state(run_dir, PROCESSING)
            transition_run_state(run_dir, ANALYSIS_COMPLETED)
            transition_run_state(run_dir, REPORT_GENERATING)
            transition_run_state(run_dir, DEGRADED)
            self.assertEqual(progress_for_run(run_dir), (100, "报告生成（部分分析能力降级）"))

    def test_job_store_does_not_expose_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            benchmark = Path(tmp) / "benchmark.mp4"
            creator = Path(tmp) / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            files = {
                "benchmark_video": {"path": benchmark, "filename": "benchmark.mp4"},
                "creator_video": {"path": creator, "filename": "creator.mp4"},
            }
            with mock.patch.object(store._executor, "submit"):
                public = store.create(
                    {"product_name": "儿童牙膏", "market": "马来西亚"},
                    files,
                )
            self.assertEqual(public["market"], "马来西亚")
            self.assertEqual(public["report_url"], "")
            self.assertEqual(public["creator_report_url"], "")
            self.assertNotIn("run_dir", public)
            self.assertNotIn("benchmark_path", public)
            store.shutdown()

    def test_public_report_urls_follow_existing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            job = {
                "id": "job-1",
                "status": "completed",
                "run_dir": str(run_dir),
                "product_name": "测试产品",
                "market": "马来西亚",
                "created_at": "",
            }

            public = store.public(job)
            self.assertEqual(public["report_url"], "")
            self.assertEqual(public["bd_report_url"], "")
            self.assertEqual(public["creator_report_url"], "")

            (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")
            public = store.public(job)
            self.assertEqual(public["report_url"], "/api/workspaces/local/jobs/job-1/report")
            self.assertEqual(public["bd_report_url"], "")
            self.assertEqual(public["creator_report_url"], "")
            self.assertEqual(public["report_kind"], "legacy")

            (run_dir / "bd_report.html").write_text("<html></html>", encoding="utf-8")
            public = store.public(job)
            self.assertEqual(public["report_url"], "/api/workspaces/local/jobs/job-1/report")
            self.assertEqual(public["bd_report_url"], "/api/workspaces/local/jobs/job-1/report")
            self.assertEqual(public["creator_report_url"], "")
            self.assertEqual(public["report_kind"], "audience")

            (run_dir / "bd_report.html").write_text("not html", encoding="utf-8")
            public = store.public(job)
            self.assertEqual(public["report_url"], "/api/workspaces/local/jobs/job-1/report")
            self.assertEqual(public["bd_report_url"], "")
            self.assertEqual(public["report_kind"], "legacy")

            (run_dir / "bd_report.html").write_text("<html></html>", encoding="utf-8")
            (run_dir / "creator_report.html").write_text("<html></html>", encoding="utf-8")
            public = store.public(job)
            self.assertEqual(public["report_url"], "/api/workspaces/local/jobs/job-1/report")
            self.assertEqual(public["bd_report_url"], "/api/workspaces/local/jobs/job-1/report")
            self.assertEqual(public["creator_report_url"], "/api/workspaces/local/jobs/job-1/creator-report")
            self.assertEqual(public["report_kind"], "audience")
            store.shutdown()

    def test_job_store_scopes_jobs_by_owner_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp), workspace_id="workspace-a")
            benchmark = Path(tmp) / "benchmark.mp4"
            creator = Path(tmp) / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            files = {
                "benchmark_video": {"path": benchmark, "filename": "benchmark.mp4"},
                "creator_video": {"path": creator, "filename": "creator.mp4"},
            }
            with mock.patch.object(store._executor, "submit"):
                public = store.create(
                    {"product_name": "测试产品"},
                    files,
                    owner_id="owner-a",
                )
            job_id = str(public["id"])
            self.assertEqual(public["workspace_id"], "workspace-a")
            self.assertEqual(public["job_url"], f"/api/workspaces/workspace-a/jobs/{job_id}")
            self.assertEqual(
                store.get(job_id, owner_id="owner-a", workspace_id="workspace-a")["id"],
                job_id,
            )
            self.assertIsNone(store.get(job_id, owner_id="owner-b", workspace_id="workspace-a"))
            self.assertIsNone(store.get(job_id, owner_id="owner-a", workspace_id="workspace-b"))
            self.assertEqual(store.all(owner_id="owner-b", workspace_id="workspace-a"), [])
            store.jobs["legacy-job"] = {
                "id": "legacy-job",
                "workspace_id": "workspace-a",
                "run_dir": str(Path(tmp) / "legacy-run"),
            }
            self.assertIsNone(store.get("legacy-job", owner_id="owner-a", workspace_id="workspace-a"))
            store.shutdown()

    def test_http_report_requires_matching_browser_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root)
            run_dir = root / "job-run"
            run_dir.mkdir()
            (run_dir / "bd_report.html").write_text("<html>private report</html>", encoding="utf-8")
            with store._lock:
                store.jobs["job-1"] = {
                    "id": "job-1",
                    "owner_id": "owner-a",
                    "workspace_id": "local",
                    "visibility": "private",
                    "status": "completed",
                    "run_dir": str(run_dir),
                    "product_name": "测试产品",
                    "market": "马来西亚",
                    "created_at": "",
                }
                store._persist_locked()
            server = FlayrServer(("127.0.0.1", 0), store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/api/workspaces/local/jobs/job-1/report"
            try:
                signed_owner_a = _signed_client_cookie("owner-a", server.client_cookie_secret)
                signed_owner_b = _signed_client_cookie("owner-b", server.client_cookie_secret)
                response = urlopen(Request(url, headers={"Cookie": f"flayr_client_id={signed_owner_a}"}))
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read().decode("utf-8"), "<html>private report</html>")
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, headers={"Cookie": f"flayr_client_id={signed_owner_b}"}))
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, headers={"Cookie": "flayr_client_id=owner-a"}))
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, headers={"Cookie": f"flayr_client_id={signed_owner_a[:-1]}0"}))
                self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.shutdown()

    def test_browser_identity_secret_survives_server_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_store = JobStore(root)
            first_server = FlayrServer(("127.0.0.1", 0), first_store)
            first_secret = first_server.client_cookie_secret
            first_server.server_close()
            first_store.shutdown()

            second_store = JobStore(root)
            second_server = FlayrServer(("127.0.0.1", 0), second_store)
            try:
                self.assertEqual(second_server.client_cookie_secret, first_secret)
                self.assertEqual(
                    _signed_client_cookie("owner-a", second_server.client_cookie_secret),
                    _signed_client_cookie("owner-a", first_secret),
                )
            finally:
                second_server.server_close()
                second_store.shutdown()

    def test_http_asset_rejects_extension_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root)
            run_dir = root / "job-run"
            run_dir.mkdir()
            (run_dir / "frame.png").write_bytes(b"not a png")
            with store._lock:
                store.jobs["job-1"] = {
                    "id": "job-1",
                    "owner_id": "owner-a",
                    "workspace_id": "local",
                    "visibility": "private",
                    "status": "completed",
                    "run_dir": str(run_dir),
                    "product_name": "测试产品",
                    "market": "马来西亚",
                    "created_at": "",
                }
                store._persist_locked()
            server = FlayrServer(("127.0.0.1", 0), store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/api/workspaces/local/jobs/job-1/assets/frame.png"
            try:
                signed_owner_a = _signed_client_cookie("owner-a", server.client_cookie_secret)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, headers={"Cookie": f"flayr_client_id={signed_owner_a}"}))
                self.assertEqual(error.exception.code, 415)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.shutdown()

    def test_http_serves_split_frontend_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            server = FlayrServer(("127.0.0.1", 0), store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                for path, marker in (
                    ("/styles.css", ".app"),
                    ("/app.js", "./components/audience-switch.js"),
                    ("/components/report-view.js", "reportUrlForAudience"),
                ):
                    response = urlopen(base + path)
                    self.assertEqual(response.status, 200)
                    self.assertIn(marker, response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.shutdown()

    def test_public_projection_handles_one_hundred_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            with store._lock:
                for index in range(100):
                    store.jobs[f"job-{index}"] = {
                        "id": f"job-{index}",
                        "owner_id": "owner-a",
                        "workspace_id": "local",
                        "visibility": "private",
                        "status": "completed",
                        "run_dir": str(Path(tmp) / f"run-{index}"),
                        "product_name": f"产品-{index}",
                        "market": "马来西亚",
                        "created_at": "",
                    }
                store._persist_locked()
            public_jobs = store.all(owner_id="owner-a", workspace_id="local")
            self.assertEqual(len(public_jobs), 100)
            projections = [store.public(job) for job in public_jobs]
            self.assertEqual(len(projections), 100)
            self.assertTrue(all("run_dir" not in item for item in projections))
            store.shutdown()

    def test_restart_recovery_closes_incomplete_job_with_failed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            files = {
                "benchmark_video": {"path": benchmark, "filename": "benchmark.mp4"},
                "creator_video": {"path": creator, "filename": "creator.mp4"},
            }
            with mock.patch.object(store._executor, "submit"):
                public = store.create({"product_name": "测试"}, files, owner_id="owner-a")
            job_id = str(public["id"])
            run_dir = Path(str(store.jobs[job_id]["run_dir"]))
            transition_run_state(run_dir, PROCESSING)
            store.jobs[job_id]["status"] = "running"
            store._persist_locked()
            store.shutdown()

            recovered_store = JobStore(root)
            recovered = recovered_store.get(job_id)
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(read_run_state(run_dir)["state"], "FAILED")
            self.assertIn("服务重新启动", recovered["failure_reason"])
            recovered_store.shutdown()

    def test_failed_job_clears_estimated_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            benchmark = Path(tmp) / "benchmark.mp4"
            creator = Path(tmp) / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            files = {
                "benchmark_video": {"path": benchmark, "filename": "benchmark.mp4"},
                "creator_video": {"path": creator, "filename": "creator.mp4"},
            }
            with mock.patch.object(store._executor, "submit"):
                public = store.create({"product_name": "测试"}, files)
            store._finish(str(public["id"]), 1)
            failed = store.public(store.get(str(public["id"])) or {})
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["estimated_remaining_seconds"], 0)
            store.shutdown()


if __name__ == "__main__":
    unittest.main()
