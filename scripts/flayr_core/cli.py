"""Command-line parser for Flayr."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .asr import DEFAULT_FUN_ASR_API_URL, DEFAULT_FUN_ASR_MODEL
from .market import normalize_target_market
from .resources import ResourceLimits


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze and improve TikTok commerce short videos.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "mode",
        choices=("breakdown", "compare", "improve", "scope"),
        help="Run mode.",
    )
    parser.add_argument("--benchmark-video", type=Path, help="Benchmark video path.")
    parser.add_argument("--creator-video", type=Path, help="Creator video path.")
    parser.add_argument("--product-name", default="未填写", help="Product name.")
    parser.add_argument(
        "--proposition-key",
        default="",
        help="Explicit key in references/brand_propositions.json. Never inferred from an online run directory.",
    )
    parser.add_argument("--product-category", default="", help="Product category from the structure-library category set.")
    parser.add_argument(
        "--comparison-scope-override",
        choices=("same_task_structure",),
        help=(
            "运营确认的比较关系覆盖。same_task_structure 仅用于不同产品但共享消费者任务且具有替代关系的情况；"
            "各阶段仍由 stage_eligibility 单独判断，不自动开放固定阶段。"
        ),
    )
    parser.add_argument("--product-price", default="未填写", help="Product price.")
    parser.add_argument(
        "--product-tier",
        choices=("low", "mid", "high"),
        default=None,
        help="运营提供的客单价档（以 TikTok Shop 同品类为参照：low 走量/mid 主流/high 类目内溢价）。"
        "提供则覆盖模型对 price_tier 的世界知识判断（运营领域知识更可靠）；不提供则用模型判断兜底。",
    )
    parser.add_argument(
        "--target-market",
        type=normalize_target_market,
        default="auto",
        help="Target market: auto, sea, or a two-letter SEA market code (for example my, th, id). Only my loads Malaysia-specific rules.",
    )
    parser.add_argument("--core-selling-points", default="", help="Verified product selling points and differentiation.")
    parser.add_argument(
        "--primary-selling-point",
        default="",
        help="Operator-approved primary commercial selling point for this video route.",
    )
    parser.add_argument("--target-user", default="", help="Target audience profile and core pain point.")
    parser.add_argument(
        "--purchase-motivation",
        choices=("MO-解决问题", "MO-提升体验", "MO-情感满足", "MO-刚需补货"),
        help="Target user's primary purchase motivation.",
    )
    parser.add_argument("--creator-profile", default="", help="Optional creator account style or performance baseline.")
    parser.add_argument(
        "--product-notes",
        default="",
        help="Optional selling points, target user, or other product notes.",
    )
    parser.add_argument("--output-dir", type=Path, help="Output run directory.")
    parser.add_argument(
        "--max-total-wall-time",
        type=float,
        default=ResourceLimits().max_total_wall_time,
        help=(
            "单次运行总墙钟上限（秒），默认 1800；只影响本次 run 的资源预算，"
            "不改变单个 HTTP 请求的 timeout。慢模型验证可显式提高。"
        ),
    )
    parser.add_argument(
        "--max-llm-calls",
        type=int,
        default=ResourceLimits().max_llm_calls,
        help=(
            "本次运行允许的真实 LLM 网络尝试上限，默认 32；重放命中不计数。"
            "恢复运行可显式收紧，防止局部失效意外扩散成整链重跑。"
        ),
    )
    parser.add_argument(
        "--reuse-preprocessing",
        action="store_true",
        help=(
            "复用 --output-dir 中已有的预处理（抽帧/转写/镜头轨/字幕轨），跳过重抽。"
            "用于实验迭代（同视频改 prompt/代码重跑）和 LLM 失败后补跑，大幅省时。"
        ),
    )
    parser.add_argument(
        "--asr-language",
        dest="asr_language",
        default="auto",
        help="Speech language hint passed to online Fun-ASR. Default: auto.",
    )
    parser.add_argument(
        "--asr-api-url",
        default=DEFAULT_FUN_ASR_API_URL,
        help="Online Fun-ASR endpoint. Defaults to the approved Beijing MaaS endpoint.",
    )
    parser.add_argument(
        "--asr-model",
        default=os.environ.get("FLAYR_ASR_MODEL", DEFAULT_FUN_ASR_MODEL),
        help="Online ASR model. Default: fun-asr-flash-2026-06-15.",
    )
    parser.add_argument(
        "--asr-api-key-env",
        default="DASHSCOPE_API_KEY",
        help="Environment variable for the Qwen/DashScope ASR key; only the same approved Qwen endpoint may provide the fallback.",
    )
    parser.add_argument(
        "--analysis-result-json",
        type=Path,
        help=(
            "Optional historical analysis JSON. It is accepted only with --legacy-import and is "
            "always published as audit-only degraded output."
        ),
    )
    parser.add_argument(
        "--legacy-import",
        action="store_true",
        help="Explicitly import --analysis-result-json as legacy audit data; never as a current completed run.",
    )
    stage2_reuse = parser.add_mutually_exclusive_group()
    stage2_reuse.add_argument(
        "--stage2-replay-from",
        type=Path,
        help=(
            "Strictly replay matching completed Stage2 provider artifacts from another run. "
            "Never calls the Stage2 provider when an artifact is missing or its request identity changed."
        ),
    )
    stage1_reuse = parser.add_mutually_exclusive_group()
    stage1_reuse.add_argument(
        "--stage1-replay-from",
        type=Path,
        help=(
            "严格重放匹配的 Stage1-A/B/C provider artifact；缺失、损坏或请求身份变化时直接失败，"
            "绝不调用 Stage1 provider。"
        ),
    )
    stage1_reuse.add_argument(
        "--stage1-resume-from",
        type=Path,
        help=(
            "优先复用匹配的 Stage1-A/B/C provider artifact；仅对缺失、失败或语义变化的阶段调用 provider。"
        ),
    )
    stage2_reuse.add_argument(
        "--stage2-resume-from",
        type=Path,
        help=(
            "Reuse matching completed Stage2 provider artifacts from another run and call the provider "
            "only for missing, failed, or semantically changed groups."
        ),
    )
    parser.add_argument(
        "--llm-model",
        help=(
            "Legacy single-model route. The same model performs visual evidence extraction and "
            "judgment. Cannot be combined with --judgment-model/--vision-model."
        ),
    )
    parser.add_argument(
        "--judgment-model",
        help="Model for Step-0, Stage1-B, Stage2/Stage3 and other text/world-knowledge judgments.",
    )
    parser.add_argument(
        "--vision-model",
        help="Model for OCR, Stage1-A, Stage1-C and Phase C visual/native-video work.",
    )
    parser.add_argument(
        "--llm-api-url",
        default=os.environ.get("FLAYR_LLM_API_URL", "").strip(),
        help="Approved-provider Chat Completions endpoint; only allowlisted official domains are accepted.",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable that contains the LLM API key.",
    )
    parser.add_argument(
        "--llm-api-key-keychain-service",
        help="macOS Keychain generic-password service used to read the LLM API key.",
    )
    parser.add_argument(
        "--llm-api-key-keychain-account",
        default="API_KEY",
        help="macOS Keychain account used with --llm-api-key-keychain-service. Default: API_KEY.",
    )
    parser.add_argument(
        "--llm-dry-run",
        action="store_true",
        help="Write the LLM request payload without calling the API.",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help=(
            "Allow compare/improve to proceed without completed LLM or online ASR analysis. "
            "Without this flag, missing analysis exits non-zero. "
            "When set, the run is marked degraded and no success manifest is written."
        ),
    )
    parser.add_argument(
        "--provider-replay-from",
        type=Path,
        help=(
            "Strictly replay provider artifacts (including ASR, Step-0, Stage1, "
            "Stage2/Stage3, Phase C and postprocess). "
            "A missing or mismatched artifact never falls back to a live provider call."
        ),
    )
    parser.add_argument(
        "--verification-stage",
        choices=("production", "fixture", "offline_replay", "fake_provider", "ordinary_sample", "boundary_sample"),
        required=True,
        help=(
            "Execution intent. Use production for normal product runs; evaluation stages require "
            "the frozen prerequisite markers."
        ),
    )
    parser.add_argument(
        "--verification-root",
        type=Path,
        help="Directory containing passed verification-order markers.",
    )
    parser.add_argument(
        "--llm-include-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Use the full Step-0 + per-video fact extraction + multimodal comparison pipeline. "
            "Enabled by default; --no-llm-include-images is retained only as a rejected legacy flag."
        ),
    )
    parser.add_argument(
        "--absolute-execution-shadow",
        action="store_true",
        help=(
            "额外对两侧视频分别运行 S1-S4 单侧绝对执行审计；仅写 shadow 结果，"
            "不改变 severity。用于校准和检测跨配对锚定漂移。"
        ),
    )
    parser.add_argument(
        "--llm-image-limit",
        type=int,
        default=12,
        help="Maximum visual inputs attached to each per-video fact request. Default: 12.",
    )
    parser.add_argument(
        "--translate-with-llm",
        action="store_true",
        help="Translate local-language transcripts to Chinese with the configured LLM provider.",
    )
    parser.add_argument(
        "--translation-model",
        help="Optional model for transcript translation. Defaults to the judgment model.",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "Subtitle OCR mode. auto reuses the configured multimodal LLM when an API key "
            "is available and this is not --llm-dry-run; on forces OCR; off disables OCR."
        ),
    )
    parser.add_argument(
        "--with-ocr",
        action="store_true",
        help="Backward-compatible alias for --ocr-mode on.",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Backward-compatible alias for --ocr-mode off.",
    )
    return parser
