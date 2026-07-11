"""Transcript translation helpers for Flayr."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .llm.api import call_llm_api, extract_chat_completion_text, read_llm_api_key
from .utils import read_optional_text, write_json, write_text


ROOT = Path(__file__).resolve().parents[2]


def sync_chinese_translation(role_dir: Path, result: dict[str, Any]) -> None:
    translation_path = role_dir / "transcript.zh.txt"
    result["translation_path"] = str(translation_path)

    if translation_path.exists() and translation_path.read_text(encoding="utf-8").strip():
        result["translation_status"] = "completed"
        return

    write_text(
        translation_path,
        "待翻译：请基于 transcript.txt 输出中文翻译，保留原文顺序，并遵循 references/commerce-translation-guidelines.md 的电商口播翻译规则。\n",
    )
    result["translation_status"] = "pending"


def translate_transcript_with_llm(
    args: argparse.Namespace,
    role: str,
    role_dir: Path,
    result: dict[str, Any],
) -> None:
    transcript_path = role_dir / "transcript.txt"
    translation_path = role_dir / "transcript.zh.txt"
    transcript = read_optional_text(transcript_path).strip()
    if not transcript or transcript in {"（缺失）", "（空）"}:
        result["translation_status"] = "skipped_no_transcript"
        return

    model = args.translation_model or args.llm_model
    if not model:
        result["errors"].append("translation skipped: --translate-with-llm requires --translation-model or --llm-model")
        return

    payload = build_translation_payload(
        model,
        transcript,
        result,
        product_name=str(args.product_name or "").strip(),
        product_notes=str(args.product_notes or "").strip(),
    )
    payload_path = role_dir / "translation_request.json"
    raw_path = role_dir / "translation_response.json"
    write_json(payload_path, payload)
    if args.llm_dry_run:
        result["translation_status"] = "dry_run"
        return

    try:
        api_key = read_llm_api_key(args).strip()
        if not api_key:
            result["translation_status"] = "failed"
            result["errors"].append("translation skipped: LLM API key missing")
            return

        raw_text = call_llm_api(args.llm_api_url, api_key, payload_path, raw_path)
        write_text(raw_path, raw_text)
        translated = extract_chat_completion_text(json.loads(raw_text)).strip()
        translated = sanitize_translation_claims(translated, str(args.product_name or ""))
    except SystemExit as exc:
        result["translation_status"] = "failed"
        result["errors"].append(f"translation failed: {str(exc)[:200]}")
        return
    except Exception as exc:  # noqa: BLE001 — 可选翻译失败不得中断主分析。
        result["translation_status"] = "failed"
        result["errors"].append(f"translation failed: {str(exc)[:200]}")
        return

    if not translated:
        result["translation_status"] = "failed"
        result["errors"].append("translation failed: empty LLM output")
        return

    write_text(translation_path, translated.rstrip() + "\n")
    result["translation_status"] = "completed"
    result["translation_source"] = {
        "type": "llm",
        "model": model,
        "role": role,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def sanitize_translation_claims(translated: str, product_name: str) -> str:
    if "儿童牙膏" not in product_name:
        return translated
    text = translated.replace("洁白又美味", "“putih delight”（原词，含义待核）")
    text = re.sub(r"美白(?:效果|功能|作用|含义)?", "未核验的描述", text)
    return text


def build_translation_payload(
    model: str,
    transcript: str,
    video_info: dict[str, Any],
    product_name: str = "",
    product_notes: str = "",
) -> dict[str, Any]:
    guidelines = read_optional_text(ROOT / "references" / "commerce-translation-guidelines.md")
    system_prompt = read_optional_text(ROOT.parent / "AirTranslate" / "Resources" / "TranslationSystemPrompt.md")
    detected_language = video_info.get("detected_language") or video_info.get("transcription_language") or "auto"
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是面向中国电商运营团队的东南亚 TikTok Shop 口播翻译器。"
                    "只输出中文译文，不要解释，不要 Markdown。"
                    "保留原文顺序，按自然口播断行。"
                    "遇到商品、优惠、购买动作和 TikTok Shop 术语时，优先按电商场景翻译。"
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(
                    [
                        f"检测语言：{detected_language}",
                        f"产品上下文（用户给定）：{product_name or '未提供'}",
                        f"补充约束：{product_notes or '无'}",
                        "商品类型相关词若听写含混，必须与用户给定产品上下文一致；不确定时使用中性指代，不要翻成冲突的品类。",
                        "儿童口腔用品中，不得把不确定的描述扩写为“美味”、可食用或医疗功效暗示；不确定词保守保留原词或译为中性描述。",
                        "AirTranslate 翻译提示参考：",
                        system_prompt,
                        "Flayr 电商术语规则：",
                        guidelines,
                        "待翻译口播：",
                        transcript,
                    ]
                ),
            },
        ],
        "temperature": 0.1,
    }
