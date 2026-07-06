#!/usr/bin/env python3
"""S1 B2 多样本矩阵验收 runner。

策略：
- 跨样本并发，同一样本内部串行，避免 dev_stage2_result_XX 互相覆盖。
- 首轮每样本 repeat=2；S1 不一致或成功数不足时补到 repeat=3。
- 审计只读取 dev_stage2_stability.json 中本次成功记录，避免旧文件污染。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SAMPLES = [
    "runs/sample-are_xie",
    "runs/sample-bluetoothwanju",
    "runs/sample-carslan-b0",
    "runs/sample-colorkey-b0",
    "runs/sample-kakwanreview",
    "runs/sample-skincare",
    "runs/sample-tashadiyana",
    "runs/sample-youkoubo-c2",
]


def run_command(cmd: list[str], log_path: Path, timeout_seconds: int) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n\n$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                code = proc.wait()
            log.write(f"\n[timeout] killed after {timeout_seconds}s\n")
            return 124
        log.write(f"\n[exit] {code}\n")
        return code


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_s1(result: dict[str, Any]) -> dict[str, Any]:
    for stage in result.get("stage_analysis") or []:
        if isinstance(stage, dict) and str(stage.get("stage") or "").startswith("S1"):
            return stage
    return {}


def agreement(values: list[Any]) -> tuple[str, float]:
    vals = [str(v) for v in values if v is not None]
    if not vals:
        return "", 0.0
    top, count = Counter(vals).most_common(1)[0]
    return top, round(count / len(vals), 2)


def hook_summary(hooks: list[dict[str, Any]]) -> dict[str, Any]:
    if not hooks:
        return {}
    dims = {}
    for key in ("camera", "copy", "sound", "rhythm"):
        _, ratio = agreement([(hook.get("dims") or {}).get(key) for hook in hooks])
        dims[key] = ratio
    leak_count = sum(1 for hook in hooks if hook.get("landing_window_leak") is True)
    type_mode, type_agree = agreement([hook.get("type") for hook in hooks])
    exists_mode, exists_agree = agreement([hook.get("exists") for hook in hooks])
    landing_mode, landing_agree = agreement([hook.get("landing_met") for hook in hooks])
    boundary_values = [
        float(value)
        for hook in hooks
        for value in [hook.get("hook_boundary_seconds")]
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    boundary_mode, boundary_agree = agreement(boundary_values)
    boundary_span = round(max(boundary_values) - min(boundary_values), 2) if len(boundary_values) >= 2 else 0.0
    anchors_mode, anchors_agree = agreement([hook.get("anchors_proposition") for hook in hooks])
    return {
        "type": {"mode": type_mode, "agreement": type_agree},
        "exists": {"mode": exists_mode, "agreement": exists_agree},
        "landing": {"mode": landing_mode, "agreement": landing_agree},
        "boundary": {
            "mode": boundary_mode,
            "agreement": boundary_agree,
            "span_seconds": boundary_span,
            "jitter_tolerated": boundary_span <= 1.0,
        },
        "dims_agreement": dims,
        "anchors": {"mode": anchors_mode, "agreement": anchors_agree},
        "leak_rate": round(leak_count / len(hooks), 2),
    }


def analyze_run(run_dir: Path) -> dict[str, Any]:
    stability = load_json(run_dir / "dev_stage2_stability.json")
    records = [record for record in stability.get("records") or [] if isinstance(record, dict)]
    ok_records = [record for record in records if record.get("ok")]
    s1_values: list[str] = []
    creator_hooks: list[dict[str, Any]] = []
    benchmark_hooks: list[dict[str, Any]] = []
    result_paths: list[str] = []

    for record in ok_records:
        result_path = Path(str(record.get("result_path") or ""))
        if not result_path.is_file():
            continue
        result_paths.append(str(result_path))
        result = load_json(result_path)
        s1 = get_s1(result)
        s1_values.append(str(s1.get("severity") or ""))
        for side, store in (("creator", creator_hooks), ("benchmark", benchmark_hooks)):
            hook = s1.get(f"{side}_hook")
            if isinstance(hook, dict):
                store.append(hook)

    severity_mode, severity_agree = agreement(s1_values)
    creator = hook_summary(creator_hooks)
    benchmark = hook_summary(benchmark_hooks)
    issues = classify_issues(
        requested=len(records),
        successful=len(ok_records),
        s1_values=s1_values,
        creator=creator,
        benchmark=benchmark,
    )
    return {
        "run_dir": str(run_dir),
        "requested": len(records),
        "successful": len(ok_records),
        "s1_severity": s1_values,
        "s1_severity_mode": severity_mode,
        "s1_severity_agreement": severity_agree,
        "creator": creator,
        "benchmark": benchmark,
        "issues": issues,
        "result_paths": result_paths,
    }


def classify_issues(
    requested: int,
    successful: int,
    s1_values: list[str],
    creator: dict[str, Any],
    benchmark: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    if successful < min(2, requested):
        issues.append("api_failure")
    if len(set(s1_values)) > 1:
        issues.append("s1_severity_unstable")
    for side_name, side in (("creator", creator), ("benchmark", benchmark)):
        if not side:
            issues.append(f"{side_name}_hook_missing")
            continue
        boundary = side.get("boundary") or {}
        if boundary.get("agreement", 0) < 0.67 and not boundary.get("jitter_tolerated"):
            issues.append(f"{side_name}_boundary_unstable")
        if side.get("landing", {}).get("agreement", 0) < 0.67:
            issues.append(f"{side_name}_landing_unstable")
        if side.get("exists", {}).get("agreement", 0) < 0.67:
            issues.append(f"{side_name}_exists_unstable")
        if side.get("anchors", {}).get("agreement", 0) < 0.67:
            issues.append(f"{side_name}_anchors_unstable")
        weak_dims = [
            dim for dim, ratio in (side.get("dims_agreement") or {}).items()
            if ratio < 0.67
        ]
        if weak_dims:
            issues.append(f"{side_name}_dims_unstable:{','.join(weak_dims)}")
    return issues


def needs_topup(summary: dict[str, Any], target_repeat: int) -> bool:
    if summary.get("successful", 0) < 2:
        return True
    if len(summary.get("s1_severity") or []) < target_repeat:
        return True
    return bool(summary.get("issues"))


def run_sample(
    run_dir: Path,
    repeat: int,
    retries: int,
    log_dir: Path,
    reuse_existing: bool,
    skip_existing: bool = False,
    timeout_seconds: int = 1500,
) -> dict[str, Any]:
    log_path = log_dir / f"{run_dir.name}.log"
    missing = [
        name for name in ("analysis.json", "analysis_input.md", "video_facts_benchmark.json", "video_facts_creator.json")
        if not (run_dir / name).is_file()
    ]
    if missing:
        summary = {
            "run_dir": str(run_dir),
            "requested": repeat,
            "successful": 0,
            "s1_severity": [],
            "issues": [f"missing_artifacts:{','.join(missing)}"],
            "result_paths": [],
            "command_exit": 1,
            "log_path": str(log_path),
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"missing artifacts: {', '.join(missing)}\n", encoding="utf-8")
        return summary
    cmd = [
        sys.executable,
        "scripts/dev_test_stage2.py",
        str(run_dir),
        "--repeat",
        str(repeat),
        "--retries",
        str(retries),
    ]
    if reuse_existing:
        cmd.append("--reuse-existing")
    if skip_existing:
        cmd.append("--skip-existing")
    code = run_command(cmd, log_path, timeout_seconds)
    summary = analyze_run(run_dir)
    summary["command_exit"] = code
    summary["log_path"] = str(log_path)
    return summary


def write_markdown(path: Path, data: dict[str, Any]) -> None:
    lines = [
        "# S1 B2 Matrix Summary",
        "",
        f"- created_at: {data.get('created_at')}",
        f"- jobs: {data.get('jobs')}",
        f"- repeat_initial: {data.get('repeat_initial')}",
        f"- repeat_topup: {data.get('repeat_topup')}",
        "",
        "| sample | ok | S1 severity | creator boundary/landing | benchmark boundary/landing | issues |",
        "|---|---:|---|---|---|---|",
    ]
    for item in data.get("samples") or []:
        creator = item.get("creator") or {}
        benchmark = item.get("benchmark") or {}
        c = f"{creator.get('boundary', {}).get('mode','')}({creator.get('boundary', {}).get('agreement',0)}) / {creator.get('landing', {}).get('mode','')}({creator.get('landing', {}).get('agreement',0)})"
        b = f"{benchmark.get('boundary', {}).get('mode','')}({benchmark.get('boundary', {}).get('agreement',0)}) / {benchmark.get('landing', {}).get('mode','')}({benchmark.get('landing', {}).get('agreement',0)})"
        lines.append(
            "| {sample} | {ok} | {sev} | {creator} | {benchmark} | {issues} |".format(
                sample=Path(str(item.get("run_dir"))).name,
                ok=item.get("successful", 0),
                sev=", ".join(item.get("s1_severity") or []),
                creator=c,
                benchmark=b,
                issues=", ".join(item.get("issues") or []),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", nargs="*", help="run dirs; default uses baseline matrix")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--topup-repeat", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--sample-timeout", type=int, default=1500, help="per-sample subprocess timeout in seconds")
    parser.add_argument("--reuse-existing", action="store_true", help="reuse raw results instead of calling LLM")
    parser.add_argument("--out-json", default="runs/s1_b2_matrix_summary.json")
    parser.add_argument("--out-md", default="runs/s1_b2_matrix_summary.md")
    parser.add_argument("--log-dir", default="runs/s1_b2_matrix_logs")
    args = parser.parse_args()

    sample_paths = [Path(item) for item in (args.samples or DEFAULT_SAMPLES)]
    log_dir = Path(args.log_dir)
    print(f"[matrix] samples={len(sample_paths)} jobs={args.jobs} repeat={args.repeat}", flush=True)

    first_pass: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
                executor.submit(run_sample, path, args.repeat, args.retries, log_dir, args.reuse_existing, False, args.sample_timeout): path
            for path in sample_paths
        }
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                summary = future.result()
            except Exception as exc:  # pragma: no cover - dev tool should keep batch going
                summary = {"run_dir": str(path), "successful": 0, "issues": [f"runner_error:{exc}"]}
            first_pass.append(summary)
            print(f"[done] {path.name}: S1={summary.get('s1_severity')} issues={summary.get('issues')}", flush=True)

    topup_targets = [
        Path(item["run_dir"])
        for item in first_pass
        if needs_topup(item, args.repeat)
    ]
    topup: list[dict[str, Any]] = []
    if topup_targets and args.topup_repeat > args.repeat:
        print(f"[topup] {len(topup_targets)} samples -> repeat={args.topup_repeat}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {
                executor.submit(run_sample, path, args.topup_repeat, args.retries, log_dir, False, True, args.sample_timeout): path
                for path in topup_targets
            }
            for future in concurrent.futures.as_completed(futures):
                path = futures[future]
                try:
                    summary = future.result()
                except Exception as exc:  # pragma: no cover
                    summary = {"run_dir": str(path), "successful": 0, "issues": [f"runner_error:{exc}"]}
                topup.append(summary)
                print(f"[topup done] {path.name}: S1={summary.get('s1_severity')} issues={summary.get('issues')}", flush=True)

    by_run = {item["run_dir"]: item for item in first_pass}
    by_run.update({item["run_dir"]: item for item in topup})
    samples = [by_run.get(str(path), {"run_dir": str(path), "issues": ["missing"]}) for path in sample_paths]
    output = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "jobs": args.jobs,
        "repeat_initial": args.repeat,
        "repeat_topup": args.topup_repeat,
        "retries": args.retries,
        "sample_timeout": args.sample_timeout,
        "samples": samples,
    }
    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(out_md, output)
    print(f"[summary] {out_json}")
    print(f"[summary] {out_md}")


if __name__ == "__main__":
    main()
