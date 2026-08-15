"""flayr_core.llm.api：LLM HTTP 调用底层。

只负责"和 LLM HTTP 服务对话"这件事，不包含任何业务规则、payload 构造或结果校验。
translation 等只需要 API 调用的下游模块应该直接 import 本模块，
而不是通过 llm package 拉入整套业务规则。
"""

from __future__ import annotations

import argparse
import codecs
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..resources import (
    ResourceBudgetExceeded,
    current_budget,
    encode_file_data_url,
    finite_nonnegative,
)
from ..network import (
    DEFAULT_QWEN_API_HOSTS,
    DEFAULT_TRANSCRIPT_ONLY_QWEN_HOSTS,
    OutboundURLPolicyError,
    ValidatedOutboundURL,
    validate_outbound_url,
)
from ..utils import cleanup_stale_temp_entries, run_command, write_text

LLM_CURL_MAX_TIME_SECONDS = 1800
LLM_CURL_LOW_SPEED_LIMIT_BYTES_PER_SECOND = 1
LLM_CURL_LOW_SPEED_TIME_SECONDS = 180
LLM_CURL_RETRIES = 2
LLM_OUTPUT_EXPANSIONS = 2
LLM_FINALIZATION_RESERVE_SECONDS = 30
LLM_COMMAND_WRAPPER_GRACE_SECONDS = 5
LLM_COMMAND_CLEANUP_GRACE_SECONDS = 4
LLM_RUN_OVERHEAD_RESERVE_SECONDS = (
    LLM_FINALIZATION_RESERVE_SECONDS
    + LLM_COMMAND_WRAPPER_GRACE_SECONDS
    + LLM_COMMAND_CLEANUP_GRACE_SECONDS
)
LLM_MAX_OUTPUT_TOKENS = 65536
LLM_GENERIC_MAX_OUTPUT_TOKENS = 32768
VIDEO_DATA_URL_MAX_DURATION_SECONDS = 180.0
VIDEO_DATA_URL_MAX_BYTES = 24 * 1024 * 1024
VIDEO_TRANSCODE_TIMEOUT_SECONDS = 300
AUDIO_DATA_URL_MAX_DURATION_SECONDS = 600.0
AUDIO_DATA_URL_MAX_BYTES = 8 * 1024 * 1024
AUDIO_TRANSCODE_TIMEOUT_SECONDS = 300
IMAGE_DATA_URL_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_SINGLE_REQUEST_BYTES = 64 * 1024 * 1024
SSE_MAX_EVENT_BYTES = 4 * 1024 * 1024
LLM_TRANSPORT_DIAGNOSTIC_BYTES = 64 * 1024
RETIRED_MODEL_PREFIXES = ("qwen3-vl-flash",)


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared capabilities for a known approved provider profile.

    This is a static compatibility matrix, not a live capability probe. Unknown
    endpoints are blocked by the outbound allowlist and must not be treated as
    audio-capable.
    """

    profile: str
    confidence: str
    native_video_input: bool
    standalone_audio_input: bool
    native_audio_analysis: bool


@dataclass(frozen=True)
class _StreamingAttemptResult:
    content: str = ""
    usage: dict[str, Any] | None = None
    complete: bool = False
    finish_reason: str | None = None
    error: str = ""
    retryable: bool = True


def provider_capabilities(api_url: str, model: str = "") -> ProviderCapabilities:
    """Look up the explicit approved-provider profile for an endpoint/model pair."""
    normalized_model = str(model or "").strip().lower()
    try:
        hostname = (urlsplit(str(api_url or "")).hostname or "").lower()
    except ValueError:
        hostname = ""
    if hostname in DEFAULT_TRANSCRIPT_ONLY_QWEN_HOSTS and normalized_model.startswith("qwen"):
        return ProviderCapabilities(
            profile="beijing_maas_qwen_transcript_visual",
            confidence="runtime_verified",
            native_video_input=normalized_model.startswith("qwen3-vl"),
            standalone_audio_input=False,
            native_audio_analysis=False,
        )
    if hostname in DEFAULT_QWEN_API_HOSTS and normalized_model.startswith("qwen"):
        native_audio = normalized_model.startswith("qwen3-omni")
        return ProviderCapabilities(
            profile="dashscope_qwen_compatible",
            confidence="verified_matrix",
            native_video_input=(
                normalized_model.startswith("qwen3-vl")
                or normalized_model.startswith("qwen3-omni")
            ),
            standalone_audio_input=native_audio,
            native_audio_analysis=native_audio,
        )
    return ProviderCapabilities(
        profile="unknown_provider",
        confidence="unverified",
        native_video_input=False,
        standalone_audio_input=False,
        native_audio_analysis=False,
    )


def can_send_standalone_audio(api_url: str, model: str = "") -> bool:
    """Return the matrix decision for OpenAI-style ``input_audio`` blocks."""
    return provider_capabilities(api_url, model).standalone_audio_input


def can_analyze_native_video(api_url: str, model: str = "") -> bool:
    """Return whether the profile accepts native video as visual input."""
    return provider_capabilities(api_url, model).native_video_input


def can_analyze_native_audio(api_url: str, model: str = "") -> bool:
    """Return the matrix decision for direct waveform perception."""
    return provider_capabilities(api_url, model).native_audio_analysis


def reject_retired_model(model: str) -> None:
    """Block retired models at the shared provider boundary before any network call."""
    normalized = str(model or "").strip().lower()
    for prefix in RETIRED_MODEL_PREFIXES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            raise SystemExit(f"model has been retired and cannot be called: {model}")


def _curl_resolve_entries(validated: ValidatedOutboundURL) -> tuple[str, ...]:
    """Pin curl to the public addresses validated for the original hostname."""
    addresses = []
    for address in validated.resolved_addresses:
        curl_address = f"[{address}]" if ":" in address else address
        addresses.append(curl_address)
    if not addresses:
        return ()
    # Repeating --resolve for one host replaces the previous address in curl.
    # A comma-separated address set preserves every validated failover target.
    return (f"{validated.hostname}:{validated.port}:{','.join(addresses)}",)


def _write_request_json(path: Path, payload: dict[str, Any]) -> None:
    """Serialize a request directly to its temporary file without a second full string."""
    with path.open("w", encoding="utf-8") as sink:
        json.dump(payload, sink, ensure_ascii=False)


def _write_completion_response(
    raw_path: Path,
    content: str,
    usage: dict[str, Any] | None,
    finish_reason: str,
    *,
    cleanup_raw: bool,
) -> str:
    """Persist one assembled completion while keeping raw response cleanup bounded."""
    response: dict[str, Any] = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    }
    if usage:
        response["usage"] = usage
    raw_text = json.dumps(response, ensure_ascii=False)
    try:
        write_text(raw_path, raw_text)
    finally:
        if cleanup_raw:
            raw_path.unlink(missing_ok=True)
    return raw_text


def read_llm_api_key(args: argparse.Namespace) -> str:
    """优先从环境变量读取 API key，回退到 macOS Keychain。"""
    # env 与 keychain 两条路径都 strip：尾换行混进 Authorization 头会把请求体顶空（400 Request body is required）
    env_key = os.environ.get(args.llm_api_key_env, "").strip()
    if env_key:
        return env_key
    service = args.llm_api_key_keychain_service
    if not service:
        return ""
    command = [
        "security",
        "find-generic-password",
        "-s",
        service,
        "-a",
        args.llm_api_key_keychain_account,
        "-w",
    ]
    completed = run_command(command)
    if completed.returncode != 0:
        return ""
    # keychain 读出的值带尾换行，混进 Authorization 头会把请求体顶空（400 Request body is required），必须 strip
    return completed.stdout.strip()


class IncrementalSSEParser:
    """Parse SSE events as bytes arrive, with explicit event and stream caps."""

    def __init__(self, *, max_event_bytes: int = SSE_MAX_EVENT_BYTES, max_total_bytes: int | None = None) -> None:
        self.max_event_bytes = max(1, int(max_event_bytes))
        self.max_total_bytes = max_total_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._line_buffer = ""
        self._data_lines: list[str] = []
        self._event_bytes = 0
        self._total_bytes = 0
        self._parts: list[str] = []
        self._usage: dict[str, Any] | None = None
        self._complete = False
        self._finish_reason: str | None = None
        self.error: str | None = None

    def feed(self, chunk: bytes) -> None:
        if self.error:
            return
        self._total_bytes += len(chunk)
        if self.max_total_bytes is not None and self._total_bytes > self.max_total_bytes:
            self.error = f"SSE response exceeded {self.max_total_bytes} bytes"
            return
        try:
            self._line_buffer += self._decoder.decode(chunk)
        except UnicodeDecodeError as exc:
            self.error = f"invalid UTF-8 SSE response: {exc.reason}"
            return
        self._consume_lines()

    def finish(self) -> None:
        if self.error:
            return
        try:
            self._line_buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            self.error = f"invalid UTF-8 SSE response: {exc.reason}"
            return
        self._consume_lines()
        if self._line_buffer:
            self._process_line(self._line_buffer)
            self._line_buffer = ""
        if self._data_lines and not self.error:
            self.error = "SSE stream ended before the event delimiter"

    def result(self) -> tuple[str, dict[str, Any] | None, bool, str | None, str | None]:
        return "".join(self._parts), self._usage, self._complete, self._finish_reason, self.error

    def _consume_lines(self) -> None:
        while not self.error:
            newline_positions = [position for position in (self._line_buffer.find("\n"), self._line_buffer.find("\r")) if position >= 0]
            if not newline_positions:
                return
            position = min(newline_positions)
            terminator_size = 1
            if self._line_buffer[position] == "\r":
                if position + 1 >= len(self._line_buffer):
                    return
                if self._line_buffer[position + 1] == "\n":
                    terminator_size = 2
            line = self._line_buffer[:position]
            self._line_buffer = self._line_buffer[position + terminator_size:]
            self._process_line(line)

    def _process_line(self, line: str) -> None:
        self._event_bytes += len(line.encode("utf-8")) + 2
        if self._event_bytes > self.max_event_bytes:
            self.error = f"SSE event exceeded {self.max_event_bytes} bytes"
            return
        if not line:
            self._dispatch_event()
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if not separator:
            return
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)

    def _dispatch_event(self) -> None:
        if not self._data_lines:
            self._event_bytes = 0
            return
        data = "\n".join(self._data_lines)
        self._data_lines = []
        self._event_bytes = 0
        if data == "[DONE]":
            self._complete = True
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            self.error = f"invalid SSE JSON event: {exc.msg}"
            return
        if not isinstance(chunk, dict):
            self.error = "SSE JSON event must be an object"
            return
        choices = chunk.get("choices") or []
        if choices:
            if not isinstance(choices, list) or not isinstance(choices[0], dict):
                self.error = "SSE choices event has an invalid shape"
                return
            choice = choices[0]
            delta = choice.get("delta") or {}
            if isinstance(delta, dict):
                piece = delta.get("content")
                if isinstance(piece, str):
                    self._parts.append(piece)
                elif isinstance(piece, list):
                    self._parts.extend(
                        str(item.get("text") or "")
                        for item in piece
                        if isinstance(item, dict) and item.get("text")
                    )
            reason = choice.get("finish_reason")
            if reason:
                self._complete = True
                self._finish_reason = str(reason)
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self._usage = usage


def _load_streaming_payload(
    payload_path: Path,
    *,
    cleanup_payload: bool,
    request_limit: int,
) -> dict[str, Any]:
    request_size = payload_path.stat().st_size if payload_path.is_file() else -1
    if request_size < 0:
        raise SystemExit(f"LLM request payload is missing: {payload_path}")
    if request_size > request_limit:
        if cleanup_payload:
            payload_path.unlink(missing_ok=True)
        raise SystemExit(
            f"LLM request payload exceeds the single-request byte limit: {request_size}"
        )
    try:
        with payload_path.open("r", encoding="utf-8") as source:
            payload = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"LLM request payload is invalid: {exc}") from exc
    finally:
        if cleanup_payload:
            payload_path.unlink(missing_ok=True)
    if not isinstance(payload, dict):
        raise SystemExit("LLM request payload must be a JSON object")
    reject_retired_model(str(payload.get("model") or ""))
    payload["stream"] = True
    stream_options = payload.get("stream_options")
    if not isinstance(stream_options, dict):
        stream_options = {}
    stream_options["include_usage"] = True
    payload["stream_options"] = stream_options
    return payload


def _normalize_transport_limits(
    max_time_seconds: int,
    low_speed_time_seconds: int,
    retries: int,
    output_expansions: int,
) -> tuple[int, int, int, int]:
    try:
        return (
            max(
                1,
                int(
                    finite_nonnegative(
                        max_time_seconds,
                        "LLM request timeout",
                        maximum=LLM_CURL_MAX_TIME_SECONDS,
                    )
                ),
            ),
            max(
                1,
                int(
                    finite_nonnegative(
                        low_speed_time_seconds,
                        "LLM low-speed timeout",
                        maximum=LLM_CURL_MAX_TIME_SECONDS,
                    )
                ),
            ),
            max(0, int(finite_nonnegative(retries, "LLM retries", maximum=10))),
            max(
                0,
                int(
                    finite_nonnegative(
                        output_expansions,
                        "LLM output expansions",
                        maximum=10,
                    )
                ),
            ),
        )
    except ValueError as exc:
        raise SystemExit(f"invalid LLM resource limit: {exc}") from exc


def _build_streaming_curl_command(
    *,
    api_url: str,
    resolve_entries: tuple[str, ...],
    logical_request_id: str,
    request_path: Path,
    max_time_seconds: int,
    low_speed_time_seconds: int,
    request_limit: int,
) -> list[str]:
    command = [
        "curl",
        "-sS",
        "--http1.1",
        "--no-buffer",
        "--noproxy",
        "*",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--max-redirs",
        "0",
        "--fail-with-body",
        "--connect-timeout",
        "30",
        "--max-time",
        str(max_time_seconds),
        "--speed-limit",
        str(LLM_CURL_LOW_SPEED_LIMIT_BYTES_PER_SECOND),
        "--speed-time",
        str(low_speed_time_seconds),
        "--max-filesize",
        str(request_limit),
        "-H",
        "@-",
        "-H",
        "Content-Type: application/json",
        "-H",
        f"X-Flayr-Request-ID: {logical_request_id}",
        "-H",
        f"Idempotency-Key: {logical_request_id}",
        "--data-binary",
        f"@{request_path}",
        "--write-out",
        "%{stderr}__FLAYR_HTTP_STATUS__%{http_code}\n",
        api_url,
    ]
    command[1:1] = [
        item for entry in resolve_entries for item in ("--resolve", entry)
    ]
    return command


def _transport_attempt_timeout(active_budget: Any, max_time_seconds: int) -> tuple[int, str]:
    if active_budget is None:
        return max_time_seconds, ""
    available = int(
        active_budget.remaining_wall_seconds() - LLM_RUN_OVERHEAD_RESERVE_SECONDS
    )
    if available <= 0:
        return 0, (
            "insufficient wall time for another LLM transport attempt "
            f"while preserving {LLM_FINALIZATION_RESERVE_SECONDS}s for deterministic finalization"
        )
    return min(max_time_seconds, available), ""


def _run_streaming_attempt(
    *,
    command: list[str],
    request_path: Path,
    temp_root: Path,
    attempt: int,
    attempt_timeout_seconds: int,
    request_limit: int,
    active_budget: Any,
    call_kind: str,
    logical_request_id: str,
    retry_reason: str,
    api_key: str,
    response_meta: dict[str, Any] | None,
) -> _StreamingAttemptResult:
    attempt_command = list(command)
    attempt_command[attempt_command.index("--max-time") + 1] = str(
        attempt_timeout_seconds
    )
    current_request_size = request_path.stat().st_size
    if current_request_size > request_limit:
        raise SystemExit(
            f"LLM request payload exceeds the single-request byte limit: {current_request_size}"
        )
    if active_budget is not None:
        try:
            active_budget.reserve_api_call(
                current_request_size,
                kind=call_kind,
                request_id=logical_request_id,
                attempt=attempt,
                retry_reason=retry_reason,
            )
        except ResourceBudgetExceeded as exc:
            raise SystemExit(str(exc)) from exc
    if response_meta is not None:
        response_meta["transport_attempts"] = attempt
        response_meta["transport_timeout_seconds"] = attempt_timeout_seconds
        response_meta["transport_attempt_timeouts_seconds"].append(
            attempt_timeout_seconds
        )
    response_path = temp_root / f"attempt-{attempt}.sse"
    parser = IncrementalSSEParser(max_total_bytes=request_limit)
    with response_path.open("wb") as response_file:
        def consume_response(chunk: bytes) -> None:
            response_file.write(chunk)
            parser.feed(chunk)

        completed = run_command(
            attempt_command,
            timeout_seconds=attempt_timeout_seconds + LLM_COMMAND_WRAPPER_GRACE_SECONDS,
            budget=active_budget,
            max_output_bytes=request_limit + LLM_TRANSPORT_DIAGNOSTIC_BYTES,
            stdin_text=f"Authorization: Bearer {api_key}\n",
            stdout_callback=consume_response,
            capture_stdout=False,
        )
    parser.finish()
    response_size = response_path.stat().st_size if response_path.is_file() else 0
    if active_budget is not None:
        try:
            active_budget.reserve_download(response_size)
        except ResourceBudgetExceeded as exc:
            raise SystemExit(str(exc)) from exc
    http_status = parse_curl_http_status(completed.stderr)
    if completed.returncode != 0 or http_status is None or not 200 <= http_status < 300:
        body = ""
        if response_path.is_file():
            with response_path.open("r", encoding="utf-8", errors="replace") as source:
                body = source.read(400).strip()
        diagnostic = strip_curl_http_status(completed.stderr).strip()
        error = diagnostic or completed.stdout.strip() or "curl failed"
        if http_status is None:
            error = f"missing HTTP status: {error}"
        else:
            error = f"HTTP {http_status}: {error}"
            if 300 <= http_status < 400:
                error = f"{error}（禁止跟随未重新校验的重定向）"
        if body:
            error = f"{error}\n{body}"
        return _StreamingAttemptResult(
            error=error,
            retryable=is_retryable_error(error, http_status=http_status),
        )
    if response_size > request_limit:
        return _StreamingAttemptResult(
            error="LLM response exceeded the single-request byte limit"
        )
    content, usage, complete, finish_reason, parse_error = parser.result()
    if parse_error:
        return _StreamingAttemptResult(error=f"SSE parse failed: {parse_error}")
    return _StreamingAttemptResult(
        content=content,
        usage=usage,
        complete=complete,
        finish_reason=finish_reason,
    )


def _write_successful_stream_response(
    *,
    raw_path: Path,
    result: _StreamingAttemptResult,
    finish_reason: str,
    cleanup_raw: bool,
    response_meta: dict[str, Any] | None,
    retry_reasons: list[str],
) -> str:
    if response_meta is not None:
        response_meta.update(
            {
                "transport_status": "completed",
                "transport_retry_reasons": list(retry_reasons),
                "finish_reason": finish_reason,
                "usage": result.usage or {},
            }
        )
    return _write_completion_response(
        raw_path,
        result.content,
        result.usage,
        finish_reason,
        cleanup_raw=cleanup_raw,
    )


def _execute_streaming_request(
    *,
    api_url: str,
    api_key: str,
    raw_path: Path,
    payload: dict[str, Any],
    resolve_entries: tuple[str, ...],
    logical_request_id: str,
    max_time_seconds: int,
    low_speed_time_seconds: int,
    retries: int,
    output_expansions: int,
    request_limit: int,
    active_budget: Any,
    call_kind: str,
    initial_retry_reason: str | None,
    cleanup_raw: bool,
    response_meta: dict[str, Any] | None,
    transport_retry_reasons: list[str],
) -> str:
    with tempfile.TemporaryDirectory(
        prefix=f".{raw_path.stem}.flayr-tmp.", dir=raw_path.parent
    ) as temp_dir:
        temp_root = Path(temp_dir)
        request_path = temp_root / "request.json"
        _write_request_json(request_path, payload)
        command = _build_streaming_curl_command(
            api_url=api_url,
            resolve_entries=resolve_entries,
            logical_request_id=logical_request_id,
            request_path=request_path,
            max_time_seconds=max_time_seconds,
            low_speed_time_seconds=low_speed_time_seconds,
            request_limit=request_limit,
        )
        last_error = ""
        retry_reason = str(initial_retry_reason or "")[:200]
        attempt = 0
        transport_retries_used = 0
        output_expansions_used = 0
        while True:
            attempt += 1
            attempt_timeout, last_error = _transport_attempt_timeout(
                active_budget, max_time_seconds
            )
            if attempt_timeout <= 0:
                transport_retry_reasons.append(last_error)
                break
            retry_kind = "transport"
            result = _run_streaming_attempt(
                command=command,
                request_path=request_path,
                temp_root=temp_root,
                attempt=attempt,
                attempt_timeout_seconds=attempt_timeout,
                request_limit=request_limit,
                active_budget=active_budget,
                call_kind=call_kind,
                logical_request_id=logical_request_id,
                retry_reason=retry_reason,
                api_key=api_key,
                response_meta=response_meta,
            )
            if result.error:
                last_error = result.error
                if (
                    not result.retryable
                    or "total wall time budget exceeded" in last_error
                ):
                    transport_retry_reasons.append(last_error[:200])
                    break
            elif result.complete and result.content and result.finish_reason != "length":
                return _write_successful_stream_response(
                    raw_path=raw_path,
                    result=result,
                    finish_reason=result.finish_reason or "stop",
                    cleanup_raw=cleanup_raw,
                    response_meta=response_meta,
                    retry_reasons=transport_retry_reasons,
                )
            elif result.finish_reason == "length":
                old_budget, new_budget = increase_output_budget(payload)
                budget_field = output_budget_field(payload)
                if new_budget > old_budget and output_expansions_used < output_expansions:
                    output_expansions_used += 1
                    retry_kind = "output_expansion"
                    _write_request_json(request_path, payload)
                    last_error = (
                        f"输出被 {budget_field}={old_budget} 截断，"
                        f"已提高至 {new_budget} 后重试"
                    )
                elif result.content:
                    return _write_successful_stream_response(
                        raw_path=raw_path,
                        result=result,
                        finish_reason="length",
                        cleanup_raw=cleanup_raw,
                        response_meta=response_meta,
                        retry_reasons=transport_retry_reasons,
                    )
                else:
                    last_error = f"输出在 {budget_field}={old_budget} 仍被截断"
                    break
            else:
                last_error = (
                    "流式响应不完整（连接在 [DONE] 前中断）"
                    if result.content
                    else "流式响应无内容"
                )
            retry_reason = last_error
            transport_retry_reasons.append(last_error[:200])
            if retry_kind == "transport":
                if transport_retries_used >= retries:
                    break
                transport_retries_used += 1
            sleep_seconds = (
                float(5 * transport_retries_used) if retry_kind == "transport" else 0.0
            )
            if active_budget is not None:
                retry_window = max(
                    0.0,
                    active_budget.remaining_wall_seconds()
                    - LLM_RUN_OVERHEAD_RESERVE_SECONDS,
                )
                if retry_window <= 0:
                    break
                if retry_kind == "transport":
                    sleep_seconds = min(sleep_seconds, retry_window)
            if response_meta is not None:
                response_meta["request_retry_kinds"].append(retry_kind)
            if retry_kind == "transport":
                if sleep_seconds <= 0:
                    break
                time.sleep(sleep_seconds)
        if response_meta is not None:
            response_meta.update(
                {
                    "transport_status": "failed",
                    "transport_retry_reasons": list(transport_retry_reasons),
                    "transport_error": last_error[:500],
                }
            )
        raise SystemExit(f"LLM streaming request failed: {last_error}")


def call_llm_api(
    api_url: str,
    api_key: str,
    payload_path: Path,
    raw_path: Path,
    *,
    max_time_seconds: int = LLM_CURL_MAX_TIME_SECONDS,
    low_speed_time_seconds: int = LLM_CURL_LOW_SPEED_TIME_SECONDS,
    retries: int = LLM_CURL_RETRIES,
    output_expansions: int = LLM_OUTPUT_EXPANSIONS,
    budget: Any = None,
    call_kind: str = "llm",
    request_id: str | None = None,
    initial_retry_reason: str | None = None,
    cleanup_payload: bool = True,
    cleanup_raw: bool = True,
    response_meta: dict[str, Any] | None = None,
) -> str:
    """流式（SSE）调用已批准供应商的 chat completions，分块拼装后合成标准 completion JSON 返回。

    为什么流式：非流式下大响应体会被网络/代理按体积静默截断（无错误、finish_reason=None、
    body 不完整），导致 JSON 残缺，且无法靠重发修复（重发也截断在同一长度）。流式分块传输
    规避"单个大 body 被切"，并能用 data:[DONE] 哨兵可靠判断是否完整；连接中途断则整次重试。
    返回值仍是标准 {"choices":[{"message":{"content":...}}]} 形状，下游无需改动。
    """
    if not shutil.which("curl"):
        if cleanup_payload:
            payload_path.unlink(missing_ok=True)
        raise SystemExit("LLM streaming 需要 curl，但系统未找到 curl。")
    try:
        validated_url = validate_outbound_url(api_url)
    except OutboundURLPolicyError as exc:
        if cleanup_payload:
            payload_path.unlink(missing_ok=True)
        raise SystemExit(str(exc)) from exc
    resolve_entries = _curl_resolve_entries(validated_url)
    if not resolve_entries:
        if cleanup_payload:
            payload_path.unlink(missing_ok=True)
        raise SystemExit("API 域名没有可用于连接的已验证公网地址")

    active_budget = budget or current_budget()
    request_limit = (
        active_budget.limits.max_single_request_bytes if active_budget else DEFAULT_SINGLE_REQUEST_BYTES
    )
    payload = _load_streaming_payload(
        payload_path,
        cleanup_payload=cleanup_payload,
        request_limit=request_limit,
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    max_time_seconds, low_speed_time_seconds, retries, output_expansions = (
        _normalize_transport_limits(
            max_time_seconds,
            low_speed_time_seconds,
            retries,
            output_expansions,
        )
    )
    cleanup_stale_temp_entries(
        raw_path.parent,
        (
            f".{raw_path.stem}.stream_req.",
            f".{raw_path.stem}.auth.",
            f".{raw_path.stem}.attempt-",
            f".{raw_path.stem}.flayr-tmp.",
        ),
    )
    logical_request_id = str(request_id or uuid.uuid4().hex)
    transport_retry_reasons: list[str] = []
    if initial_retry_reason:
        transport_retry_reasons.append(str(initial_retry_reason)[:200])
    if response_meta is not None:
        response_meta.update(
            {
                "logical_request_id": logical_request_id,
                "transport_attempts": 0,
                "transport_retry_reasons": list(transport_retry_reasons),
                "transport_attempt_timeouts_seconds": [],
                "request_retry_kinds": [],
            }
        )
    return _execute_streaming_request(
        api_url=api_url,
        api_key=api_key,
        raw_path=raw_path,
        payload=payload,
        resolve_entries=resolve_entries,
        logical_request_id=logical_request_id,
        max_time_seconds=max_time_seconds,
        low_speed_time_seconds=low_speed_time_seconds,
        retries=retries,
        output_expansions=output_expansions,
        request_limit=request_limit,
        active_budget=active_budget,
        call_kind=call_kind,
        initial_retry_reason=initial_retry_reason,
        cleanup_raw=cleanup_raw,
        response_meta=response_meta,
        transport_retry_reasons=transport_retry_reasons,
    )


def increase_output_budget(payload: dict[str, Any]) -> tuple[int, int]:
    """length 截断时把单次输出预算翻倍，并保留 provider 字段语义。"""
    budget_field = output_budget_field(payload)
    try:
        old_budget = int(payload.get(budget_field) or 8192)
    except (TypeError, ValueError):
        old_budget = 8192
    cap = LLM_MAX_OUTPUT_TOKENS if budget_field == "max_completion_tokens" else LLM_GENERIC_MAX_OUTPUT_TOKENS
    new_budget = min(max(old_budget * 2, 16384), cap)
    payload[budget_field] = new_budget
    return old_budget, new_budget


def output_budget_field(payload: dict[str, Any]) -> str:
    """Return the output-limit field already selected by the request builder."""
    return "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"


def parse_curl_http_status(stderr_text: str) -> int | None:
    """Read curl's structured HTTP status marker from stderr."""
    matches = re.findall(r"__FLAYR_HTTP_STATUS__(\d{3})", str(stderr_text or ""))
    if not matches:
        return None
    try:
        status = int(matches[-1])
    except ValueError:
        return None
    return status if 100 <= status <= 599 else None


