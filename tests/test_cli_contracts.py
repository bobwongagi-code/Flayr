"""Command-line contract regressions extracted from the architecture suite."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr
from flayr_core.llm import pipeline
from flayr_core.llm.api import provider_api_url


class CliContractTests(unittest.TestCase):
    def test_full_multimodal_analysis_is_the_cli_default(self) -> None:
        args = flayr.build_parser().parse_args(["compare", "--verification-stage", "production"])
        self.assertTrue(args.llm_include_images)
        legacy = flayr.build_parser().parse_args(
            ["--no-llm-include-images", "compare", "--verification-stage", "production"]
        )
        self.assertFalse(legacy.llm_include_images)

    def test_dual_model_route_is_explicit_and_legacy_route_stays_single_model(self) -> None:
        dual = flayr.build_parser().parse_args(
            [
                "compare",
                "--verification-stage",
                "production",
                "--judgment-model",
                "qwen3.7-plus",
                "--vision-model",
                "qwen3-vl-plus",
            ]
        )
        self.assertEqual(pipeline.judgment_model(dual), "qwen3.7-plus")
        self.assertEqual(pipeline.vision_model(dual), "qwen3-vl-plus")
        self.assertEqual(pipeline._stage1_model(dual, "A"), "qwen3-vl-plus")
        self.assertEqual(pipeline._stage1_model(dual, "B"), "qwen3.7-plus")
        self.assertEqual(pipeline._stage1_model(dual, "C"), "qwen3-vl-plus")

        legacy = flayr.build_parser().parse_args(
            [
                "compare",
                "--verification-stage",
                "production",
                "--llm-model",
                "qwen3.6-plus",
            ]
        )
        self.assertEqual(pipeline.judgment_model(legacy), "qwen3.6-plus")
        self.assertEqual(pipeline.vision_model(legacy), "qwen3.6-plus")

    def test_dual_model_cli_rejects_partial_or_mixed_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            common = [
                "compare",
                "--benchmark-video",
                str(benchmark),
                "--creator-video",
                str(creator),
                "--verification-stage",
                "production",
            ]
            partial = flayr.build_parser().parse_args(
                [*common, "--judgment-model", "qwen3.7-plus"]
            )
            with self.assertRaisesRegex(SystemExit, "requires both"):
                flayr.validate_inputs(partial)
            mixed = flayr.build_parser().parse_args(
                [
                    *common,
                    "--llm-model",
                    "qwen3.6-plus",
                    "--judgment-model",
                    "qwen3.7-plus",
                    "--vision-model",
                    "qwen3-vl-plus",
                ]
            )
            with self.assertRaisesRegex(SystemExit, "cannot be combined"):
                flayr.validate_inputs(mixed)
            retired = flayr.build_parser().parse_args(
                [
                    *common,
                    "--judgment-model",
                    "qwen3.7-plus",
                    "--vision-model",
                    "qwen3-vl-flash",
                ]
            )
            with self.assertRaisesRegex(SystemExit, "retired"):
                flayr.validate_inputs(retired)

    def test_cli_defaults_live_routes_to_the_company_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLAYR_LLM_API_URL", None)
            os.environ.pop("FLAYR_LLM_MODEL", None)
            os.environ.pop("FLAYR_JUDGMENT_MODEL", None)
            os.environ.pop("FLAYR_VISION_MODEL", None)
            os.environ.pop("FLAYR_LLM_API_KEY_ENV", None)
            os.environ.pop("FLAYR_VISION_API_URL", None)
            os.environ.pop("FLAYR_VISION_API_KEY_ENV", None)
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            args = flayr.build_parser().parse_args(
                [
                    "compare",
                    "--benchmark-video",
                    str(benchmark),
                    "--creator-video",
                    str(creator),
                    "--verification-stage",
                    "production",
                ]
            )
            flayr.validate_inputs(args)
            self.assertEqual(pipeline.judgment_model(args), "gpt-6-sol")
            self.assertEqual(pipeline.vision_model(args), "qwen3-vl-plus")
            self.assertEqual(
                args.llm_api_url,
                "https://rootifyaiapi.rootifyglobal.com/v1/chat/completions",
            )
            self.assertEqual(args.llm_api_key_env, "FLAYR_LLM_API_KEY")
            self.assertEqual(
                args.vision_api_url,
                "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
            )
            self.assertEqual(provider_api_url(args, "judgment"), args.llm_api_url)
            self.assertEqual(provider_api_url(args, "vision"), args.vision_api_url)

    def test_explicit_legacy_route_remains_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            for env_name in (
                "FLAYR_LLM_API_URL",
                "FLAYR_LLM_MODEL",
                "FLAYR_JUDGMENT_MODEL",
                "FLAYR_VISION_MODEL",
                "FLAYR_VISION_API_URL",
                "FLAYR_VISION_API_KEY_ENV",
            ):
                os.environ.pop(env_name, None)
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
            args = flayr.build_parser().parse_args(
                [
                    "compare",
                    "--benchmark-video",
                    str(benchmark),
                    "--creator-video",
                    str(creator),
                    "--verification-stage",
                    "production",
                    "--llm-model",
                    "qwen3-vl-plus",
                    "--llm-api-url",
                    endpoint,
                ]
            )
            flayr.validate_inputs(args)
            self.assertEqual(provider_api_url(args, "judgment"), endpoint)
            self.assertEqual(provider_api_url(args, "vision"), endpoint)

    def test_cli_preserves_offline_import_and_full_provider_replay_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            for env_name in (
                "FLAYR_LLM_API_URL",
                "FLAYR_JUDGMENT_MODEL",
                "FLAYR_VISION_MODEL",
                "FLAYR_LLM_API_KEY_ENV",
            ):
                os.environ.pop(env_name, None)
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            result = root / "analysis.json"
            replay = root / "replay"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            result.write_text("{}", encoding="utf-8")
            replay.mkdir()
            common = [
                "compare",
                "--benchmark-video",
                str(benchmark),
                "--creator-video",
                str(creator),
                "--verification-stage",
                "production",
            ]

            imported = flayr.build_parser().parse_args(
                [*common, "--analysis-result-json", str(result), "--legacy-import"]
            )
            flayr.validate_inputs(imported)
            self.assertIsNone(imported.judgment_model)
            self.assertIsNone(imported.vision_model)
            self.assertEqual(imported.llm_api_url, "")

            replayed = flayr.build_parser().parse_args(
                [*common, "--provider-replay-from", str(replay)]
            )
            flayr.validate_inputs(replayed)
            self.assertIsNone(replayed.judgment_model)
            self.assertIsNone(replayed.vision_model)
            self.assertEqual(replayed.llm_api_url, "")

    def test_cli_rejects_qwen_on_openai_endpoint_and_accepts_dashscope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            benchmark.write_bytes(b"benchmark")
            creator.write_bytes(b"creator")
            common = [
                "compare",
                "--benchmark-video",
                str(benchmark),
                "--creator-video",
                str(creator),
                "--verification-stage",
                "production",
                "--judgment-model",
                "qwen3.7-plus",
                "--vision-model",
                "qwen3-vl-plus",
            ]
            rejected = flayr.build_parser().parse_args(
                [*common, "--llm-api-url", "https://api.openai.com/v1/chat/completions"]
            )
            with self.assertRaisesRegex(SystemExit, "approved Qwen endpoint"):
                flayr.validate_inputs(rejected)

            accepted = flayr.build_parser().parse_args(
                [
                    *common,
                    "--llm-api-url",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                ]
            )
            self.assertEqual(set(flayr.validate_inputs(accepted)), {"benchmark", "creator"})

    def test_cli_requires_explicit_execution_intent(self) -> None:
        with self.assertRaises(SystemExit):
            flayr.build_parser().parse_args(["compare"])

    def test_legacy_text_entrypoint_is_explicitly_rejected(self) -> None:
        args = flayr.build_parser().parse_args(
            ["--no-llm-include-images", "compare", "--verification-stage", "production"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "text-only LLM"):
                pipeline.run_large_model_analysis(
                    args,
                    {},
                    root / "analysis_input.md",
                    root,
                )

    def test_external_analysis_import_requires_explicit_legacy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmark = root / "benchmark.mp4"
            creator = root / "creator.mp4"
            result = root / "analysis_result.json"
            benchmark.write_bytes(b"fixture")
            creator.write_bytes(b"fixture")
            result.write_text("{}", encoding="utf-8")
            args = flayr.build_parser().parse_args(
                [
                    "compare",
                    "--benchmark-video",
                    str(benchmark),
                    "--creator-video",
                    str(creator),
                    "--verification-stage",
                    "production",
                    "--analysis-result-json",
                    str(result),
                ]
            )
            with self.assertRaisesRegex(SystemExit, "--analysis-result-json"):
                flayr.validate_inputs(args)
            args = flayr.build_parser().parse_args(
                [
                    "compare",
                    "--benchmark-video",
                    str(benchmark),
                    "--creator-video",
                    str(creator),
                    "--verification-stage",
                    "production",
                    "--analysis-result-json",
                    str(result),
                    "--legacy-import",
                ]
            )
            self.assertEqual(set(flayr.validate_inputs(args)), {"benchmark", "creator"})

    def test_cli_does_not_accept_abbreviated_protected_network_flags(self) -> None:
        with self.assertRaises(SystemExit):
            flayr.build_parser().parse_args(["compare", "--llm-api-u", "https://attacker.invalid"])


if __name__ == "__main__":
    unittest.main()
