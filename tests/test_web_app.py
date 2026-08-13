from __future__ import annotations

import os
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from unittest import mock

from scripts.web_app import (
    FlayrServer,
    JobStore,
    JOB_RETENTION_SECONDS,
    MIN_FREE_SPACE_BYTES,
    UPLOAD_STAGING_TTL_SECONDS,
    UPLOAD_IDLE_TIMEOUT_SECONDS,
    UPLOAD_TOTAL_TIMEOUT_SECONDS,
    SubmissionRateLimiter,
    WEB_ALLOWED_HOSTS_ENV,
    WEB_AUTH_TOKEN_ENV,
    _resolve_web_security,
    _signed_client_cookie,
    cleanup_upload_files,
    estimated_remaining_seconds,
    parse_multipart,
    progress_for_run,
    safe_asset_path,
    utc_now,
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
    def test_web_security_requires_explicit_public_mode(self) -> None:
        with self.assertRaises(ValueError):
            _resolve_web_security("192.168.1.20", False)
        self.assertEqual(_resolve_web_security("127.0.0.1", False), (False, "", set()))

        with mock.patch.dict(
            os.environ,
            {
                WEB_AUTH_TOKEN_ENV: "t" * 32,
                WEB_ALLOWED_HOSTS_ENV: "reports.example.test:8443",
            },
        ):
            public_mode, token, allowed_hosts = _resolve_web_security("0.0.0.0", True)
        self.assertTrue(public_mode)
        self.assertEqual(token, "t" * 32)
        self.assertEqual(allowed_hosts, {("reports.example.test", 8443)})

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(WEB_AUTH_TOKEN_ENV, None)
            os.environ.pop(WEB_ALLOWED_HOSTS_ENV, None)
            with self.assertRaises(ValueError):
                _resolve_web_security("0.0.0.0", True)

    def test_public_web_requires_bearer_host_and_origin_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            server = FlayrServer(
                ("127.0.0.1", 0),
                store,
                public_mode=True,
                auth_token="t" * 32,
                allowed_hosts={("example.test", None)},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/api/jobs"
            open_direct = build_opener(ProxyHandler({})).open
            try:
                with self.assertRaises(HTTPError) as error:
                    open_direct(Request(url, headers={"Host": "example.test"}))
                self.assertEqual(error.exception.code, 401)

                with self.assertRaises(HTTPError) as error:
                    open_direct(
                        Request(
                            url,
                            headers={
                                "Host": "wrong.example.test",
                                "Authorization": "Bearer " + "t" * 32,
                            },
                        )
                    )
                self.assertEqual(error.exception.code, 403)

                response = open_direct(
                    Request(
                        url,
                        headers={
                            "Host": "example.test",
                            "Authorization": "Bearer " + "t" * 32,
                        },
                    )
                )
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload, {"jobs": [], "recovery_warning": ""})

                with self.assertRaises(HTTPError) as error:
                    open_direct(
                        Request(
                            url,
                            data=b"",
                            headers={
                                "Host": "example.test",
                                "Authorization": "Bearer " + "t" * 32,
                                "Content-Type": "application/json",
                            },
                        )
                    )
                self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.shutdown()

    def test_submission_limiter_applies_to_each_owner_and_ip_key(self) -> None:
        limiter = SubmissionRateLimiter()
        keys = ("owner:one", "ip:127.0.0.1")
        self.assertTrue(limiter.admit(keys, limit=3, window_seconds=60))
        self.assertTrue(limiter.admit(keys, limit=3, window_seconds=60))
        self.assertTrue(limiter.admit(keys, limit=3, window_seconds=60))
        self.assertFalse(limiter.admit(keys, limit=3, window_seconds=60))
        self.assertTrue(limiter.admit(("owner:two", "ip:127.0.0.2"), limit=3, window_seconds=60))

    def test_public_admission_limits_cover_queue_owner_and_daily_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            today = utc_now()
            try:
                with store._lock:
                    for index in range(4):
                        store.jobs[f"active-{index}"] = {
                            "id": f"active-{index}",
                            "workspace_id": "local",
                            "owner_id": f"owner-{index}",
                            "status": "queued",
                            "created_at": today,
                        }
                self.assertIn("队列已满", store.admission_error(owner_id="owner-a", workspace_id="local"))

                with store._lock:
                    store.jobs.clear()
                    for index in range(2):
                        store.jobs[f"owner-active-{index}"] = {
                            "id": f"owner-active-{index}",
                            "workspace_id": "local",
                            "owner_id": "owner-a",
                            "status": "running",
                            "created_at": today,
                        }
                self.assertIn("已有任务", store.admission_error(owner_id="owner-a", workspace_id="local"))

                with store._lock:
                    store.jobs.clear()
                    for index in range(20):
                        store.jobs[f"daily-{index}"] = {
                            "id": f"daily-{index}",
                            "workspace_id": "local",
                            "owner_id": "owner-a",
                            "status": "completed",
                            "created_at": today,
                        }
                self.assertIn("今日任务额度", store.admission_error(owner_id="owner-a", workspace_id="local"))
            finally:
                store.shutdown()

    def test_duplicate_multipart_fields_are_rejected_without_staging_leaks(self) -> None:
        boundary = b"----DuplicateBoundary"
        body = (
            b"--" + boundary + b"\r\n"
            b"Content-Disposition: form-data; name=\"creator_video\"; filename=\"one.mp4\"\r\n"
            b"Content-Type: video/mp4\r\n\r\n"
            b"first\r\n"
            b"--" + boundary + b"\r\n"
            b"Content-Disposition: form-data; name=\"creator_video\"; filename=\"two.mp4\"\r\n"
            b"Content-Type: video/mp4\r\n\r\n"
            b"second\r\n"
            b"--" + boundary + b"--\r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body_path = root / "body"
            body_path.write_bytes(body)
            with mock.patch("scripts.web_app.WEB_ROOT", root):
                with self.assertRaisesRegex(Exception, "multipart 字段重复"):
                    parse_multipart(body_path, f"multipart/form-data; boundary={boundary.decode()}")
            self.assertEqual(list(root.glob(".upload-part-*")), [])

    def test_cleanup_upload_files_removes_unadopted_and_unknown_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / ".upload-part-one"
            second = Path(tmp) / ".upload-part-two"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            cleanup_upload_files({
                "creator_video": {"path": first},
                "unexpected": {"path": second},
            })
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_storage_reservation_enforces_quota_and_free_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            try:
                with (
                    mock.patch("scripts.web_app._directory_size", return_value=0),
                    mock.patch("scripts.web_app.shutil.disk_usage", return_value=mock.Mock(free=MIN_FREE_SPACE_BYTES)),
                ):
                    with self.assertRaisesRegex(Exception, "可用磁盘空间不足"):
                        store.reserve_upload("local", 1)
                with (
                    mock.patch("scripts.web_app._directory_size", return_value=10 * 1024 * 1024 * 1024),
                    mock.patch("scripts.web_app.shutil.disk_usage", return_value=mock.Mock(free=10**15)),
                ):
                    with self.assertRaisesRegex(Exception, "工作区存储空间"):
                        store.reserve_upload("local", 1)
                with (
                    mock.patch("scripts.web_app._directory_size", return_value=0),
                    mock.patch("scripts.web_app.shutil.disk_usage", return_value=mock.Mock(free=10**15)),
                ):
                    store.reserve_upload("local", 128)
                    self.assertEqual(store._storage_reservations["local"], 128)
                    store.release_upload("local", 128)
                    self.assertNotIn("local", store._storage_reservations)
            finally:
                store.shutdown()

    def test_startup_gc_removes_old_staging_and_expired_terminal_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_upload = root / ".upload-part-old"
            stale_upload.write_bytes(b"orphan")
            old_time = time.time() - UPLOAD_STAGING_TTL_SECONDS - 1
            os.utime(stale_upload, (old_time, old_time))
            job_root = root / "jobs" / "job-old"
            (job_root / "run").mkdir(parents=True)
            (job_root / "run" / "report.html").write_text("<html></html>", encoding="utf-8")
            old_created_at = "2000-01-01T00:00:00+00:00"
            (root / "jobs.json").write_text(
                json.dumps({
                    "job-old": {
                        "id": "job-old",
                        "workspace_id": "local",
                        "owner_id": "owner-a",
                        "status": "completed",
                        "created_at": old_created_at,
                        "run_dir": str(job_root / "run"),
                    }
                }),
                encoding="utf-8",
            )
            store = JobStore(root)
            try:
                self.assertFalse(stale_upload.exists())
                self.assertIsNone(store.get("job-old"))
                self.assertFalse(job_root.exists())
            finally:
                store.shutdown()

    def test_corrupt_job_index_is_quarantined_and_rebuilt(self) -> None:
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
                public = store.create({"product_name": "恢复测试"}, files, owner_id="owner-a")
            job_id = str(public["id"])
            with store._lock:
                store._persist_locked()
            backup = (root / "jobs.json.bak").read_bytes()
            store.shutdown()

            (root / "jobs.json").write_text('{"truncated":', encoding="utf-8")
            recovered_store = JobStore(root)
            try:
                self.assertIn(job_id, recovered_store.jobs)
                self.assertEqual(recovered_store.jobs[job_id]["product_name"], "恢复测试")
                self.assertIn("任务索引损坏", recovered_store.recovery_warning)
                self.assertEqual((root / "jobs.json.bak").read_bytes(), backup)
                quarantined = list(root.glob("jobs.json.corrupt-*"))
                self.assertEqual(len(quarantined), 1)
                self.assertTrue((root / "jobs" / job_id / "job.json").is_file())
            finally:
                recovered_store.shutdown()

    def test_delete_job_removes_index_files_and_running_process(self) -> None:
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
            try:
                with mock.patch.object(store._executor, "submit"):
                    public = store.create({"product_name": "测试"}, files, owner_id="owner-a")
                job_id = str(public["id"])
                job_root = root / "jobs" / job_id
                process = mock.Mock()
                with store._lock:
                    store._running_processes[job_id] = process
                with mock.patch("scripts.web_app.stop_process_group") as stop:
                    self.assertFalse(store.delete(job_id, owner_id="owner-b", workspace_id="local"))
                    self.assertTrue(store.delete(job_id, owner_id="owner-a", workspace_id="local"))
                stop.assert_called_once_with(process, grace_seconds=5.0)
                self.assertNotIn(job_id, store.jobs)
                self.assertFalse(job_root.exists())
            finally:
                store.shutdown()

    def test_http_delete_job_uses_browser_owner_scope(self) -> None:
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
            server = FlayrServer(("127.0.0.1", 0), store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/api/jobs/{job_id}"
            try:
                signed_owner_a = _signed_client_cookie("owner-a", server.client_cookie_secret)
                response = urlopen(
                    Request(
                        url,
                        method="DELETE",
                        headers={"Cookie": f"flayr_client_id={signed_owner_a}"},
                    )
                )
                self.assertEqual(response.status, 204)
                self.assertNotIn(job_id, store.jobs)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.shutdown()

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

    def test_web_worker_prefers_explicit_dual_model_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = {
                "benchmark_path": "/tmp/benchmark.mp4",
                "creator_path": "/tmp/creator.mp4",
                "product_name": "product",
                "category": "category",
                "price": "1",
                "market_code": "my",
                "selling_point": "point",
                "run_dir": str(Path(tmp) / "run"),
            }
            with mock.patch.dict(
                os.environ,
                {
                    "FLAYR_JUDGMENT_MODEL": "qwen3.7-plus",
                    "FLAYR_VISION_MODEL": "qwen3-vl-plus",
                    "FLAYR_LLM_MODEL": "qwen3.6-plus",
                },
                clear=False,
            ):
                command = store._command(job)
            store.shutdown()
            self.assertIn("--judgment-model", command)
            self.assertIn("qwen3.7-plus", command)
            self.assertIn("--vision-model", command)
            self.assertIn("qwen3-vl-plus", command)
            self.assertNotIn("--llm-model", command)

    def test_estimated_remaining_time_uses_coarse_phase_buckets(self) -> None:
        self.assertEqual(estimated_remaining_seconds(0), 30 * 60)
        self.assertEqual(estimated_remaining_seconds(10), 30 * 60)
        self.assertEqual(estimated_remaining_seconds(18), 25 * 60)
        self.assertEqual(estimated_remaining_seconds(50), 20 * 60)
        self.assertEqual(estimated_remaining_seconds(57), estimated_remaining_seconds(50))
        self.assertEqual(estimated_remaining_seconds(92), 2 * 60)
        self.assertEqual(estimated_remaining_seconds(100), 0)

    def test_degraded_progress_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            initialize_run_state(run_dir)
            transition_run_state(run_dir, PROCESSING)
            transition_run_state(run_dir, ANALYSIS_COMPLETED)
            transition_run_state(run_dir, REPORT_GENERATING)
            transition_run_state(run_dir, DEGRADED)
            self.assertEqual(progress_for_run(run_dir), (100, "报告生成（部分分析能力降级）"))

    def test_web_worker_does_not_create_or_advance_lifecycle_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root)
            job_id = "job-observer-only"
            run_dir = root / "run"
            log_path = root / "worker.log"
            run_dir.mkdir()
            store.jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "run_dir": str(run_dir),
                "log_path": str(log_path),
            }
            finished_process = mock.Mock()
            finished_process.poll.return_value = 0
            finished_process.wait.return_value = 0
            with (
                mock.patch.object(store, "_command", return_value=["flayr-test"]),
                mock.patch("scripts.web_app.subprocess.Popen", return_value=finished_process) as popen,
                mock.patch.object(store, "_finish"),
            ):
                store._run_job(job_id)
            self.assertIsNone(read_run_state(run_dir))
            kwargs = popen.call_args.kwargs
            if os.name == "posix":
                self.assertTrue(kwargs.get("start_new_session"))
            else:
                self.assertGreater(kwargs.get("creationflags", 0), 0)
            store.shutdown()

    def test_shutdown_stops_running_process_groups_before_waiting_for_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            process = mock.Mock()
            with (
                mock.patch("scripts.web_app.stop_process_group") as stop,
                mock.patch.object(store._executor, "shutdown", wraps=store._executor.shutdown) as executor_shutdown,
            ):
                with store._lock:
                    store._running_processes["job-running"] = process
                store.shutdown()
            stop.assert_called_once_with(process, grace_seconds=5.0)
            executor_shutdown.assert_called_once_with(wait=True, cancel_futures=True)

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
                tampered_signature = signed_owner_a[:-1] + (
                    "0" if signed_owner_a[-1] != "0" else "1"
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, headers={"Cookie": f"flayr_client_id={tampered_signature}"}))
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

    def test_http_streams_asset_without_reading_whole_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JobStore(root)
            run_dir = root / "job-run"
            run_dir.mkdir()
            payload = b"x" * (128 * 1024)
            (run_dir / "frame.bin").write_bytes(payload)
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
            url = f"http://127.0.0.1:{server.server_address[1]}/api/jobs/job-1/assets/frame.bin"
            try:
                signed_owner_a = _signed_client_cookie("owner-a", server.client_cookie_secret)
                with mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("asset was read whole")):
                    response = urlopen(
                        Request(url, headers={"Cookie": f"flayr_client_id={signed_owner_a}"})
                    )
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.shutdown()

    def test_http_upload_times_out_when_body_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            server = FlayrServer(("127.0.0.1", 0), store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    mock.patch("scripts.web_app.UPLOAD_IDLE_TIMEOUT_SECONDS", 0.05),
                    mock.patch("scripts.web_app.UPLOAD_TOTAL_TIMEOUT_SECONDS", 0.1),
                    socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=2) as client,
                ):
                    client.settimeout(2)
                    client.sendall(
                        b"POST /api/jobs HTTP/1.0\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"Content-Type: multipart/form-data; boundary=test\r\n"
                        b"Content-Length: 4\r\n"
                        b"\r\n"
                        b"x"
                    )
                    response_parts = []
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response_parts.append(chunk)
                    response = b"".join(response_parts)
                self.assertIn(b"400", response)
                self.assertIn("上传读取超时".encode("utf-8"), response)
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
            initialize_run_state(run_dir, job_id=job_id)
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
