"""Runtime field ownership regression tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_result_field_ownership import inventory, ownership_violations  # noqa: E402


class ResultFieldOwnershipTests(unittest.TestCase):
    def test_reviewed_projection_ownership_is_distinct_from_report_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = root / "scripts" / "flayr_core"
            production.mkdir(parents=True)
            (production / "human_review.py").write_text(
                "reviewed = copy.deepcopy(stage)\n"
                "reviewed['comparison_status'] = 'confirmed'\n"
                "reviewed['model_gap_magnitude'] = 'large'\n"
                "reviewed['severity'] = 'large'\n",
                encoding="utf-8",
            )
            (production / "report.py").write_text(
                "result['severity'] = 'large'\n",
                encoding="utf-8",
            )

            fields = ("comparison_status", "model_gap_magnitude", "severity")
            violations = ownership_violations(inventory(root, fields))

            self.assertEqual(
                [item for item in violations if "human_review.py" in item],
                [],
            )
            self.assertTrue(any("report.py" in item for item in violations))


if __name__ == "__main__":
    unittest.main()
