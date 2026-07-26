from __future__ import annotations

import unittest

from scripts.flayr_core.freeze_contract import (
    EVALUATION_ROLES,
    REQUIRED_FREEZE_CHECKS,
    cohort_freeze_status,
    evaluation_role_for_sample,
)
from scripts.flayr_core.model_execution import ModelExecutionConfig


class FreezeContractTest(unittest.TestCase):
    def _config(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "provider": "openai",
            "api_url": "https://example.invalid/v1/chat/completions",
            "model": "test-model",
            "fallback_model": None,
            "temperature": 0.0,
            "max_tokens": 16384,
            "top_p": None,
            "seed": 0,
            "response_format": {"type": "json_object"},
            "stop": ["<END>"],
            "transport_retry": 2,
            "completion_attempts": 3,
            "timeout": {"connect": 30, "read": None, "low_speed": 180, "overall": 1800},
        }
        value.update(overrides)
        return value

    def test_model_execution_config_is_complete_and_canonical(self) -> None:
        first = ModelExecutionConfig.from_mapping(self._config())
        reordered = ModelExecutionConfig.from_mapping(
            {key: self._config()[key] for key in reversed(tuple(self._config()))}
        )
        self.assertEqual(first.as_dict()["seed"], 0)
        self.assertEqual(first.as_dict(), reordered.as_dict())
        self.assertEqual(first.sha256, reordered.sha256)
        self.assertEqual(
            set(first.as_dict()),
            {
                "schema_version", "provider", "api_url", "model", "fallback_model",
                "temperature", "max_tokens", "top_p", "seed", "response_format", "stop",
                "transport_retry", "completion_attempts", "timeout",
            },
        )

    def test_model_execution_config_rejects_incomplete_identity(self) -> None:
        incomplete = self._config()
        incomplete.pop("provider")
        with self.assertRaisesRegex(ValueError, "provider"):
            ModelExecutionConfig.from_mapping(incomplete)

    def test_evaluation_role_is_orthogonal_to_historical_metadata(self) -> None:
        label = {"partition": "seen_validation"}
        sample = {"group": "seen_validation", "purpose": "seen_validation"}
        role, errors = evaluation_role_for_sample(label, sample)
        self.assertEqual(role, "mechanism_regression")
        self.assertEqual(errors, [])
        self.assertNotIn("evaluation_role", label)
        self.assertNotIn("evaluation_role", sample)
        self.assertEqual(EVALUATION_ROLES, {"calibration", "mechanism_regression", "blind_promotion"})

    def test_freeze_status_blocks_any_missing_required_check(self) -> None:
        checks = {name: {"ok": True, "sha256": "fixed"} for name in REQUIRED_FREEZE_CHECKS}
        checks["validation_root"] = {"ok": False}
        status = cohort_freeze_status(checks)
        self.assertEqual(status["status"], "BLOCKED")
        self.assertEqual(status["blocked"], ["validation_root"])


if __name__ == "__main__":
    unittest.main()
