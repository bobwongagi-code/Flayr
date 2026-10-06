"""Company-native transport, using the existing completion return contract."""

from __future__ import annotations

import base64
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from .gemini_gateway import (
    assemble_native_response, build_native_request, canonical_qualification_wire,
    normalize_native_json, replace_native_video_with_url,
)
from .r2_video import R2VideoConfig, upload_video_url
from ..network import ROOTIFY_COMPANY_LLM_API_HOST, validate_outbound_url
from ..utils import run_command


def call_native_completion(
    api_key: str, payload: dict[str, Any], raw_path: Path, *,
    max_time_seconds: int, retries: int, budget: Any, call_kind: str,
    request_id: str, response_meta: dict[str, Any] | None, cleanup_raw: bool,
) -> str:
    # Helpers are imported lazily to keep api.py's native dispatch acyclic.
    from .api import _build_streaming_curl_command, _curl_resolve_entries, parse_curl_http_status

    model = payload["model"]
    if model != "gemini-3.7-flash" or not api_key or any(char in api_key for char in "\r\n"):
        raise SystemExit("Invalid native model or credential")
    endpoint = f"https://{ROOTIFY_COMPANY_LLM_API_HOST}/v1beta/models/{model}:generateContent"
    validated = validate_outbound_url(endpoint, allowed_hosts={ROOTIFY_COMPANY_LLM_API_HOST})
    plain = call_kind in {"ocr", "translation"}
    native = build_native_request(
        payload, response_schema=payload.get("_flayr_native_schema"),
        response_mime_type="text/plain" if plain else "application/json",
    )
    video_hashes = []
    for contents in native["contents"]:
        for part in list(contents["parts"]):
            inline = part.get("inlineData", {})
            if inline.get("mimeType") == "video/mp4":
                uri, digest = upload_video_url(base64.b64decode(inline["data"], validate=True),
                                              R2VideoConfig.from_environment(), budget=budget)
                native = replace_native_video_with_url(native, video_sha256=digest, video_uri=uri)
                video_hashes.append(digest)
    data = json.dumps(native, ensure_ascii=False, separators=(",", ":"))
    request_bytes = len(data.encode())
    limit = budget.limits.max_single_request_bytes if budget else 64 * 1024 * 1024
    if request_bytes > limit:
        raise SystemExit("Native request exceeds the single-request byte limit")
    attempts: list[dict[str, Any]] = []
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".gemini-native.", dir=raw_path.parent) as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(data, encoding="utf-8")
            for index in range(min(max(0, retries) + 1, 3)):
                timeout = max_time_seconds
                if budget is not None:
                    timeout = min(timeout, max(1, int(budget.remaining_wall_seconds() - 30)))
                    budget.reserve_api_call(request_bytes, kind=call_kind, request_id=request_id,
                                            attempt=index + 1, retry_reason="same native request" if index else "")
                command = _build_streaming_curl_command(
                    api_url=endpoint, resolve_entries=_curl_resolve_entries(validated),
                    logical_request_id=request_id, request_path=request_path,
                    max_time_seconds=timeout, low_speed_time_seconds=timeout, request_limit=limit,
                )
                header_path = Path(directory) / "response.headers"
                command[1:1] = ["--dump-header", str(header_path)]
                started = time.monotonic()
                completed = run_command(command, timeout_seconds=timeout + 5, max_output_bytes=limit,
                                        budget=budget, stdin_text=f"Authorization: Bearer {api_key}\n")
                status = parse_curl_http_status(completed.stderr)
                headers = header_path.read_text(encoding="utf-8", errors="replace")[:65536] if header_path.exists() else ""
                provider_request_id = next((line.split(":", 1)[1].strip()[:200] for line in headers.splitlines()
                                            if line.lower().startswith("x-oneapi-request-id:")), None)
                attempts.append({"http_status": status, "curl_exit_code": completed.returncode,
                                 "provider_request_id": provider_request_id,
                                 "elapsed_seconds": round(time.monotonic() - started, 3)})
                if budget is not None:
                    budget.reserve_download(len(completed.stdout.encode()))
                if response_meta is not None:
                    response_meta.update(logical_request_id=request_id, transport_attempts=len(attempts),
                                         native_attempts=list(attempts), transport="company_gemini_native",
                                         video_sha256=video_hashes, request_bytes=request_bytes)
                if completed.returncode or status != 200:
                    # Retain failures without disclosing signed URLs or credentials.
                    failure = raw_path.with_name(raw_path.name + f".native-failure-{index + 1}.json")
                    failure.write_text(json.dumps(attempts[-1]) + "\n", encoding="utf-8")
                    transient = status in {408, 429, 500, 502, 503, 504} or completed.returncode in {28, 52, 55, 56}
                    retry_allowed = index == 0 or (index == 1 and status == 504 and attempts[0]["http_status"] == 504)
                    if transient and retry_allowed and index < min(retries, 2):
                        time.sleep(5)
                        continue
                    raise SystemExit(f"Native gateway transport failed: HTTP={status}, curl={completed.returncode}")
                assembled = assemble_native_response([json.loads(completed.stdout)], expected_model=model)
                content = assembled["content"]
                if not plain:
                    content = normalize_native_json(content)
                    targets = payload.get("_flayr_qualification_stages")
                    if targets is not None:
                        content = json.dumps(canonical_qualification_wire(json.loads(content), targets), ensure_ascii=False)
                native_usage = assembled["usage_metadata"] or {}
                usage = {
                    "prompt_tokens": native_usage.get("promptTokenCount", 0),
                    "completion_tokens": native_usage.get("candidatesTokenCount", 0) + native_usage.get("thoughtsTokenCount", 0),
                    "total_tokens": native_usage.get("totalTokenCount", 0),
                }
                response = {"model": model, "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                            "usage": usage, "usage_metadata": native_usage}
                raw = json.dumps(response, ensure_ascii=False)
                if not cleanup_raw:
                    raw_path.write_text(raw, encoding="utf-8")
                if response_meta is not None:
                    response_meta.update(status="completed", response_model=model,
                                         usage=usage, usage_metadata=native_usage,
                                         transport_retry_reasons=["same native request"] * (len(attempts) - 1))
                return raw
    except (ValueError, OSError) as exc:
        if response_meta is not None:
            response_meta["status"] = "failed"
        failure = raw_path.with_name(raw_path.name + ".native-answer-failure.json")
        failure.write_text(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                                      "reason": str(exc)[:240], "attempts": attempts}) + "\n", encoding="utf-8")
        raise SystemExit(f"Native answer rejected: {exc}") from exc
    finally:
        if cleanup_raw:
            raw_path.unlink(missing_ok=True)
    raise SystemExit("Native request had no permitted transport attempts")
