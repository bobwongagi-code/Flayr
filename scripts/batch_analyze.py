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
  "common_args": ["--llm-include-images","--llm-image-limit","12","--target-market","my"],
  "jobs": [
    {"name":"are_xie","creator":"/abs/creator.mp4","benchmark":"/abs/bench.mp4",
     "args":["--product-category","..."]}   # args 可选，覆盖/追加该 job 的参数
  ]
}

模型、端点、密钥从 runner 命令行传入，不放进低信任 jobs.json：
  python3 scripts/batch_analyze.py jobs.json --llm-model <model> \
      --llm-api-url <trusted-endpoint> --llm-api-key-keychain-service <service>

约定：
- 每个 job 输出到 runs/sample-<name>（或 job["output_dir"]）。
- 只有由主程序最后原子写入且通过输入/产物哈希校验的 _SUCCESS.json 才算完成；
  单独存在 analysis.json 不会跳过任务。
- 自动加 --reuse-preprocessing：同一 output-dir 重跑时复用已有抽帧/转写/轨，省时。
- 一个 job 失败只标 failed，不影响其余 job。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # works both as ``python scripts/batch_analyze.py`` and as a test module
    from flayr_core.run_manifest import SUCCESS_MANIFEST_NAME, command_digest, validate_success_manifest
except ModuleNotFoundError:  # pragma: no cover - package import path in test runners
    from scripts.flayr_core.run_manifest import SUCCESS_MANIFEST_NAME, command_digest, validate_success_manifest

ROOT = Path(__file__).resolve().parents[1]
SAFE_JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LOCK_SCHEMA_VERSION = 1
SHUTDOWN_GRACE_SECONDS = 5.0
RUNNER_OWNED_FLAGS = frozenset({
    "--benchmark-video",
    "--creator-video",
    "--output-dir",
    "--reuse-preprocessing",
    # 凭据与网络参数：禁止作业覆盖，防止低信任作业配置窃取高信任凭据
    "--llm-model",
    "--llm-api-url",
    "--llm-api-key-env",
    "--llm-api-key-keychain-service",
    "--llm-api-key-keychain-account",
    "--translation-model",
})


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_status(path: Path, status: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def job_output_dir(job: dict, runs_dir: Path) -> Path:
    root = runs_dir.expanduser().resolve()
    raw = job.get("output_dir")
    candidate = Path(str(raw)) if raw else root / f"sample-{job['name']}"
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.expanduser().resolve()
    if resolved == root or not _path_is_under(resolved, root):
        raise ValueError(f"job {job.get('name') or '<unknown>'} 的 output_dir 必须位于 runs 根目录内：{root}")
    return resolved


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_spec(spec: Any, runs_dir: Path, concurrency: int) -> tuple[list[dict], list[str]]:
    """校验作业清单，防止名称/目录冲突和参数覆盖 runner 持有的输入输出边界。"""
    if not isinstance(spec, dict):
        raise ValueError("jobs 文件根节点必须是 JSON object。")
    runs_dir = runs_dir.expanduser().resolve()
    jobs = spec.get("jobs")
    common_args = spec.get("common_args", [])
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("jobs 必须是非空数组。")
    if concurrency < 1:
        raise ValueError("--concurrency 必须大于等于 1。")
    _validate_cli_args(common_args, "common_args")

    names: set[str] = set()
    output_dirs: set[Path] = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"jobs[{index}] 必须是 object。")
        name = str(job.get("name") or "").strip()
        if not SAFE_JOB_NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError(f"jobs[{index}].name 非法：仅允许字母、数字、点、下划线和连字符。")
        if name in names:
            raise ValueError(f"重复 job name：{name}")
        names.add(name)
        for field in ("creator", "benchmark"):
            if not isinstance(job.get(field), str) or not job[field].strip():
                raise ValueError(f"job {name} 缺少有效的 {field} 路径。")
        _validate_cli_args(job.get("args", []), f"job {name}.args")
        if "output_dir" in job and (
            not isinstance(job["output_dir"], str) or not job["output_dir"].strip()
        ):
            raise ValueError(f"job {name}.output_dir 必须是非空字符串。")
        out_dir = job_output_dir(job, runs_dir)
        if out_dir in output_dirs:
            raise ValueError(f"多个 job 指向同一 output_dir：{out_dir}")
        output_dirs.add(out_dir)
    return jobs, common_args


def _validate_cli_args(values: Any, label: str) -> None:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{label} 必须是字符串数组。")
    for value in values:
        flag = value.split("=", 1)[0]
        if flag in RUNNER_OWNED_FLAGS or any(
            flag.startswith("--") and owned.startswith(flag) for owned in RUNNER_OWNED_FLAGS
        ):
            raise ValueError(f"{label} 不得覆盖 runner 参数 {flag}。")


