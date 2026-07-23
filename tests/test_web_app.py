from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.web_app import JobStore, parse_multipart, progress_for_run, safe_asset_path


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
            (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(progress_for_run(run_dir), (100, "报告生成"))

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
