"""Upload cleanup must not strand temporary files or admission reservations."""

from __future__ import annotations

import http.client
import io
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import web_app as web


class UploadCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict(web.os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def body(self) -> Path:
        body = self.root / "body.bin"
        body.write_bytes(
            b'--test\r\nContent-Disposition: form-data; name="benchmark_video"; filename="b.mp4"\r\n\r\nfirst'
            b'\r\n--test\r\nContent-Disposition: form-data; name="creator_video"; filename="c.mp4"\r\n\r\nsecond'
            b"\r\n--test--\r\n"
        )
        return body

    def test_parser_interrupt_closes_and_removes_all_created_parts(self) -> None:
        copy = web._copy_part_until_boundary
        for error_type in (OSError, KeyboardInterrupt, SystemExit):
            with self.subTest(error=error_type.__name__):
                error = error_type("synthetic interruption")
                calls = 0

                def interrupt(source, boundary, sink, limit, error=error):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        sink.write(b"partial")
                        raise error
                    return copy(source, boundary, sink, limit)

                with mock.patch.object(
                    web, "_copy_part_until_boundary", side_effect=interrupt
                ), self.assertRaises(error_type) as result:
                    web.parse_multipart(
                        self.body(),
                        "multipart/form-data; boundary=test",
                        staging_root=self.root,
                    )
                self.assertIs(result.exception, error)
                self.assertEqual(calls, 2)
                self.assertEqual(list(self.root.glob(".upload-part-*")), [])

    def test_parser_cleanup_failure_preserves_original_and_attempts_other_parts(
        self,
    ) -> None:
        error = web.RequestError("synthetic malformed part")
        copy, unlink = web._copy_part_until_boundary, Path.unlink
        calls = 0
        attempted = []

        def fail_parse(source, boundary, sink, limit):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error
            return copy(source, boundary, sink, limit)

        def locked(path, *args, **kwargs):
            if path.name.startswith(".upload-part-"):
                attempted.append(path)
                if len(attempted) == 1:
                    raise PermissionError("synthetic file lock")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(
            web, "_copy_part_until_boundary", side_effect=fail_parse
        ), mock.patch.object(Path, "unlink", locked), self.assertRaises(
            web.RequestError
        ) as result:
            web.parse_multipart(
                self.body(),
                "multipart/form-data; boundary=test",
                staging_root=self.root,
            )
        self.assertIs(result.exception, error)
        self.assertEqual(len(attempted), 2)
        self.assertTrue(attempted[0].is_file())
        self.assertFalse(attempted[1].exists())
        self.assertTrue(error.__notes__)

    def test_cleanup_attempts_every_file_then_reports_failure(self) -> None:
        paths = [self.root / name for name in ("first.part", "second.part")]
        for path in paths:
            path.write_bytes(b"synthetic")
        unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path == paths[0]:
                raise PermissionError("synthetic lock")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", locked), self.assertRaises(
            PermissionError
        ):
            web.cleanup_upload_files(
                {str(i): {"path": path} for i, path in enumerate(paths)}
            )
        self.assertTrue(paths[0].is_file())
        self.assertFalse(paths[1].exists())

    def store(self) -> web.JobStore:
        store = web.JobStore(self.root / "store")
        self.addCleanup(store._executor.shutdown, wait=True, cancel_futures=True)
        return store

    def handler(self, store) -> SimpleNamespace:
        return SimpleNamespace(
            path="/api/jobs",
            headers={"Content-Type": "multipart/form-data; boundary=test"},
            server=SimpleNamespace(store=store, public_mode=False),
            _authorize_request=lambda **_: True,
            _client_id=lambda: "owner-a",
            _upload_storage_reservation_bytes=lambda: 4096,
            _read_request_body=lambda sink: sink.write(b"synthetic body"),
            _json=mock.Mock(),
        )

    def test_http_cleanup_failure_does_not_skip_body_or_capacity_release(self) -> None:
        store = self.store()
        handler = self.handler(store)
        for cleanup_failure in (False, True):
            with self.subTest(cleanup_failure=cleanup_failure), mock.patch.object(
                web, "parse_multipart", return_value=({}, {})
            ), mock.patch.object(
                store, "create", return_value={"id": "accepted"}
            ), mock.patch.object(
                web,
                "cleanup_upload_files",
                side_effect=(
                    PermissionError("synthetic lock") if cleanup_failure else None
                ),
            ), mock.patch.object(
                web.sys, "stderr", io.StringIO()
            ):
                web.FlayrHandler.do_POST(handler)
            self.assertEqual(store._storage_reservations, {})
            self.assertEqual(list(store.root.glob(".upload-body-*")), [])
            self.assertEqual(handler._json.call_args.args[0], 202)

    def test_http_locked_body_still_releases_capacity(self) -> None:
        store = self.store()
        handler = self.handler(store)
        unlink = Path.unlink

        def locked(path, *args, **kwargs):
            if path.name.startswith(".upload-body-"):
                raise PermissionError("synthetic body lock")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(
            web, "parse_multipart", return_value=({}, {})
        ), mock.patch.object(
            store, "create", return_value={"id": "accepted"}
        ), mock.patch.object(
            Path, "unlink", locked
        ), mock.patch.object(
            web.sys, "stderr", io.StringIO()
        ):
            web.FlayrHandler.do_POST(handler)
        self.assertEqual(store._storage_reservations, {})
        self.assertEqual(len(list(store.root.glob(".upload-body-*"))), 1)
        self.assertEqual(handler._json.call_args.args[0], 202)

    def test_http_interruption_is_not_replaced_by_cleanup_failure(self) -> None:
        store = self.store()
        handler = self.handler(store)
        error = KeyboardInterrupt("synthetic read interruption")
        handler._read_request_body = mock.Mock(side_effect=error)
        with mock.patch.object(
            web, "cleanup_upload_files", side_effect=PermissionError("synthetic lock")
        ), mock.patch.object(web.sys, "stderr", io.StringIO()), self.assertRaises(
            KeyboardInterrupt
        ) as result:
            web.FlayrHandler.do_POST(handler)
        self.assertIs(result.exception, error)
        self.assertEqual(store._storage_reservations, {})
        self.assertEqual(list(store.root.glob(".upload-body-*")), [])
        handler._json.assert_not_called()

    def test_admission_reservation_is_released_when_final_cleanup_fails(self) -> None:
        store = self.store()
        files = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"{role}.mp4"
            path.write_bytes(b"synthetic input")
            files[role] = {"path": path, "filename": path.name}
        with mock.patch.object(store._executor, "submit"), mock.patch.object(
            web, "cleanup_upload_files", side_effect=PermissionError("synthetic lock")
        ), mock.patch.object(web.sys, "stderr", io.StringIO()):
            job = store.create({}, files, enforce_limits=True, owner_id="owner-a")
        self.assertEqual(store._admission_reservations, {})
        self.assertEqual(store.jobs[job["id"]]["status"], "queued")
        self.assertTrue(Path(store.jobs[job["id"]]["creator_path"]).is_file())

    def test_interrupted_adoption_rolls_back_only_its_own_directory(self) -> None:
        store = self.store()
        files = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"{role}.mp4"
            path.write_bytes(b"synthetic input")
            files[role] = {"path": path, "filename": path.name}
        move = web.shutil.move
        error = KeyboardInterrupt("synthetic adoption interruption")
        calls = 0

        def interrupt(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error
            return move(source, destination)

        with mock.patch.object(
            web.shutil, "move", side_effect=interrupt
        ), self.assertRaises(KeyboardInterrupt) as result:
            store.create({}, files, enforce_limits=True, owner_id="owner-a")
        self.assertIs(result.exception, error)
        self.assertEqual(store._admission_reservations, {})
        self.assertEqual(store.jobs, {})
        self.assertEqual(list(store.jobs_root.iterdir()), [])

    def test_existing_job_directory_is_never_adopted_or_rolled_back(self) -> None:
        store = self.store()
        existing = store.jobs_root / ("job-" + "a" * 12)
        existing.mkdir()
        marker = existing / "prior-source.txt"
        marker.write_text("prior job", encoding="utf-8")
        files = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"{role}.mp4"
            path.write_bytes(b"synthetic input")
            files[role] = {"path": path, "filename": path.name}
        with mock.patch.object(
            web.uuid, "uuid4", return_value=SimpleNamespace(hex="a" * 32)
        ), mock.patch.object(store._executor, "submit") as submit, self.assertRaises(
            FileExistsError
        ):
            store.create({}, files, enforce_limits=True, owner_id="owner-a")
        self.assertEqual(marker.read_text(encoding="utf-8"), "prior job")
        self.assertEqual(store._admission_reservations, {})
        self.assertEqual(store.jobs, {})
        submit.assert_not_called()

    def test_real_http_acceptance_releases_capacity_despite_cleanup_error(self) -> None:
        store = self.store()
        server = web.FlayrServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever)
        released = threading.Event()
        release = store.release_upload

        def release_upload(workspace_id, size):
            release(workspace_id, size)
            released.set()

        body = self.body().read_bytes()
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=5
        )
        try:
            with mock.patch.object(store._executor, "submit"), mock.patch.object(
                web,
                "cleanup_upload_files",
                side_effect=PermissionError("synthetic lock"),
            ), mock.patch.object(
                store, "release_upload", side_effect=release_upload
            ), mock.patch.object(
                web.sys, "stderr", io.StringIO()
            ):
                connection.request(
                    "POST",
                    "/api/jobs",
                    body,
                    {"Content-Type": "multipart/form-data; boundary=test"},
                )
                response = connection.getresponse()
                response.read()
                capacity_released = released.wait(timeout=2)
                self.assertEqual(response.status, 202)
                self.assertTrue(capacity_released, "HTTP request stranded capacity")
            self.assertEqual(store._storage_reservations, {})
            self.assertEqual(list(store.root.glob(".upload-body-*")), [])
            self.assertEqual(len(store.jobs), 1)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
