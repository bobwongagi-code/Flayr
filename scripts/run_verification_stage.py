#!/usr/bin/env python3
"""Run one frozen verification stage and persist evidence-backed proof."""

from __future__ import annotations

import argparse
from pathlib import Path

from flayr_core.verification_order import run_verification_stage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    run_verification_stage(
        args.root,
        args.stage,
        command=command,
        evidence_paths=args.evidence,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
