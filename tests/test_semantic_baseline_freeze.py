from __future__ import annotations

import unittest

from scripts.build_legacy_gt_migration_inventory import build_inventory, classify_legacy_cell
from scripts.verify_semantic_baseline_freeze import verify_freeze


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
        self.assertEqual(verify_freeze(), [])


if __name__ == "__main__":
    unittest.main()
