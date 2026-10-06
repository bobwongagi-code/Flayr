from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import flayr  # noqa: E402


class OcrProviderPolicyTests(unittest.TestCase):
    def args(self, **changes: object) -> SimpleNamespace:
        values = dict(
            vision_model="gemini-3.7-flash",
            vision_api_url="https://rootifyaiapi.rootifyglobal.com/v1/chat/completions",
            vision_api_key_env="FLAYR_LLM_API_KEY",
            llm_api_url="https://rootifyaiapi.rootifyglobal.com/v1/chat/completions",
            ocr_mode="on",
            llm_dry_run=False,
        )
        values.update(changes)
        return SimpleNamespace(**values)

    def test_approved_company_gemini_enables_ocr_on_and_auto(self) -> None:
        with mock.patch.object(flayr, "read_provider_api_key", return_value="test-key"):
            for mode in ("on", "auto"):
                with self.subTest(mode=mode):
                    self.assertEqual(
                        flayr.resolve_ocr_policy(self.args(ocr_mode=mode)), (True, "test-key", "")
                    )

    def test_missing_key_and_explicit_disable_remain_disabled(self) -> None:
        with mock.patch.object(flayr, "read_provider_api_key", return_value=""):
            self.assertEqual(flayr.resolve_ocr_policy(self.args())[2], "disabled_no_ocr_key")
        with mock.patch.object(flayr, "read_provider_api_key", return_value="test-key"):
            for change in ({"ocr_mode": "off"}, {"no_ocr": True}, {"llm_dry_run": True}):
                self.assertEqual(flayr.resolve_ocr_policy(self.args(**change))[2], "disabled_by_policy")

    def test_unapproved_model_or_endpoint_is_not_assumed_visual(self) -> None:
        with mock.patch.object(flayr, "read_provider_api_key", return_value="test-key"):
            for change in (
                {"vision_model": "gpt-6-sol"},
                {"vision_api_url": "https://example.test/v1/chat/completions"},
            ):
                self.assertEqual(
                    flayr.resolve_ocr_policy(self.args(**change))[2], "disabled_non_vision_config"
                )

    def test_existing_qwen_configuration_remains_enabled(self) -> None:
        with mock.patch.object(flayr, "read_provider_api_key", return_value="test-key"):
            self.assertTrue(flayr.resolve_ocr_policy(self.args(vision_model="qwen3-vl-plus"))[0])


if __name__ == "__main__":
    unittest.main()
