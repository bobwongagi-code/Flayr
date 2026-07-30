#!/usr/bin/env python3
"""冻结、校验或消费 blind validation cohort；不调用视频分析或模型。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.validation_cohort import (
    build_cohort_lock,
    read_json,
    spend_cohort_lock,
    verify_cohort_lock,
)


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="管理 Flayr blind validation cohort")
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze = subparsers.add_parser("freeze", help="冻结新 blind cohort")
    freeze.add_argument("--labels", type=Path, default=Path("references/ground-truth-labels.json"))
    freeze.add_argument("--manifest", type=Path, default=Path("references/validation-inputs.json"))
    freeze.add_argument("--sample", action="append", required=True, help="blind sample id，可重复")
    freeze.add_argument("--provider", required=True)
    freeze.add_argument("--model", required=True)
    freeze.add_argument("--api-url", required=True)
    freeze.add_argument("--fallback-model", default=None)
    freeze.add_argument("--temperature", type=float, default=0.0)
    # Qwen3.6 Plus uses max_completion_tokens=65536 for the full contract;
    # this field records the equivalent output ceiling in the freeze manifest.
    freeze.add_argument("--max-tokens", type=int, default=65536)
    freeze.add_argument("--top-p", type=float, default=None)
    freeze.add_argument("--seed", type=int, default=None)
    freeze.add_argument("--response-format", type=json.loads, default=None, help="JSON object，例如 '{\"type\":\"json_object\"}'")
    freeze.add_argument("--stop", action="append", default=None, help="停止序列，可重复")
    freeze.add_argument("--transport-retry", type=int, default=2)
    freeze.add_argument("--completion-attempts", type=int, default=3)
    freeze.add_argument("--connect-timeout", type=int, default=30)
    freeze.add_argument("--read-timeout", type=int, default=None)
    freeze.add_argument("--low-speed-timeout", type=int, default=180)
    freeze.add_argument("--overall-timeout", type=int, default=1800)
    freeze.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="校验 cohort 内容是否漂移")
    verify.add_argument("lock", type=Path)

    spend = subparsers.add_parser("spend", help="结果已打开或用于改规则，标记 cohort 已消耗")
    spend.add_argument("lock", type=Path)
    spend.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "freeze":
        lock = build_cohort_lock(
            ROOT,
            args.labels,
            args.manifest,
            args.sample,
            {
                "schema_version": 1,
                "provider": args.provider,
                "api_url": args.api_url,
                "model": args.model,
                "fallback_model": args.fallback_model,
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
                "top_p": args.top_p,
                "seed": args.seed,
                "response_format": args.response_format,
                "stop": args.stop,
                "transport_retry": args.transport_retry,
                "completion_attempts": args.completion_attempts,
                "timeout": {
                    "connect": args.connect_timeout,
                    "read": args.read_timeout,
                    "low_speed": args.low_speed_timeout,
                    "overall": args.overall_timeout,
                },
            },
        )
        write_json_atomic(args.output, lock)
        print(f"frozen={len(lock['sample_ids'])} output={args.output}")
        return 0
    lock = read_json(args.lock)
    if args.command == "verify":
        errors = verify_cohort_lock(lock)
        print(json.dumps({"valid": not errors, "status": lock.get("status"), "errors": errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    updated = spend_cohort_lock(lock, args.reason)
    write_json_atomic(args.lock, updated)
    print(f"status=spent output={args.lock}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
