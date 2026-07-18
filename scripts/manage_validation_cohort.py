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
    freeze.add_argument("--model", required=True)
    freeze.add_argument("--api-url", required=True)
    freeze.add_argument("--temperature", type=float, default=0.0)
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
            {"model": args.model, "api_url": args.api_url, "temperature": args.temperature},
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
