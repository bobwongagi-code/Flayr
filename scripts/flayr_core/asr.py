"""Online Fun-ASR transcription for Flayr."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .llm.api import audio_to_mp3_data_url, read_llm_api_key
from .llm.provider_artifacts import provider_call_with_artifact, provider_role_replay_root
from .network import DEFAULT_QWEN_API_HOSTS, OutboundURLPolicyError, validate_outbound_url
from .resources import ResourceBudgetExceeded, current_budget
from .utils import run_command, write_json, write_text


DEFAULT_FUN_ASR_API_URL = (
    "https://llm-nlx73tfv3mm6w67e.cn-beijing.maas.aliyuncs.com/"
    "api/v1/services/aigc/multimodal-generation/generation"
)
DEFAULT_FUN_ASR_MODEL = "fun-asr-flash-2026-06-15"
ASR_FAILURE_PLACEHOLDER = "Online ASR failed; no transcript is available."
ASR_REQUEST_TIMEOUT_SECONDS = 900
ASR_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ASR_RETRIES = 1


def read_asr_api_key(args: Any) -> str:
    """Read the Qwen/DashScope key without putting it in a command or artifact."""
    env_name = str(getattr(args, "asr_api_key_env", "DASHSCOPE_API_KEY") or "DASHSCOPE_API_KEY")
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    # Do not fall back to an unrelated provider key. The shared fallback is
    # allowed only when the configured LLM endpoint is also an approved Qwen
    # endpoint; otherwise ASR must fail closed and ask for its own key env.
    try:
        llm_hostname = (urlsplit(str(getattr(args, "llm_api_url", "") or "")).hostname or "").lower()
    except ValueError:
        llm_hostname = ""
    if llm_hostname not in DEFAULT_QWEN_API_HOSTS:
        return ""
    return read_llm_api_key(args).strip()


def run_online_asr(
    api_url: str,
    model: str,
    api_key: str,
    language: str,
    audio_path: Path,
    role_dir: Path,
    transcript_path: Path,
    result: dict[str, Any],
    *,
    budget: Any = None,
    provider_replay_from: Path | None = None,
    replay_role_name: str | None = None,
) -> None:
    """Transcribe one local audio artifact through the approved Fun-ASR endpoint."""
    replay_role = str(replay_role_name or role_dir.name).strip()
    if provider_replay_from is not None:
        replay_role_dir = provider_role_replay_root(provider_replay_from, replay_role)
        if replay_role_dir == role_dir.expanduser().resolve():
            result["transcription_status"] = "failed"
            result.setdefault("errors", []).append(
                "online ASR replay output must differ from the replay source"
            )
            return
    segments_path = role_dir / "transcript.srt"
    words_path = role_dir / "transcript.words.json"
    json_path = role_dir / "transcript.json"
    _clear_transcript_artifacts(transcript_path, segments_path, words_path, json_path)
    result["transcript_segments_path"] = None
    result["transcript_segments_available"] = False
    result["transcript_words_path"] = None
    result["transcript_words_available"] = False
    result["transcription_backend"] = "fun-asr"
    result["transcription_model"] = model
    result["transcription_api_url"] = api_url
    result["transcription_provider_artifact"] = None
    result["transcription_execution_source"] = None
    result["transcription_provider_meta"] = {}

    replay_requested = provider_replay_from is not None
    if not api_key and not replay_requested:
        _mark_asr_failed(
            transcript_path,
            result,
            "online ASR API key is missing",
        )
        return
    if not audio_path.is_file():
        _mark_asr_failed(transcript_path, result, "audio artifact is missing")
        return

    requested_language = str(language or "auto").strip().lower()
    result["requested_language"] = requested_language
    data_url = audio_to_mp3_data_url(
        audio_path,
        max_duration_seconds=600.0,
        max_data_bytes=8 * 1024 * 1024,
        timeout_seconds=300,
        budget=budget,
    )
    if not data_url:
        _mark_asr_failed(transcript_path, result, "audio could not be prepared for online ASR")
        return

    payload = _build_asr_payload(model, data_url, requested_language)
    provider_artifact_path = role_dir / "provider_asr.json"
    result["transcription_provider_artifact"] = provider_artifact_path.name
    replay_root = (
        provider_role_replay_root(provider_replay_from, replay_role)
        if provider_replay_from is not None
        else None
    )
    live_meta: dict[str, Any] = {}

    try:
        response, response_meta, execution_source = provider_call_with_artifact(
            artifact_path=provider_artifact_path,
            replay_root=replay_root,
            call_kind="asr",
            payload=payload,
            model=model,
            api_url=api_url,
            response_meta=live_meta,
            call=lambda: (
                _call_asr_endpoint(
                    api_url,
                    api_key,
                    payload,
                    role_dir,
                    budget=budget,
                    response_meta=live_meta,
                ),
                live_meta,
            ),
        )
        result["transcription_provider_meta"] = response_meta
        result["transcription_execution_source"] = execution_source
    except (OSError, SystemExit, ValueError, ResourceBudgetExceeded) as exc:
        if provider_replay_from is not None:
            raise
        result["transcription_provider_meta"] = live_meta
        result["transcription_execution_source"] = "failed"
        _mark_asr_failed(transcript_path, result, f"online ASR request failed: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - ASR failure must remain a typed, durable run outcome.
        if provider_replay_from is not None:
            raise
        result["transcription_provider_meta"] = live_meta
        result["transcription_execution_source"] = "failed"
        _mark_asr_failed(transcript_path, result, f"online ASR unexpected failure: {exc}")
        return

    write_json(json_path, response)
    normalized = normalize_asr_response(response)
    text = str(normalized.get("text") or "").strip()
    if not text:
        _mark_asr_failed(transcript_path, result, "online ASR returned no transcript text")
        return

    write_text(transcript_path, text + "\n")
    segments = normalized.get("segments") if isinstance(normalized.get("segments"), list) else []
    words = normalized.get("words") if isinstance(normalized.get("words"), list) else []
    if segments:
        write_text(segments_path, render_srt(segments))
        result["transcript_segments_path"] = str(segments_path)
        result["transcript_segments_available"] = True
    if words:
        write_json(
            words_path,
            {
                "schema_version": "flayr.transcript_words.v1",
                "source": str(json_path),
                "provider": "fun-asr",
                "words": words,
            },
        )
        result["transcript_words_path"] = str(words_path)
        result["transcript_words_available"] = True
    result["transcription_language"] = normalized.get("language") or (
        requested_language if requested_language != "auto" else None
    )
    result["detected_language"] = normalized.get("language")
    result["transcription_status"] = "completed"


def _clear_transcript_artifacts(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _mark_asr_failed(transcript_path: Path, result: dict[str, Any], message: str) -> None:
    write_text(transcript_path, ASR_FAILURE_PLACEHOLDER + "\n")
    result["transcription_status"] = "failed"
    result.setdefault("errors", []).append(message)


def _build_asr_payload(model: str, data_url: str, language: str) -> dict[str, Any]:
    """Build the model-specific HTTP shape documented by Beijing MaaS."""
    model_name = str(model or DEFAULT_FUN_ASR_MODEL)
    if model_name.lower().startswith("fun-asr-flash"):
        content: dict[str, Any] = {
            "type": "input_audio",
            "input_audio": {"data": data_url},
        }
    else:
        # Fun-ASR-Realtime's non-streaming HTTP contract uses ``audio``
        # directly in the message content; Flash uses ``input_audio``.
        content = {"audio": data_url}
    payload: dict[str, Any] = {
        "model": model_name,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [content],
                }
            ]
        },
        "parameters": {"format": "mp3"},
        "resources": [],
    }
    if str(language or "auto").strip().lower() != "auto":
        payload["parameters"]["language_hints"] = [str(language).strip().lower()]
    return payload


def _call_asr_endpoint(
    api_url: str,
    api_key: str,
    payload: dict[str, Any],
    role_dir: Path,
    *,
    budget: Any = None,
    response_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        validated = validate_outbound_url(api_url)
    except OutboundURLPolicyError as exc:
        raise SystemExit(str(exc)) from exc
    if not shutil.which("curl"):
        raise SystemExit("online ASR requires curl, but curl was not found")

    active_budget = budget or current_budget()
    logical_request_id = (
        str(response_meta.get("logical_request_id") or "").strip()
        if isinstance(response_meta, dict)
        else ""
    ) or uuid.uuid4().hex
    if isinstance(response_meta, dict):
        response_meta.update(
            {
                "logical_request_id": logical_request_id,
                "request_id": logical_request_id,
                "completion_attempts": 0,
                "retry_reasons": [],
                "usage": {},
                "provider": "fun-asr",
                "api_url": api_url,
                "model": str(payload.get("model") or ""),
            }
        )
    request_limit = (
        active_budget.limits.max_single_request_bytes
        if active_budget is not None
        else 64 * 1024 * 1024
    )
    resolve_entries = _curl_resolve_entries(validated.hostname, validated.port, validated.resolved_addresses)
    with tempfile.TemporaryDirectory(prefix=".flayr-asr-", dir=role_dir) as temp_dir:
        temp_root = Path(temp_dir)
        request_path = temp_root / "request.json"
        request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        request_size = request_path.stat().st_size
        if request_size > request_limit:
            raise SystemExit(f"online ASR request exceeds the single-request byte limit: {request_size}")
        command = [
            "curl",
            "-sS",
            "--http1.1",
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
            str(ASR_REQUEST_TIMEOUT_SECONDS),
            "--max-filesize",
            str(ASR_MAX_RESPONSE_BYTES),
            "-H",
            "@-",
            "-H",
            "Content-Type: application/json",
            "-H",
            "X-DashScope-SSE: disable",
            "-H",
            f"X-Flayr-Request-ID: {logical_request_id}",
            "--data-binary",
            f"@{request_path}",
            "--write-out",
            "%{stderr}__FLAYR_HTTP_STATUS__%{http_code}\\n",
            api_url,
        ]
        command[1:1] = [item for entry in resolve_entries for item in ("--resolve", entry)]
        last_error = ""
        for attempt in range(ASR_RETRIES + 1):
            if active_budget is not None:
                active_budget.reserve_api_call(
                    request_size,
                    kind="asr",
                    request_id=logical_request_id,
                    attempt=attempt + 1,
                    retry_reason=last_error,
                )
            if isinstance(response_meta, dict):
                response_meta["completion_attempts"] = attempt + 1
                if last_error:
                    response_meta["retry_reasons"].append(last_error[:200])
            completed = run_command(
                command,
                timeout_seconds=ASR_REQUEST_TIMEOUT_SECONDS,
                max_output_bytes=ASR_MAX_RESPONSE_BYTES + 64 * 1024,
                stdin_text=f"Authorization: Bearer {api_key}\n",
                budget=active_budget,
            )
            status = _parse_http_status(completed.stderr)
            if isinstance(response_meta, dict):
                response_meta["last_http_status"] = status
                response_meta["attempts"] = attempt + 1
            if completed.returncode == 0 and status is not None and 200 <= status < 300:
                try:
                    parsed = json.loads(completed.stdout)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"online ASR returned invalid JSON: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise ValueError("online ASR response must be a JSON object")
                if isinstance(response_meta, dict) and isinstance(parsed.get("usage"), dict):
                    response_meta["usage"] = parsed["usage"]
                return parsed
            diagnostic = _strip_http_status(completed.stderr).strip()
            last_error = diagnostic or completed.stdout.strip() or "curl failed"
            if status is not None:
                last_error = f"HTTP {status}: {last_error}"
            if not _retryable_asr_error(status, last_error) or attempt >= ASR_RETRIES:
                raise SystemExit(last_error)
            time.sleep(min(5.0 * (attempt + 1), 10.0))
    raise SystemExit(last_error or "online ASR request failed")


def _curl_resolve_entries(hostname: str, port: int, addresses: tuple[str, ...]) -> tuple[str, ...]:
    resolved = []
    for address in addresses:
        curl_address = f"[{address}]" if ":" in address else address
        resolved.append(curl_address)
    if not resolved:
        return ()
    return (f"{hostname}:{port}:{','.join(resolved)}",)


def _parse_http_status(stderr: str) -> int | None:
    matches = re.findall(r"__FLAYR_HTTP_STATUS__(\d{3})", str(stderr or ""))
    if not matches:
        return None
    return int(matches[-1])


def _strip_http_status(stderr: str) -> str:
    return re.sub(r"\s*__FLAYR_HTTP_STATUS__\d{3}\s*", "\n", str(stderr or ""))


def _retryable_asr_error(status: int | None, message: str) -> bool:
    if status is not None:
        return status in {408, 425, 429} or 500 <= status <= 599
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "timeout",
            "timed out",
            "connection reset",
            "empty reply",
            "failed to connect",
            "couldn't connect",
            "could not connect",
            "tls",
        )
    )


def normalize_asr_response(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize Fun-ASR-Realtime and Fun-ASR-Flash response shapes."""
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    layers = [output]
    nested = output.get("output") if isinstance(output.get("output"), dict) else None
    if nested is not None:
        layers.append(nested)

    sentence_values: list[Any] = []
    text_candidates: list[str] = []
    language: str | None = None
    for layer in layers:
        sentence = layer.get("sentence")
        if sentence is not None:
            sentence_values.extend(sentence if isinstance(sentence, list) else [sentence])
        value = layer.get("text")
        if isinstance(value, str) and value.strip():
            text_candidates.append(value.strip())
        if isinstance(layer.get("language"), str) and layer["language"].strip():
            language = layer["language"].strip()

    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for index, value in enumerate(sentence_values, start=1):
        if not isinstance(value, dict):
            continue
        text = str(value.get("text") or "").strip()
        local_words = _normalize_words(value.get("words"))
        if local_words:
            words.extend(local_words)
        start = _time_seconds(value.get("begin_time"), milliseconds=True)
        end = _time_seconds(value.get("end_time"), milliseconds=True)
        if text or local_words:
            if not text:
                text = _join_word_text(local_words)
            segment: dict[str, Any] = {
                "index": int(value.get("sentence_id") or index),
                "text": text,
            }
            if start is not None:
                segment["start_seconds"] = round(start, 3)
            if end is not None:
                segment["end_seconds"] = round(end, 3)
            segments.append(segment)

    text = "\n".join(segment["text"] for segment in segments if segment.get("text")).strip()
    if not text and text_candidates:
        text = max(text_candidates, key=len)
    if not text and words:
        text = _join_word_text(words)
    return {
        "text": text,
        "segments": segments,
        "words": words,
        "language": language,
    }


def extract_word_timestamps(path: Path) -> list[dict[str, Any]]:
    """Read a saved online ASR response and return normalized word timings."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return normalize_asr_response(data).get("words", [])


def _normalize_words(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    words: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("word") or "").strip()
        start = _time_seconds(
            item.get("begin_time", item.get("start_time")),
            milliseconds="begin_time" in item or "start_time" in item,
        )
        end = _time_seconds(
            item.get("end_time", item.get("stop_time")),
            milliseconds="end_time" in item or "stop_time" in item,
        )
        if not text or start is None or end is None or end < start:
            continue
        words.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "text": text,
            }
        )
    return words


def _time_seconds(value: Any, *, milliseconds: bool) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds:
        number /= 1000.0
    return number if math.isfinite(number) and number >= 0 else None


def _join_word_text(words: list[dict[str, Any]]) -> str:
    return " ".join(str(word.get("text") or "").strip() for word in words).strip()


def render_srt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start = segment.get("start_seconds")
        end = segment.get("end_seconds")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if end < start:
            continue
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        lines.extend(
            [
                str(index),
                f"{_format_srt_timestamp(float(start))} --> {_format_srt_timestamp(float(end))}",
                text,
                "",
            ]
        )
    return "\n".join(lines)


def _format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"