def _option_value(values: list[str], option: str) -> str:
    for index, value in enumerate(values):
        if value == option and index + 1 < len(values):
            return values[index + 1]
        if value.startswith(f"{option}="):
            return value.split("=", 1)[1]
    return ""


def _success_manifest_valid(
    job: dict,
    out_dir: Path,
    common_args: list[str] | None = None,
    trusted_args: list[str] | None = None,
) -> bool:
    """A stale/partial analysis file is never a resumable completion marker."""
    effective_args = [*(common_args or []), *job.get("args", [])]
    expected_inputs: dict[str, Path] = {
        "benchmark_video": Path(job["benchmark"]),
        "creator_video": Path(job["creator"]),
    }
    analysis_result = _option_value(effective_args, "--analysis-result-json")
    if analysis_result:
        expected_inputs["analysis_result_json"] = Path(analysis_result)
    command = build_command(job, out_dir, common_args or [], trusted_args)
    return validate_success_manifest(
        out_dir,
        expected_inputs,
        {"argv_sha256": command_digest(command[2:])},
    )


def build_command(
    job: dict,
    out_dir: Path,
    common_args: list[str],
    trusted_args: list[str] | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "flayr.py"),
        "improve",
        "--creator-video",
        job["creator"],
        "--benchmark-video",
        job["benchmark"],
        "--output-dir",
        str(out_dir),
        "--reuse-preprocessing",
        *(trusted_args or []),
        *common_args,
        *job.get("args", []),
    ]
    proposition_key = str(job.get("proposition_key") or "").strip()
    if proposition_key:
        command.extend(["--proposition-key", proposition_key])
    comparison_scope_override = str(job.get("comparison_scope_override") or "").strip()
    if comparison_scope_override:
        command.extend(["--comparison-scope-override", comparison_scope_override])
    return command


def _boot_id() -> str:
    if platform.system() == "Linux":
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            pass
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            value = result.stdout.strip()
            if result.returncode == 0 and value:
                return value
        except (OSError, subprocess.SubprocessError):
            pass
    return "unknown"


def _process_start_token(pid: int) -> str:
    """Return a PID-reuse-resistant process start token where the OS exposes one."""
    if platform.system() == "Linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            end_of_comm = stat.rfind(")")
            fields = stat[end_of_comm + 2 :].split()
            if len(fields) > 19:
                return fields[19]
        except (OSError, ValueError):
            pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _lock_identity(pid: int | None = None) -> dict[str, Any]:
    owner_pid = int(pid or os.getpid())
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "pid": owner_pid,
        "process_start": _process_start_token(owner_pid),
        "host": socket.gethostname(),
        "boot_id": _boot_id(),
    }


def _read_lock_identity(lock_path: Path) -> dict[str, Any] | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw.isdigit():  # Legacy lock: safe to remove only when that PID is gone.
        return {"legacy": True, "pid": int(raw)}
    try:
        identity = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return identity if isinstance(identity, dict) else None


def _lock_owner_alive(identity: dict[str, Any]) -> bool:
    try:
        pid = int(identity.get("pid"))
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if identity.get("legacy"):
        return _pid_alive(pid)
    if identity.get("schema_version") != LOCK_SCHEMA_VERSION:
        return True
    if str(identity.get("host") or "") != socket.gethostname():
        # A shared filesystem may contain a live runner on another host.
        return True
    current_boot = _boot_id()
    recorded_boot = str(identity.get("boot_id") or "unknown")
    if current_boot != "unknown" and recorded_boot != "unknown" and current_boot != recorded_boot:
        return False
    if not _pid_alive(pid):
        return False
    recorded_start = str(identity.get("process_start") or "unknown")
    current_start = _process_start_token(pid)
    if recorded_start != "unknown" and current_start != "unknown" and recorded_start != current_start:
        return False
    return True


def acquire_lock(lock_path: Path) -> None:
    """Use an atomic lock carrying process identity, not only a reusable PID."""
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            identity = _read_lock_identity(lock_path)
            if identity is None:
                raise RuntimeError(f"runner 锁存在但无法读取身份，请确认后手动处理：{lock_path}")
            if _lock_owner_alive(identity):
                raise RuntimeError(f"已有 runner 在跑 (pid {identity.get('pid', '?')})")
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            json.dump(_lock_identity(), lock_file, ensure_ascii=False, sort_keys=True)
            lock_file.write("\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        return
    raise RuntimeError(f"无法取得 runner 锁：{lock_path}")


def release_lock(lock_path: Path) -> None:
    identity = _read_lock_identity(lock_path)
    if identity is None or identity.get("legacy"):
        return
    current = _lock_identity()
    if all(identity.get(key) == current.get(key) for key in ("pid", "process_start", "host", "boot_id")):
        lock_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Flayr 批量分析 runner")
    parser.add_argument("jobs_file", help="作业清单 JSON")
    parser.add_argument("--concurrency", type=int, default=1, help="并发作业数，默认 1（顺序）")
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"))
    # 这些选项属于启动 runner 的可信配置，不允许出现在 jobs.json。
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-api-url")
    parser.add_argument("--llm-api-key-env")
    parser.add_argument("--llm-api-key-keychain-service")
    parser.add_argument("--llm-api-key-keychain-account")
    parser.add_argument("--translation-model")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir).expanduser().resolve()
    try:
        spec = json.loads(Path(args.jobs_file).read_text(encoding="utf-8"))
        jobs, common_args = validate_spec(spec, runs_dir, args.concurrency)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    batch_dir = runs_dir / "_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    status_path = batch_dir / "status.json"

    lock_path = batch_dir / "runner.lock"
    try:
        acquire_lock(lock_path)
    except RuntimeError as error:
        print(f"[batch] {error}，拒绝重复启动。")
        return 1
    try:
        return _run_jobs(
            jobs,
            common_args,
            runs_dir,
            status_path,
            args.concurrency,
            _trusted_runner_args(args),
        )
    finally:
        release_lock(lock_path)


