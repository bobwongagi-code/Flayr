#!/usr/bin/env python3
"""Local requirement evidence review. Never starts video or model processing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from flayr_core.human_review import HumanReviewError  # noqa: E402
from flayr_core.requirement_review import init_packet, load_packet, record_note, render_packet  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读已有证据，集中核对明确要求；不自动评分或验收")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("run_dir", type=Path)
    create.add_argument("--requirements", type=Path, required=True)
    create.add_argument("--links", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    show = commands.add_parser("show")
    show.add_argument("packet", type=Path)
    note = commands.add_parser("note")
    note.add_argument("packet", type=Path)
    note.add_argument("--requirement-id", required=True)
    note.add_argument("--reviewer", required=True)
    note.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            packet = init_packet(args.run_dir, args.requirements, args.links, args.output)
        elif args.command == "note":
            packet = record_note(args.packet, args.requirement_id, args.reviewer, args.note)
        else:
            packet = load_packet(args.packet)
        print(render_packet(packet))
        return 0
    except (HumanReviewError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"要求核对失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
