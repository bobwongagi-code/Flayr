from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.build_legacy_gt_migration_inventory import build_inventory, classify_legacy_cell
from scripts import verify_semantic_baseline_freeze as verifier


class LegacyGtMigrationInventoryTest(unittest.TestCase):
    def test_legacy_small_stays_ambiguous(self) -> None:
        cell = classify_legacy_cell({"stages": {"S1": "small"}}, "S1")
        self.assertEqual(cell["migration_status"], "legacy_ambiguous")
        self.assertTrue(cell["requires_expert_confirmation"])

    def test_documented_legacy_na_is_not_treated_as_missing(self) -> None:
        cell = classify_legacy_cell(
            {
                "stages": {"S5": "na"},
                "stage_label_statuses": {
                    "S5": {"status": "not_applicable", "reason": "双方均未设置独立背书。"}
                },
            },
            "S5",
        )
        self.assertEqual(cell["migration_status"], "legacy_not_applicable_documented")
        self.assertFalse(cell["requires_expert_confirmation"])

    def test_inventory_never_creates_canonical_labels(self) -> None:
        inventory = build_inventory({
            "samples": {
                "sample": {
                    "partition": "seen_validation",
                    "stages": {stage: "small" for stage in ("S1", "S2", "S3", "S4", "S5", "S6")},
                }
            }
        })
        self.assertEqual(inventory["summary"]["status_counts"], {"legacy_ambiguous": 6})
        self.assertNotIn("human_gap", inventory["samples"]["sample"])

    def test_invalid_canonical_gap_is_not_treated_as_migrated(self) -> None:
        cell = classify_legacy_cell({"human_gap": {"S1": "smol"}}, "S1")
        self.assertEqual(cell["migration_status"], "canonical_invalid")
        self.assertTrue(cell["requires_expert_confirmation"])


class SemanticBaselineFreezeContractTest(unittest.TestCase):
    def test_repository_freeze_contract_is_consistent(self) -> None:
        self.assertEqual(verifier.verify_freeze(), [])

    def test_snapshot_and_policy_rejections_keep_cli_order(self) -> None:
        read_json = verifier._read_json
        freeze = read_json(verifier.FREEZE_PATH)
        freeze["promotion_eligible"] = True
        freeze["gt_snapshot"]["labels_sha256"] = "0" * 64
        freeze["gt_snapshot"]["relation_cells"] += 1
        freeze["gt_snapshot"]["present_key_events"] += 1
        freeze["execution_policy"]["allow_new_video_calls_initially"] = True

        def read_with_drift(path):
            return freeze if path == verifier.FREEZE_PATH else read_json(path)

        output = io.StringIO()
        with patch.object(verifier, "_read_json", side_effect=read_with_drift):
            with redirect_stdout(output):
                self.assertEqual(verifier.main(), 1)

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "semantic baseline freeze verification failed:",
                "- offline semantic baseline must never be promotion_eligible",
                "- frozen GT snapshot hash drifted",
                "- frozen relation-cell count drifted",
                "- frozen present-key-event count drifted",
                "- the frozen baseline must start with offline artifacts only",
            ],
        )

    def test_source_drift_checks_all_three_git_views_even_when_not_an_ancestor(self) -> None:
        commit = verifier._read_json(verifier.FREEZE_PATH)["production_behavior_commit"]
        frozen_paths = (
            "scripts/flayr.py", "scripts/flayr_core", *verifier.SOURCE_CONTRACT_FILES,
        )
        with (
            patch.object(verifier.subprocess, "check_call") as check_commit,
            patch.object(verifier.subprocess, "call", return_value=1) as check_ancestor,
            patch.object(
                verifier, "_git_output",
                side_effect=[
                    "scripts/flayr_core/llm/pipeline.py\nscripts/flayr_core/freeze_contract.py",
                    "scripts/flayr.py\nscripts/flayr_core/llm/pipeline.py",
                    "scripts/flayr_core/stage_evidence_contracts.py",
                ],
            ) as diff,
        ):
            errors = verifier.verify_freeze()
            check_commit.assert_called_once_with(
                ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                cwd=verifier.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            check_ancestor.assert_called_once_with(
                ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=verifier.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.assertEqual(
                [entry.args for entry in diff.call_args_list],
                [
                    ("diff", "--name-only", f"{commit}..HEAD", "--", *frozen_paths),
                    ("diff", "--name-only", "--", *frozen_paths),
                    ("diff", "--cached", "--name-only", "--", *frozen_paths),
                ],
            )

        self.assertEqual(
            errors,
            [
                "production behavior commit is not an ancestor of HEAD",
                "production semantic source drifted after the frozen behavior commit: "
                "scripts/flayr.py, scripts/flayr_core/llm/pipeline.py, "
                "scripts/flayr_core/stage_evidence_contracts.py",
            ],
        )

    def test_missing_source_commit_does_not_run_dependent_diff_checks(self) -> None:
        commit = verifier._read_json(verifier.FREEZE_PATH)["production_behavior_commit"]
        with (
            patch.object(
                verifier.subprocess, "check_call",
                side_effect=subprocess.CalledProcessError(128, "git cat-file"),
            ),
            patch.object(verifier.subprocess, "call") as check_ancestor,
            patch.object(verifier, "_git_output") as diff,
        ):
            self.assertEqual(
                verifier.verify_freeze(), [f"production behavior commit is not available: {commit}"]
            )
            check_ancestor.assert_not_called()
            diff.assert_not_called()

    def test_legacy_review_cannot_claim_canonical_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.md"
            review.write_text(
                verifier.REVIEW_PATH.read_text(encoding="utf-8")
                + "\n| fixture-only | S3 | small | fixture | 已确认 |"
                + "\n| R9999 | fixture-only | S4 | tie | fixture | 已确认 |\n",
                encoding="utf-8",
            )
            with patch.object(verifier, "REVIEW_PATH", review):
                self.assertEqual(
                    verifier.verify_freeze(),
                    [
                        "legacy GT human review table must preserve every initially ambiguous cell",
                        "fixture-only/S3 claims review confirmation without canonical GT",
                        "fixture-only/S4 claims relation confirmation without matching canonical GT",
                    ],
                )


if __name__ == "__main__":
    unittest.main()