def _trusted_runner_args(args: argparse.Namespace) -> list[str]:
    """Serialize only explicitly supplied trusted provider settings."""
    names = (
        "llm_model",
        "llm_api_url",
        "llm_api_key_env",
        "llm_api_key_keychain_service",
        "llm_api_key_keychain_account",
        "translation_model",
    )
    values: list[str] = []
    for name in names:
        value = getattr(args, name, None)
        if value:
            values.extend([f"--{name.replace('_', '-')}", str(value)])
    return values


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权发信号 = 存活


def _signal_process_group(proc: subprocess.Popen, sig: signal.Signals) -> None:
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(proc.pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


def _stop_process(proc: subprocess.Popen, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
    """Stop one child process group with its own grace window."""
    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGKILL)
        proc.wait()


def _run_jobs(
    jobs: list[dict],
    common_args: list[str],
    runs_dir: Path,
    status_path: Path,
    concurrency: int,
    trusted_args: list[str] | None = None,
) -> int:
    batch_dir = status_path.parent
    status: dict = {
        "started": now(),
        "runner_pid": os.getpid(),
        "runner_host": socket.gethostname(),
        "concurrency": concurrency,
        "jobs": {},
    }
    for job in jobs:
        out = job_output_dir(job, runs_dir)
        done = _success_manifest_valid(job, out, common_args, trusted_args)
        if not done:
            # Do not let a marker from an interrupted/changed run survive a
            # relaunch and become valid by accident later.
            (out / SUCCESS_MANIFEST_NAME).unlink(missing_ok=True)
        status["jobs"][job["name"]] = {"state": "done" if done else "pending", "output_dir": str(out)}
    write_status(status_path, status)

    pending = [job for job in jobs if status["jobs"][job["name"]]["state"] != "done"]
    running: list[tuple[dict, subprocess.Popen, object]] = []
    idx = 0

    def launch(job: dict) -> None:
        out = job_output_dir(job, runs_dir)
        log_path = batch_dir / f"{job['name']}.log"
        log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — 进程存活期间需保持打开
        try:
            log_file.write(f"\n=== attempt started {now()} pid={os.getpid()} job={job['name']} ===\n")
            log_file.flush()
            proc = subprocess.Popen(
                build_command(job, out, common_args, trusted_args),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
        except BaseException:
            log_file.close()
            raise
        status["jobs"][job["name"]].update({"state": "running", "started": now(), "log": str(log_path)})
        write_status(status_path, status)
        running.append((job, proc, log_file))

    try:
        while idx < len(pending) or running:
            while idx < len(pending) and len(running) < concurrency:
                launch(pending[idx])
                idx += 1
            time.sleep(5)
            for entry in list(running):
                job, proc, log_file = entry
                if proc.poll() is None:
                    continue
                log_file.close()
                out = job_output_dir(job, runs_dir)
                ok = proc.returncode == 0 and _success_manifest_valid(job, out, common_args, trusted_args)
                status["jobs"][job["name"]].update(
                    {"state": "done" if ok else "failed", "rc": proc.returncode, "ended": now()}
                )
                write_status(status_path, status)
                running.remove(entry)
    except BaseException:
        for job, proc, _ in running:
            status["jobs"][job["name"]].update({"state": "interrupted", "ended": now()})
        write_status(status_path, status)
        for _, proc, log_file in running:
            if proc.poll() is None:
                _stop_process(proc)
            log_file.close()
        raise

    done_n = sum(1 for item in status["jobs"].values() if item["state"] == "done")
    failed_n = sum(1 for item in status["jobs"].values() if item["state"] == "failed")
    status["finished"] = now()
    status["summary"] = f"{done_n}/{len(jobs)} done"
    write_status(status_path, status)
    print(f"[batch] {status['summary']}")
    return 1 if failed_n else 0


if __name__ == "__main__":
    raise SystemExit(main())