def strip_curl_http_status(stderr_text: str) -> str:
    """Remove the machine-readable curl status marker from human diagnostics."""
    return re.sub(r"\s*__FLAYR_HTTP_STATUS__\d{3}\s*", "\n", str(stderr_text or ""))


def is_retryable_error(error_text: str, *, http_status: int | None = None) -> bool:
    """传输/服务端瞬时错误才重试；鉴权/请求错误（401/400/403 等）不重试，快速失败。"""
    if http_status is not None:
        if http_status in {400, 401, 403, 404, 405, 422}:
            return False
        if http_status in {408, 425, 429} or 500 <= http_status <= 599:
            return True
        return False
    lowered = error_text.lower()
    if any(marker in lowered for marker in ("401", "403", "400", "unauthorized", "forbidden", "invalid api", "bad request")):
        return False
    return any(
        marker in lowered
        for marker in (
            "timed out", "timeout", "connection reset", "recv failure", "empty reply",
            "failed to connect", "couldn't connect to server", "could not connect to server",
            "transfer closed with outstanding read data", "curl: (18)",
            "framing layer", "http/2", "429", "too many requests",
            "500", "502", "503", "504", "internal_server_error", "backend",
            "could not resolve", "connection refused",
            "ssl_error_syscall", "ssl_connect", "tls handshake",
        )
    )


