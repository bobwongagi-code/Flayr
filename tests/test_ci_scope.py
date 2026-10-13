"""CI publication ranges are not individual implementation batches."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from scripts import check_change_scope as scope


ROOT = Path(__file__).resolve().parents[1]


class CIScopeTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((ROOT / "references/bitter-lesson-runtime-fix-scope.json").read_text())

    def test_publication_range_does_not_use_an_old_batch_allowlist_or_budget(self):
        paths = {f"tests/test_batch_{number}.py" for number in range(91)}
        with mock.patch.object(scope, "_changed_paths", return_value=paths), mock.patch.object(
            scope, "_line_counts", return_value=(9000, 4000)
        ) as counts:
            self.assertEqual(scope.check_publication(self.spec, "base"), [])
            counts.assert_not_called()
            self.assertTrue(scope.check_scope(self.spec, "base"))
            counts.assert_called_once()

    def test_publication_range_still_rejects_every_private_artifact_class(self):
        paths = {"runs/draft/analysis.json", "outputs/report.html", "jobs-private.json",
                 "scripts/flayr_core/__pycache__/payload.pyc", "scripts/flayr.py"}
        with mock.patch.object(scope, "_changed_paths", return_value=paths):
            issues = scope.check_publication(self.spec, "base")
        self.assertEqual(len(issues), 4)
        for path in paths - {"scripts/flayr.py"}:
            self.assertIn("forbidden path: " + path, issues)

    def test_publication_uses_the_entire_range_and_keeps_untracked_checks(self):
        calls = []

        def git(*args):
            calls.append(args)
            return "runs/older-commit/analysis.json\n" if args[0] == "diff" else "outputs/local.html\n"

        with mock.patch.object(scope, "_git", side_effect=git):
            issues = scope.check_publication(self.spec, "base")
        self.assertEqual(len(issues), 2)
        self.assertEqual(calls[0], ("diff", "--name-only", "--no-renames", "base", "--"))
        self.assertIn(("ls-files", "--others", "--exclude-standard"), calls)

    def test_initial_push_checks_complete_tree_not_only_the_tip(self):
        with mock.patch.object(scope, "_git", return_value="") as git:
            self.assertEqual(scope.check_publication(self.spec, "0" * 40), [])
        self.assertEqual(git.call_args_list[0].args,
                         ("diff", "--name-only", "--no-renames", scope.EMPTY_TREE_SHA, "HEAD", "--"))

    def test_ci_selects_publication_mode_and_preserves_event_base(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text()
        self.assertIn("FLAYR_SCOPE_MODE: publication", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("github.event.before", workflow)
        runner = (ROOT / "scripts/run-tests.sh").read_text()
        self.assertIn('"${FLAYR_SCOPE_MODE:-batch}"', runner)
        self.assertIn("--publication", runner)
        self.assertIn("unsupported scope mode", runner)


if __name__ == "__main__":
    unittest.main()
