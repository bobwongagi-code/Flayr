from __future__ import annotations

import unittest

from scripts.evaluate_compact_cohort import _safe_component_map


class CompactCohortPathTests(unittest.TestCase):
    def test_sanitized_sample_or_model_collisions_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "share output component"):
            _safe_component_map(["a/b", "a_b"], label="model")

    def test_component_map_preserves_safe_paths_for_distinct_values(self) -> None:
        self.assertEqual(
            _safe_component_map(["qwen3.6-plus", "qwen3.7-plus"], label="model"),
            {"qwen3.6-plus": "qwen3.6-plus", "qwen3.7-plus": "qwen3.7-plus"},
        )


if __name__ == "__main__":
    unittest.main()
