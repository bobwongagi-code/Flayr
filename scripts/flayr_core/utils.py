"""Shared helpers for Flayr core modules."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits, current_budget, finite_nonnegative

DEFAULT_COMMAND_TIMEOUT_SECONDS = 900
DEFAULT_COMMAND_OUTPUT_MAX_BYTES = 1 * 1024 * 1024
STALE_TEMP_ENTRY_MAX_AGE_SECONDS = 24 * 60 * 60


def cleanup_stale_temp_entries(
    directory: Path,
    prefixes: tuple[str, ...],
    *,
    max_age_seconds: float = STALE_TEMP_ENTRY_MAX_AGE_SECONDS,
) -> int:
    """Remove only known Flayr temporary entries left by an interrupted run."""
    try:
        age_limit = finite_nonnegative(max_age_seconds, "temporary entry age", maximum=30 * 24 * 60 * 60.0)
    except ValueError:
        return 0
    if not directory.is_dir() or not prefixes:
        return 0
    now = time.time()
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not any(entry.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            age = now - entry.stat().st_mtime
            if age < age_limit:
                continue
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry)
            removed += 1
        except OSError:
            continue
    return removed


def run_command(
    command: list[str],
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    *,
    max_output_bytes: int = DEFAULT_COMMAND_OUTPUT_MAX_BYTES,
    budget: Any = None,
    stdin_text: str | bytes | None = None,
    stdout_callback: Callable[[bytes], None] | None = None,
    stderr_callback: Callable[[bytes], None] | None = None,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
) -> subprocess.CompletedProcess[str]:
    """运行外部工具并限制输出、超时和本次 run 的墙钟预算。

    无论是否由主流程调用，都使用有界流式捕获路径；没有活动预算时创建
    一个仅用于命令自身的临时预算，避免独立调用者绕过输出和墙钟上限。
    """
    active_budget = budget or current_budget()
    try:
        output_limit = int(
            finite_nonnegative(max_output_bytes, "command output limit", maximum=512 * 1024 * 1024)
        )
        timeout = max(
            1,
            int(finite_nonnegative(timeout_seconds, "command timeout", maximum=24 * 60 * 60.0)),
        )
    except (TypeError, ValueError):
        return subprocess.CompletedProcess(command, 125, "", "invalid command resource limit")
    if active_budget is None:
        # Standalone callers get the same bounded behavior as the main pipeline;
        # a local budget is only a compatibility shell, not a second run budget.
        active_budget = ResourceBudget(
            ResourceLimits(max_total_wall_time=float(timeout), max_single_request_bytes=max(1, output_limit))
        )
    try:
        active_budget.check_wall_time()
    except ResourceBudgetExceeded as exc:
        return subprocess.CompletedProcess(command, 124, "", str(exc))
    remaining = active_budget.remaining_wall_seconds()
    timeout = min(timeout, max(1, int(remaining)))
    return _run_command_bounded(
        command,
        timeout,
        output_limit,
        active_budget,
        stdin_text=stdin_text,
        stdout_callback=stdout_callback,
        stderr_callback=stderr_callback,
        capture_stdout=capture_stdout,
        capture_stderr=capture_stderr,
    )


def _run_command_bounded(
    command: list[str],
    timeout_seconds: int,
    output_limit: int,
    budget: Any,
    *,
    stdin_text: str | bytes | None = None,
    stdout_callback: Callable[[bytes], None] | None = None,
    stderr_callback: Callable[[bytes], None] | None = None,
    capture_stdout: bool = True,
    capture_stderr: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Capture stdout/stderr incrementally and kill noisy children at the cap."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))

    if stdin_text is not None and process.stdin is not None:
        try:
            data = stdin_text.encode("utf-8") if isinstance(stdin_text, str) else stdin_text
            process.stdin.write(data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            try:
                process.stdin.close()
            except OSError:
                pass

    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    captured_bytes = 0
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    output_exceeded = False
    try:
        while selector.get_map():
            remaining = min(deadline - time.monotonic(), budget.remaining_wall_seconds())
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(0.25, remaining))
            if not events:
                if process.poll() is not None:
                    continue
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining_output = output_limit - captured_bytes
                if len(chunk) > remaining_output:
                    chunk = chunk[: max(0, remaining_output)]
                    output_exceeded = True
                if chunk:
                    callback = stdout_callback if key.data == "stdout" else stderr_callback
                    if callback is not None:
                        callback(chunk)
                    if key.data == "stdout" and capture_stdout:
                        buffers[key.data].extend(chunk)
                    elif key.data == "stderr" and capture_stderr:
                        buffers[key.data].extend(chunk)
                    captured_bytes += len(chunk)
                if output_exceeded:
                    selector.unregister(key.fileobj)
                    break
            if output_exceeded:
                break
    except (OSError, ResourceBudgetExceeded):
        timed_out = True
    finally:
        if timed_out or output_exceeded or process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        selector.close()
    stdout = bytes(buffers["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(buffers["stderr"]).decode("utf-8", errors="replace")
    if output_exceeded:
        return subprocess.CompletedProcess(command, 125, stdout, f"{stderr}\ncommand output exceeded {output_limit} bytes".strip())
    if timed_out:
        return subprocess.CompletedProcess(command, 124, stdout, f"{stderr}\ncommand timed out after {timeout_seconds}s".strip())
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, content: str) -> None:
    write_bytes(path, content.encode("utf-8"))


def write_bytes(path: Path, content: bytes) -> None:
    """在同目录临时文件写完并替换，避免中断后留下半份可见产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def _timeout_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return "（缺失）"
    text = path.read_text(encoding="utf-8").strip()
    return text or "（空）"
