"""Publication faults use synthetic files, never provider or media processing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import flayr


class PreprocessPublicationTransactionTests(unittest.TestCase):
    def fixture(self, root):
        role, staging = root / "creator", root / ".creator.generation-test"
        role.mkdir()
        staging.mkdir()
        (role / "old.txt").write_bytes(b"previous published bytes")
        (role / "_preprocess.json").write_text('{"version":"old"}', encoding="utf-8")
        (staging / "new.txt").write_bytes(b"candidate bytes")
        (staging / "metadata.json").write_text(
            json.dumps({"work_dir": str(staging)}), encoding="utf-8"
        )
        return role, staging

    @staticmethod
    def contents(root):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_every_precommit_failure_preserves_old_generation_and_exception(self):
        for phase in (
            "backup",
            "backup_after_move",
            "publish",
            "publish_after_move",
            "manifest",
        ):
            for failure_type in (OSError, KeyboardInterrupt, SystemExit):
                with self.subTest(
                    phase=phase, failure=failure_type.__name__
                ), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp).resolve()
                    role, staging = self.fixture(root)
                    original = self.contents(role)
                    failure = failure_type("injected publication failure")
                    real_replace, real_write = Path.replace, flayr.write_json

                    def replace(path, target):
                        backup_move = path == role and Path(target).name.startswith(
                            ".creator.previous-"
                        )
                        publish_move = path == staging and Path(target) == role
                        matches = (phase.startswith("backup") and backup_move) or (
                            phase.startswith("publish") and publish_move
                        )
                        if matches:
                            if phase.endswith("after_move"):
                                real_replace(path, target)
                            raise failure
                        return real_replace(path, target)

                    def write(path, payload):
                        if (
                            phase == "manifest"
                            and Path(path) == role / "_preprocess.json"
                        ):
                            Path(path).write_bytes(b"{")
                            raise failure
                        return real_write(path, payload)

                    with mock.patch.object(Path, "replace", replace), mock.patch.object(
                        flayr, "write_json", side_effect=write
                    ):
                        with self.assertRaises(failure_type) as caught:
                            flayr._promote_preprocess_generation(
                                staging, role, {"work_dir": str(staging)}
                            )
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(self.contents(role), original)
                    self.assertTrue((staging / "new.txt").is_file())
                    self.assertEqual(list(root.glob(".creator.previous-*")), [])

    def test_failed_first_generation_is_not_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            staging, role = root / ".creator.generation-test", root / "creator"
            staging.mkdir()
            (staging / "new.txt").write_bytes(b"candidate")
            with mock.patch.object(
                flayr, "write_json", side_effect=OSError("manifest")
            ):
                with self.assertRaises(OSError):
                    flayr._promote_preprocess_generation(
                        staging, role, {"work_dir": str(staging)}
                    )
            self.assertFalse(role.exists())
            self.assertTrue((staging / "new.txt").is_file())

    def test_success_commits_complete_manifest_then_removes_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            role, staging = self.fixture(root)
            published = flayr._promote_preprocess_generation(
                staging, role, {"work_dir": str(staging)}
            )
            self.assertFalse(staging.exists())
            self.assertFalse((role / "old.txt").exists())
            self.assertEqual((role / "new.txt").read_bytes(), b"candidate bytes")
            self.assertTrue(
                flayr._preprocess_artifacts_match(
                    role, published["preprocess_artifacts"]
                )
            )
            self.assertEqual(
                json.loads((role / "_preprocess.json").read_text(encoding="utf-8")),
                published,
            )
            self.assertEqual(list(root.glob(".creator.previous-*")), [])

    def test_rollback_failure_keeps_backup_and_original_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            role, staging = self.fixture(root)
            original = self.contents(role)
            failure = OSError("manifest failed")
            real_replace, real_write = Path.replace, flayr.write_json

            def replace(path, target):
                if path == role and Path(target) == staging:
                    raise OSError("cannot withdraw candidate")
                return real_replace(path, target)

            def write(path, payload):
                if Path(path) == role / "_preprocess.json":
                    raise failure
                return real_write(path, payload)

            with mock.patch.object(Path, "replace", replace), mock.patch.object(
                flayr, "write_json", side_effect=write
            ):
                with self.assertRaises(OSError) as caught:
                    flayr._promote_preprocess_generation(staging, role, {})
            self.assertIs(caught.exception, failure)
            backups = list(root.glob(".creator.previous-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(self.contents(backups[0]), original)
            self.assertIn("retained for recovery", " ".join(failure.__notes__))


if __name__ == "__main__":
    unittest.main()