def parse_sse_stream(sse_path: Path) -> tuple[str, dict[str, Any] | None, bool, str | None]:
    """Bounded compatibility wrapper for callers that already have an SSE file."""
    if not sse_path.is_file():
        return "", None, False, None
    parser = IncrementalSSEParser(max_total_bytes=DEFAULT_SINGLE_REQUEST_BYTES)
    with sse_path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            parser.feed(chunk)
    parser.finish()
    content, usage, complete, finish_reason, error = parser.result()
    return content, usage, complete and error is None, finish_reason


def extract_chat_completion_text(response: dict[str, Any]) -> str:
    """从供应商响应中提取文本，兼容 chat.completions 和 responses 两种 schema。"""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [part.get("text", "") for part in content if isinstance(part, dict)]
            return "\n".join(part for part in parts if part)

    output = response.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    texts.append(str(content.get("text", "")))
        if texts:
            return "\n".join(texts)

    raise SystemExit("LLM response did not contain text output.")


def image_to_data_url(path: Path, *, max_bytes: int = IMAGE_DATA_URL_MAX_BYTES) -> str:
    """把本地图片读成 base64 data URL，供多模态 LLM 输入。"""
    try:
        return encode_file_data_url(path, max_bytes=max_bytes, expected_kind="image")
    except (OSError, ValueError, ResourceBudgetExceeded) as exc:
        raise SystemExit(f"invalid image input: {exc}") from exc


