#!/usr/bin/env python3
"""Flayr 批量分析 runner：脱离 harness 跑大量视频对，支持断点续跑、故障隔离、限并发。

为什么需要：harness 跟踪的后台任务会 cull 长时/无输出的进程；本 runner 设计为用
nohup 脱离 harness 跑一次，进度写状态文件，由外部廉价轮询查看，不依赖后台通知。

用法：
  nohup python3 scripts/batch_analyze.py jobs.json --concurrency 1 \
      >runs/_batch/runner.log 2>&1 &
  # 查进度：
  cat runs/_batch/status.json

jobs.json 格式：
{
  "common_args": ["--llm-model","qwen3.5-omni-plus","--llm-api-url","...",
                  "--llm-api-key-keychain-service","VidLingo.Qwen",
                  "--llm-include-images","--llm-image-limit","12","--target-market","my"],
  "jobs": [
    {"name":"are_xie","creator":"/abs/creator.mp4","benchmark":"/abs/bench.mp4",
     "args":["--product-category","..."]}   # args 可选，覆盖/追加该 job 的参数
  ]
}

约定：
- 每个 job 输出到 runs/sample-<name>（或 job["output_dir"]）。
- 已有 analysis_result.json 的 job 自动跳过（断点续跑）；失败的 job 没有该文件，
  重新启动 runner 会自动重试它。
- 自动加 --reuse-preprocessing：同一 output-dir 重跑时复用已有抽帧/转写/轨，省时。
- 一个 job 失败只标 failed，不影响其余 job。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def write_status(path: Path, status: dict) -> None:
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def job_output_dir(job: dict, runs_dir: Path) -> Path:
    return Path(job["output_dir"]) if job.get("output_dir") else runs_dir / f"sample-{job['name']}"


def build_command(job: dict, out_dir: Path, common_args: list[str]) -> list[str]:
    command = [
        "python3",
        str(ROOT / "scripts" / "flayr.py"),
        "improve",
        "--creator-video",
        job["creator"],
        "--benchmark-video",
        job["benchmark"],
        "--output-dir",
        str(out_dir),
        "--reuse-preprocessing",
        *common_args,
        *job.get("args", []),
    ]
    proposition_key = str(job.get("proposition_key") or "").strip()
    if proposition_key:
        command.extend(["--proposition-key", proposition_key])
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description="Flayr 批量分析 runner")
    parser.add_argument("jobs_file", help="作业清单 JSON")
    parser.add_argument("--concurrency", type=int, default=1, help="并发作业数，默认 1（顺序）")
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"))
    args = parser.parse_args()

    spec = json.loads(Path(args.jobs_file).read_text(encoding="utf-8"))
    common_args = spec.get("common_args", [])
    jobs = spec["jobs"]
    runs_dir = Path(args.runs_dir)
    batch_dir = runs_dir / "_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    status_path = batch_dir / "status.json"

    # 防重复启动：已有存活 runner 时拒绝，避免两个 runner 跑同一 output-dir 互相覆写。
    lock_path = batch_dir / "runner.lock"
    if lock_path.is_file():
        old = lock_path.read_text(encoding="utf-8").strip()
        if old.isdigit() and _pid_alive(int(old)):
            print(f"[batch] 已有 runner 在跑 (pid {old})，拒绝重复启动；如确认其已死可删 {lock_path}。")
            return 1
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        return _run_jobs(jobs, common_args, runs_dir, status_path, args.concurrency)
    finally:
        lock_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权发信号 = 存活


def _run_jobs(
    jobs: list[dict],
    common_args: list[str],
    runs_dir: Path,
    status_path: Path,
    concurrency: int,
) -> int:
    batch_dir = status_path.parent
    status: dict = {"started": now(), "concurrency": concurrency, "jobs": {}}
    for job in jobs:
        out = job_output_dir(job, runs_dir)
        done = (out / "analysis_result.json").is_file()
        status["jobs"][job["name"]] = {"state": "done" if done else "pending", "output_dir": str(out)}
    write_status(status_path, status)

    pending = [job for job in jobs if status["jobs"][job["name"]]["state"] != "done"]
    running: list[tuple[dict, subprocess.Popen, object]] = []
    idx = 0

    def launch(job: dict) -> None:
        out = job_output_dir(job, runs_dir)
        log_path = batch_dir / f"{job['name']}.log"
        log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — 进程存活期间需保持打开
        proc = subprocess.Popen(
            build_command(job, out, common_args), stdout=log_file, stderr=subprocess.STDOUT
        )
        status["jobs"][job["name"]].update({"state": "running", "started": now(), "log": str(log_path)})
        write_status(status_path, status)
        running.append((job, proc, log_file))

    while idx < len(pending) or running:
        while idx < len(pending) and len(running) < max(1, concurrency):
            launch(pending[idx])
            idx += 1
        time.sleep(5)
        for entry in list(running):
            job, proc, log_file = entry
            if proc.poll() is None:
                continue
            log_file.close()
            out = job_output_dir(job, runs_dir)
            ok = (out / "analysis_result.json").is_file()
            status["jobs"][job["name"]].update(
                {"state": "done" if ok else "failed", "rc": proc.returncode, "ended": now()}
            )
            write_status(status_path, status)
            running.remove(entry)

    done_n = sum(1 for item in status["jobs"].values() if item["state"] == "done")
    status["finished"] = now()
    status["summary"] = f"{done_n}/{len(jobs)} done"
    write_status(status_path, status)
    print(f"[batch] 完成 {status['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
