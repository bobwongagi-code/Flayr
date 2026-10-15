"""Offline checks for publishing generation paths without losing diagnostics."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr
from flayr_core.video_evidence import audit_video_evidence


class PreprocessGenerationPathTests(unittest.TestCase):
    def test_promotion_preserves_audit_warnings_and_relocates_embedded_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / ".creator.generation-test"
            role = root / "creator"
            staging.mkdir()
            frame = staging / "frame.jpg"
            frame.write_bytes(b"frame")
            audit = audit_video_evidence(staging, {
                "analysis_frames": [
                    {"path": str(frame), "timestamp_seconds": 0},
                    {"path": str(frame), "timestamp_seconds": 0},
                ],
            }, {"work_dir": str(staging)})
            old_warning = f"analysis frame manifest contains duplicate path: {frame}"
            self.assertIn(old_warning, audit["warnings"])
            result = {"work_dir": str(staging), "audit_path": audit["path"],
                      "errors": audit["warnings"], "video_evidence": {"status": "partial"}}
            published = flayr._promote_preprocess_generation(staging, role, result)
            saved = json.loads((role / "video_evidence_audit.json").read_text())
            new_warning = f"analysis frame manifest contains duplicate path: {(role / 'frame.jpg').resolve()}"
            self.assertEqual(saved["status"], "warn")
            self.assertIn(new_warning, saved["warnings"])
            self.assertIn(new_warning, saved["coverage"]["warnings"])
            self.assertIn(new_warning, published["errors"])
            self.assertEqual(published["video_evidence"]["status"], "partial")
            self.assertNotIn(str(staging), json.dumps(saved))
            self.assertTrue(flayr._preprocess_artifacts_match(role, published["preprocess_artifacts"]))

    def test_embedded_paths_keep_punctuation_multiple_references_and_external_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old, new = root / ".creator.generation-test", root / "creator"
            old.mkdir()
            external = root / "external.jpg"
            text = f"missing: '{old / 'a.jpg'}'; frames: {old / 'b.jpg'}, {external}"
            rewritten = flayr._rewrite_generation_paths({"errors": [text]}, old, new)
            self.assertEqual(rewritten["errors"][0],
                             f"missing: '{(new / 'a.jpg').resolve()}'; frames: {(new / 'b.jpg').resolve()}, {external}")

    def test_prefix_lookalikes_and_urls_are_not_rewritten_or_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old, new = root / ".creator.generation-test", root / "creator"
            old.mkdir()
            values = [f"other: {old}-other/a.jpg", f"nested: /elsewhere{old}/a.jpg",
                      f"remote: https://example.test{old}/a.jpg"]
            payload = {"notes": values, "path": str(old / "a.jpg")}
            artifact = old / "audit.json"
            artifact.write_text(json.dumps(payload))
            flayr._rewrite_generation_json_artifacts(old, old, new)
            saved = json.loads(artifact.read_text())
            self.assertEqual(saved["notes"], values)
            self.assertEqual(saved["path"], str((new / "a.jpg").resolve()))

    def test_unrewritable_path_key_and_invalid_json_still_block_publication(self):
        for invalid_json in (False, True):
            with self.subTest(invalid_json=invalid_json), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                old, new = root / ".creator.generation-test", root / "creator"
                old.mkdir()
                new.mkdir()
                sentinel = new / "old.txt"
                sentinel.write_text("previous generation")
                artifact = old / "audit.json"
                artifact.write_text(str(old / "x.jpg") if invalid_json else json.dumps({str(old / "x.jpg"): True}))
                with self.assertRaises(ValueError):
                    flayr._promote_preprocess_generation(old, new, {"work_dir": str(old)})
                self.assertEqual(sentinel.read_text(), "previous generation")
                self.assertTrue(old.exists())

    def test_standalone_escape_is_not_relocated_into_a_valid_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old, new = root / ".creator.generation-test", root / "creator"
            old.mkdir()
            value = str(old / ".." / "outside.jpg")
            self.assertEqual(flayr._rewrite_generation_paths(value, old, new), value)
            (old / "audit.json").write_text(json.dumps({"path": value}))
            with self.assertRaisesRegex(ValueError, "staging path remains"):
                flayr._rewrite_generation_json_artifacts(old, old, new)


if __name__ == "__main__":
    unittest.main()