def video_to_data_url(
    path: Path,
    fps: float = 3.0,
    max_width: int = 480,
    start: float | None = None,
    duration: float | None = None,
    max_duration_seconds: float = VIDEO_DATA_URL_MAX_DURATION_SECONDS,
    max_data_bytes: int = VIDEO_DATA_URL_MAX_BYTES,
    timeout_seconds: int = VIDEO_TRANSCODE_TIMEOUT_SECONDS,
    budget: Any = None,
) -> str | None:
    """把本地视频重编码成小体积 mp4 的 base64 data URL，供 omni 原生视频理解。

    用 ffmpeg 在客户端直接控制抽帧密度（fps）和分辨率，不依赖 API 端 fps 参数：
      - fps：较高（3~5）抓住转场/表情突变/特效高潮等变化点；
      - max_width：降到 480 宽（保持宽高比），把 base64 payload 压到几 MB，
        既够 omni 看清画面又规避大 body 走代理失败的问题；
      - 音轨完整保留（omni 据此听 BGM / 语气 / 音效）。
      - start/duration：只切一个时间窗，用于 Phase C 回看低置信阶段。

    ffmpeg 不可用或转码失败时返回 None，调用方应回退到抽帧+音频模式。
    """
    active_budget = budget or current_budget()
    if not path.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        fps = finite_nonnegative(fps, "video fps", maximum=60.0)
        max_width = int(finite_nonnegative(max_width, "video max_width", maximum=4096.0))
        max_duration_seconds = finite_nonnegative(
            max_duration_seconds, "video max duration", maximum=24 * 60 * 60.0
        )
        max_data_bytes = int(finite_nonnegative(max_data_bytes, "video max bytes", maximum=64 * 1024 * 1024))
        timeout_seconds = int(finite_nonnegative(timeout_seconds, "video timeout", maximum=1800.0))
        if fps <= 0 or max_width <= 0 or max_data_bytes <= 0 or timeout_seconds <= 0:
            return None
        if start is not None:
            start = finite_nonnegative(start, "video start", maximum=24 * 60 * 60.0)
        if duration is not None:
            duration = finite_nonnegative(duration, "video duration")
            if duration <= 0 or duration > max_duration_seconds:
                return None
        media_duration = probe_media_duration_seconds(path, budget=active_budget)
        if media_duration is None:
            return None
        if duration is None and media_duration > max_duration_seconds:
            return None
        if duration is not None and (start or 0.0) + duration > media_duration + 0.25:
            return None
    except (ValueError, ResourceBudgetExceeded):
        return None
    cleanup_stale_temp_entries(Path(tempfile.gettempdir()), (".flayr-media-",))
    try:
        with tempfile.TemporaryDirectory(prefix=".flayr-media-") as temp_dir:
            tmp_path = str(Path(temp_dir) / "clip.mp4")
            command = [ffmpeg, "-y"]
            if start is not None:
                command += ["-ss", str(max(0.0, start))]
            if duration is not None:
                command += ["-t", str(max(0.1, duration))]
            command += [
                "-i", str(path),
                "-vf", f"fps={fps},scale={max_width}:-2",
                "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "64k",
                # Multithreaded AAC output can differ byte-for-byte for the
                # same tail window, which invalidates strict replay identity.
                "-threads", "1",
                "-movflags", "+faststart",
                "-fs", str(max_data_bytes),
                tmp_path,
            ]
            completed = run_command(command, timeout_seconds=timeout_seconds, budget=active_budget)
            if completed.returncode != 0:
                return None
            if Path(tmp_path).stat().st_size >= max_data_bytes:
                return None
            return encode_file_data_url(Path(tmp_path), max_bytes=max_data_bytes, expected_kind="video")
    except (OSError, ResourceBudgetExceeded, ValueError):
        return None


