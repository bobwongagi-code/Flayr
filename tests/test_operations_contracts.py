"""Regression tests for run-directory and release-operation contracts."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr  # noqa: E402


class OperationsContractTests(unittest.TestCase):
    def test_nonempty_explicit_output_dir_requires_explicit_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "analysis.json").write_text("stale", encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=False, mode="improve")
            with self.assertRaisesRegex(SystemExit, "已存在且非空"):
                flayr.create_run_dir(args)

    def test_web_run_state_does_not_block_new_explicit_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "run_state.json").write_text('{"state":"CREATED"}', encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=False, mode="improve")
            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())

    def test_reuse_removes_known_stale_top_level_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "analysis.json").write_text("stale", encoding="utf-8")
            (output_dir / "report.html").write_text("stale", encoding="utf-8")
            (output_dir / "benchmark").mkdir()
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=True, mode="improve")
            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())
            self.assertFalse((output_dir / "analysis.json").exists())
            self.assertFalse((output_dir / "report.html").exists())
            self.assertTrue((output_dir / "benchmark").is_dir())

    def test_reuse_rejects_unknown_top_level_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "notes.txt").write_text("not a Flayr artifact", encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=True, mode="improve")
            with self.assertRaisesRegex(SystemExit, "未识别的旧内容"):
                flayr.create_run_dir(args)


if __name__ == "__main__":
    unittest.main()
