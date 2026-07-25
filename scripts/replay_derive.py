#!/usr/bin/env python3
"""Replay saved Flayr results through deterministic derive without API calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from flayr_core.offline_replay import (
    discover_analysis_inputs,
    read_analysis_input,
    replay_derive_result,
    replay_many,
)


def _read_result(path: Path) -> dict:
    return read_analysis_input(path)


def _ensure_output_outside_input_root(input_root: Path, output_root: Path) -> None:
    try:
        output_root.relative_to(input_root)
    except ValueError:
        return
    raise ValueError("replay output 不能位于 input-root 内，否则会在下一次运行中消费自身输出")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线重放 Flayr derive；不会调用任何模型或网络服务")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", action="append", type=Path, help="单个 analysis.json，可重复传入")
    group.add_argument("--input-root", type=Path, help="递归查找 analysis.json 的历史结果目录")
    parser.add_argument("--output", type=Path, required=True, help="单个输入时为 JSON 文件，多输入时为输出目录")
    args = parser.parse_args()

    if args.input:
        paths = [path.expanduser().resolve() for path in args.input]
    else:
        root = args.input_root.expanduser().resolve()
        output_root = args.output.expanduser().resolve()
        _ensure_output_outside_input_root(root, output_root)
        try:
            paths = discover_analysis_inputs(root)
        except NotADirectoryError:
            parser.error(f"input-root 不是目录：{root}")
        if not paths:
            parser.error(f"input-root 下没有找到 analysis.json：{root}")

    if len(paths) == 1 and args.input and args.output.suffix.lower() == ".json":
        output = args.output.expanduser().resolve()
        if output == paths[0].expanduser().resolve():
            parser.error("单输入 replay 的 output 不能覆盖 input")
        output.parent.mkdir(parents=True, exist_ok=True)
        replay = replay_derive_result(_read_result(paths[0]), paths[0])
        output.write_text(json.dumps(replay, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replay_summary = replay["offline_derive_replay"]["summary"]
        print(
            "replayed=1 "
            f"historical_final_changes={replay_summary['historical_final_to_replay']['changed_stage_count']} "
            f"model_baseline_changes={replay_summary['model_to_replay']['changed_stage_count']}"
        )
        print(f"output={output}")
        return 0

    output_root = args.output.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    report = replay_many(paths)
    for record in report["records"]:
        target = output_root / str(record["sample_id"]) / "analysis.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record["result"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path = output_root / "replay-summary.json"
    summary = {
        **report,
        "records": [
            {key: value for key, value in record.items() if key != "result"}
            for record in report["records"]
        ],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"replayed={report['summary']['inputs']} "
        f"historical_final_changes={report['summary']['historical_final_to_replay']['changed_stage_count']} "
        f"model_baseline_changes={report['summary']['model_to_replay']['changed_stage_count']}"
    )
    print(f"output={output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