def probe_media_duration_seconds(path: Path, budget: Any = None) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    completed = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout_seconds=30,
        max_output_bytes=4096,
        budget=budget or current_budget(),
    )
    if completed.returncode != 0:
        return None
    try:
        value = float(completed.stdout.strip())
        if not math.isfinite(value) or value < 0:
            return None
        return value
    except (TypeError, ValueError):
        return None


def audio_to_mp3_data_url(
    path: Path,
    start: float | None = None,
    duration: float | None = None,
    *,
    max_duration_seconds: float = AUDIO_DATA_URL_MAX_DURATION_SECONDS,
    max_data_bytes: int = AUDIO_DATA_URL_MAX_BYTES,
    timeout_seconds: int = AUDIO_TRANSCODE_TIMEOUT_SECONDS,
    budget: Any = None,
) -> str | None:
    """把本地音频（通常是 audio.wav）转成 mp3 的 base64 data URL，供 omni 模型听音轨。

    wav 体积大，先用 ffmpeg 压成 64k mp3 再内联，避免 payload 过大。
    传入 start/duration 时只切对应时间窗（秒），用于阶段二按变化点切片、声画对齐。
    ffmpeg 不可用或转码失败时返回 None，调用方应跳过音频输入（降级处理）。
    """
    active_budget = budget or current_budget()
    if not path.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        max_duration_seconds = finite_nonnegative(
            max_duration_seconds, "audio max duration", maximum=24 * 60 * 60.0
        )
        max_data_bytes = int(finite_nonnegative(max_data_bytes, "audio max bytes", maximum=64 * 1024 * 1024))
        timeout_seconds = int(finite_nonnegative(timeout_seconds, "audio timeout", maximum=1800.0))
        if max_data_bytes <= 0 or timeout_seconds <= 0:
            return None
        if start is not None:
            start = finite_nonnegative(start, "audio start", maximum=24 * 60 * 60.0)
        if duration is not None:
            duration = finite_nonnegative(duration, "audio duration")
            if duration <= 0 or duration > max_duration_seconds:
                return None
        source_duration = probe_media_duration_seconds(path, budget=active_budget)
        if source_duration is None:
            return None
        if duration is None and source_duration > max_duration_seconds:
            return None
        if duration is not None and (start or 0.0) + duration > source_duration + 0.25:
            return None
    except (ValueError, ResourceBudgetExceeded):
        return None
    cleanup_stale_temp_entries(Path(tempfile.gettempdir()), (".flayr-media-",))
    try:
        with tempfile.TemporaryDirectory(prefix=".flayr-media-") as temp_dir:
            tmp_path = str(Path(temp_dir) / "clip.mp3")
            command = [ffmpeg, "-y"]
            if start is not None:
                command += ["-ss", str(max(0.0, start))]
            if duration is not None:
                command += ["-t", str(max(0.1, duration))]
            command += [
                "-i", str(path), "-vn", "-acodec", "libmp3lame", "-b:a", "64k",
                "-fs", str(max_data_bytes), tmp_path,
            ]
            completed = run_command(command, timeout_seconds=timeout_seconds, budget=active_budget)
            if completed.returncode != 0 or Path(tmp_path).stat().st_size >= max_data_bytes:
                return None
            return encode_file_data_url(Path(tmp_path), max_bytes=max_data_bytes, expected_kind="audio")
    except (OSError, ResourceBudgetExceeded, ValueError):
        return None
