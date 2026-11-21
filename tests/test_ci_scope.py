"""CI publication ranges are not individual implementation batches."""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import check_change_scope as scope


ROOT = Path(__file__).resolve().parents[1]


class CIScopeTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((ROOT / "references/bitter-lesson-runtime-fix-scope.json").read_text())

    def test_publication_range_does_not_use_an_old_batch_allowlist_or_budget(self):
        paths = {f"tests/test_batch_{number}.py" for number in range(91)}
        with (
            mock.patch.object(scope, "_publication_changes", return_value=[("candidate", sorted(paths))]),
            mock.patch.object(scope, "_changed_paths", return_value=paths),
            mock.patch.object(scope, "_line_counts", return_value=(9000, 4000)) as counts,
        ):
            self.assertEqual(scope.check_publication(self.spec, "base"), [])
            counts.assert_not_called()
            self.assertTrue(scope.check_scope(self.spec, "base"))
            counts.assert_called_once()

    def test_publication_range_still_rejects_every_private_artifact_class(self):
        paths = {"runs/draft/analysis.json", "outputs/report.html", "jobs-private.json",
                 "scripts/flayr_core/__pycache__/payload.pyc", "scripts/flayr.py"}
        with mock.patch.object(scope, "_publication_changes", return_value=[("candidate", sorted(paths))]):
            issues = scope.check_publication(self.spec, "base")
        self.assertEqual(len(issues), 4)
        for path in paths - {"scripts/flayr.py"}:
            self.assertIn("candidate: forbidden path: " + path, issues)

    def test_ci_selects_publication_mode_and_preserves_event_base(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text()
        self.assertIn("FLAYR_SCOPE_MODE: publication", workflow)
        self.assertIn("github.event.pull_request.base.sha", workflow)
        self.assertIn("github.event.before", workflow)
        runner = (ROOT / "scripts/run-tests.sh").read_text()
        self.assertIn('"${FLAYR_SCOPE_MODE:-batch}"', runner)
        self.assertIn("--publication", runner)
        self.assertIn("unsupported scope mode", runner)


class PublicationRepositoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        root_patch = mock.patch.object(scope, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.spec = {"change_scope": {
            "allowed_globs": ["*.txt"],
            "forbidden_globs": ["runs/**", "outputs/**"],
            "max_files": 10,
            "max_added_lines": 10,
            "max_deleted_lines": 10,
        }}
        self.git("init", "-q")
        self.write("notes.txt", "base")
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.run(
            [
                "git", "-c", "user.name=Scope Fixture",
                "-c", "user.email=scope@example.invalid",
                "-c", "commit.gpgsign=false",
                "-c", f"core.hooksPath={self.root / 'no-hooks'}",
                "-c", "core.autocrlf=false", *args,
            ],
            check=True, cwd=self.root, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="utf-8")
        return path

    def commit(self, label):
        self.git("add", "--all")
        self.git("commit", "-qm", label)
        return self.git("rev-parse", "HEAD")

    def test_local_deletion_cannot_hide_a_committed_forbidden_file(self):
        path = self.write("runs/report.json", "{}")
        introduced = self.commit("include artifact")
        path.unlink()
        expected = [f"{introduced}: forbidden path: runs/report.json"]
        self.assertEqual(scope.check_publication(self.spec, self.base), expected)
        self.git("add", "--", "runs/report.json")
        self.assertEqual(scope.check_publication(self.spec, self.base), expected)

    def test_staged_and_untracked_files_do_not_change_publication_scope(self):
        self.write("notes.txt", "ready to publish")
        self.commit("safe change")
        self.write("runs/staged.json", "{}")
        self.git("add", "--", "runs/staged.json")
        self.write("outputs/untracked.html", "<p>local draft</p>")

        self.assertEqual(scope.check_publication(self.spec, self.base), [])
        self.assertEqual(scope.check_publication(self.spec, "0" * 40), [])
        self.assertEqual(scope.check_publication(self.spec, self.base, approved_paths=["notes.txt"]), [])
        self.assertEqual(scope.check_scope(self.spec, self.base), [
            "forbidden path: outputs/untracked.html",
            "forbidden path: runs/staged.json",
        ])

    def test_publication_can_check_an_explicit_candidate_instead_of_head(self):
        self.write("notes.txt", "candidate")
        candidate = self.commit("safe candidate")
        self.write("runs/later.json", "{}")
        later = self.commit("not the candidate")
        spec = self.write("spec.json", json.dumps(self.spec))
        with mock.patch("sys.argv", [
            "check_change_scope.py", "--publication", "--spec", str(spec),
            "--base-ref", self.base, "--head-ref", candidate, "--approved-path", "notes.txt",
        ]), redirect_stdout(io.StringIO()):
            self.assertEqual(scope.main(), 0)
        self.assertEqual(
            scope.check_publication(self.spec, self.base), [f"{later}: forbidden path: runs/later.json"]
        )

    def test_initial_push_checks_complete_candidate_tree(self):
        artifact = "runs/old \u62a5\u544a.json"
        self.write(artifact, "{}")
        introduced = self.commit("add artifact")
        self.write("notes.txt", "candidate")
        candidate = self.commit("notes only")
        self.assertEqual(
            scope.check_publication(self.spec, "0" * 40, head_ref=candidate),
            [f"{introduced}: forbidden path: {artifact}"],
        )

    def test_publication_rejects_internal_files_added_and_removed_in_the_range(self):
        self.write("notes.txt", "approved change")
        self.commit("publish notes")
        internal = self.write("references/internal draft.md", "private fixture")
        artifact = self.write("runs/middle.json", "{}")
        introduced = self.commit("accidentally add internal files")
        internal.unlink()
        artifact.unlink()
        candidate = self.commit("remove internal files again")
        self.assertEqual(self.git("diff", "--name-only", self.base, candidate), "notes.txt")
        self.assertIn(
            f"{introduced}: forbidden path: runs/middle.json",
            scope.check_publication(self.spec, self.base, head_ref=candidate),
        )

        spec = self.write("spec.json", json.dumps(self.spec))
        out = io.StringIO()
        with mock.patch("sys.argv", [
            "check_change_scope.py", "--publication", "--spec", str(spec),
            "--base-ref", self.base, "--head-ref", candidate, "--approved-path", "notes.txt",
        ]), redirect_stdout(out):
            self.assertEqual(scope.main(), 1)
        for commit in (introduced, candidate):
            self.assertIn(f"{commit}: path outside approved publication scope: references/internal draft.md",
                          out.getvalue())
            self.assertIn(f"{commit}: forbidden path: runs/middle.json", out.getvalue())

    def test_publication_checks_merge_edits_without_reattributing_base_changes(self):
        self.git("branch", "incoming")
        self.write("already-public.txt", "on the base only")
        public_base = self.commit("advance public base")
        self.git("checkout", "-q", "incoming")
        self.write("notes.txt", "approved")
        self.write("references/branch draft.md", "internal")
        branch_commit = self.commit("prepare branch")
        self.git("merge", "--no-commit", "--no-ff", public_base)
        self.write("references/merge draft.md", "internal")
        merge = self.commit("complete merge")

        self.assertEqual(scope.check_publication(
            self.spec, public_base, head_ref=merge, approved_paths=["notes.txt"],
        ), [
            f"{branch_commit}: path outside approved publication scope: references/branch draft.md",
            f"{merge}: path outside approved publication scope: references/merge draft.md",
        ])

    def test_approved_paths_require_explicit_publication_base(self):
        err = io.StringIO()
        with mock.patch("sys.argv", [
            "check_change_scope.py", "--publication", "--approved-path", "notes.txt",
        ]), redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            scope.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("explicit --base-ref", err.getvalue())


if __name__ == "__main__":
    unittest.main()
