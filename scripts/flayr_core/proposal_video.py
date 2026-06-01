"""DashScope/Wan adapters for proposal demo clips."""

from __future__ import annotations

import argparse
import json
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .llm.api import audio_to_mp3_data_url, image_to_data_url, read_llm_api_key
from .utils import run_command, write_json


I2V_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
S2V_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2video/video-synthesis"
S2V_DETECT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2video/face-detect"


@dataclass(frozen=True)
class ProposalVideoConfig:
    backend: str = "none"
    model: str = ""
    api_url: str = ""
    resolution: str = "720P"
    timeout_seconds: int = 600
    poll_interval_seconds: int = 15
    submit_only: bool = False
    face_image_url: str = ""
    line_audio_url: str = ""
    s2v_detect: bool = True
    api_key: str = ""


@dataclass(frozen=True)
class ClipRefs:
    prompt: str
    anchor_frame_path: Path | None
    output_path: Path
    duration_sec: float
    face_image_url: str = ""
    line_audio_url: str = ""
    # voice clone 合成的本地话术音频；有则 i2v 打开 audio 做口型同步（实测可用本地 base64）。
    line_audio_path: Path | None = None


def config_from_args(args: argparse.Namespace) -> ProposalVideoConfig:
    backend = getattr(args, "proposal_video_backend", "none")
    model = getattr(args, "proposal_video_model", "") or default_model_for_backend(backend)
    api_url = getattr(args, "proposal_video_api_url", "") or default_endpoint_for_backend(backend)
    return ProposalVideoConfig(
        backend=backend,
        model=model,
        api_url=api_url,
        resolution=getattr(args, "proposal_video_resolution", "720P"),
        timeout_seconds=max(0, int(getattr(args, "proposal_video_timeout", 600))),
        poll_interval_seconds=max(1, int(getattr(args, "proposal_video_poll_interval", 15))),
        submit_only=bool(getattr(args, "proposal_video_submit_only", False)),
        face_image_url=getattr(args, "proposal_face_image_url", "") or "",
        line_audio_url=getattr(args, "proposal_line_audio_url", "") or "",
        s2v_detect=not bool(getattr(args, "proposal_skip_s2v_detect", False)),
        api_key=read_llm_api_key(args).strip() if backend != "none" else "",
    )


def default_model_for_backend(backend: str) -> str:
    if backend == "dashscope-i2v":
        return "wan2.6-i2v-flash"
    if backend == "dashscope-s2v":
        return "wan2.2-s2v"
    return ""


def default_endpoint_for_backend(backend: str) -> str:
    if backend == "dashscope-i2v":
        return I2V_ENDPOINT
    if backend == "dashscope-s2v":
        return S2V_ENDPOINT
    return ""


def maybe_generate_ai_clip(
    config: ProposalVideoConfig,
    refs: ClipRefs,
    trace_prefix: Path,
) -> dict[str, Any]:
    if config.backend == "none":
        return {
            "ai_generation_status": "not_configured",
            "clip_ai_uri": None,
            "is_ai_simulated": False,
            "ai_generation_error": "AI demo clip backend is not configured.",
        }
    if not config.api_key.strip():
        return {
            "ai_generation_status": "missing_api_key",
            "clip_ai_uri": None,
            "is_ai_simulated": False,
            "ai_generation_error": "DashScope API key missing.",
        }
    if config.backend == "dashscope-i2v":
        return generate_i2v_clip(config, refs, trace_prefix)
    if config.backend == "dashscope-s2v":
        return generate_s2v_clip(config, refs, trace_prefix)
    return {
        "ai_generation_status": "unsupported_backend",
        "clip_ai_uri": None,
        "is_ai_simulated": False,
        "ai_generation_error": f"Unsupported proposal video backend: {config.backend}",
    }


def generate_i2v_clip(
    config: ProposalVideoConfig,
    refs: ClipRefs,
    trace_prefix: Path,
) -> dict[str, Any]:
    if not refs.anchor_frame_path or not refs.anchor_frame_path.is_file():
        return {
            "ai_generation_status": "missing_anchor_frame",
            "clip_ai_uri": None,
            "is_ai_simulated": False,
            "ai_generation_error": "No local anchor frame available for DashScope i2v.",
        }
    payload: dict[str, Any] = {
        "model": config.model,
        "input": {
            "prompt": refs.prompt,
            "img_url": image_to_data_url(refs.anchor_frame_path),
        },
        "parameters": {
            "resolution": config.resolution,
            "prompt_extend": True,
            "duration": normalized_i2v_duration(config.model, refs.duration_sec),
            "watermark": True,
            "shot_type": "single",
        },
    }
    # 有 voice clone 合成的本地话术音频 → 打开 audio 做口型同步（实测 i2v 吃本地 base64）。
    # 否则保持 audio=False（纯画面生成，不配音）。
    audio_data_url = (
        audio_to_mp3_data_url(refs.line_audio_path)
        if refs.line_audio_path and refs.line_audio_path.is_file()
        else None
    )
    if audio_data_url:
        payload["input"]["audio_url"] = audio_data_url
        payload["parameters"]["audio"] = True
    elif config.model == "wan2.6-i2v-flash":
        payload["parameters"]["audio"] = False
    return submit_and_resolve_video_task(config, payload, trace_prefix, refs.output_path)


def generate_s2v_clip(
    config: ProposalVideoConfig,
    refs: ClipRefs,
    trace_prefix: Path,
) -> dict[str, Any]:
    face_image_url = refs.face_image_url or config.face_image_url
    line_audio_url = refs.line_audio_url or config.line_audio_url
    if not face_image_url or not line_audio_url:
        return {
            "ai_generation_status": "missing_public_refs",
            "clip_ai_uri": None,
            "is_ai_simulated": False,
            "ai_generation_error": "wan2.2-s2v requires public HTTP(S) face image and line audio URLs.",
        }
    if config.s2v_detect:
        detect = detect_s2v_face(config, face_image_url, trace_prefix)
        if not detect.get("check_pass"):
            return {
                "ai_generation_status": "face_detect_failed",
                "clip_ai_uri": None,
                "is_ai_simulated": False,
                "ai_generation_error": detect.get("message") or "S2V face detect did not pass.",
                "face_detect": detect,
            }
    payload = {
        "model": config.model,
        "input": {
            "image_url": face_image_url,
            "audio_url": line_audio_url,
        },
        "parameters": {
            "resolution": config.resolution,
        },
    }
    return submit_and_resolve_video_task(config, payload, trace_prefix, refs.output_path)


def detect_s2v_face(config: ProposalVideoConfig, image_url: str, trace_prefix: Path) -> dict[str, Any]:
    payload = {"model": "wan2.2-s2v-detect", "input": {"image_url": image_url}}
    request_path = trace_prefix.with_name(f"{trace_prefix.name}_detect_request.json")
    response_path = trace_prefix.with_name(f"{trace_prefix.name}_detect_response.json")
    write_json(request_path, payload)
    try:
        response = post_json(S2V_DETECT_ENDPOINT, config.api_key, payload, async_call=False)
    except RuntimeError as exc:
        return {"check_pass": False, "message": str(exc)}
    write_json(response_path, response)
    output = response.get("output", {}) if isinstance(response, dict) else {}
    return {
        "check_pass": bool(output.get("check_pass")),
        "humanoid": bool(output.get("humanoid")),
        "message": output.get("message") or "",
        "request_id": response.get("request_id", ""),
    }


def submit_and_resolve_video_task(
    config: ProposalVideoConfig,
    payload: dict[str, Any],
    trace_prefix: Path,
    output_path: Path,
) -> dict[str, Any]:
    request_path = trace_prefix.with_name(f"{trace_prefix.name}_request.json")
    response_path = trace_prefix.with_name(f"{trace_prefix.name}_response.json")
    write_json(request_path, payload)
    try:
        created = post_json(config.api_url, config.api_key, payload, async_call=True)
    except RuntimeError as exc:
        return ai_error("submit_failed", str(exc))
    write_json(response_path, created)
    task_id = str(created.get("output", {}).get("task_id") or "")
    if not task_id:
        return ai_error("submit_failed", "DashScope response did not contain output.task_id.", created)
    if config.submit_only:
        return {
            "ai_generation_status": "submitted",
            "clip_ai_uri": None,
            "is_ai_simulated": True,
            "ai_task_id": task_id,
            "ai_generation_error": "Task submitted; polling was skipped.",
        }
    resolved = poll_video_task(config, task_id, response_path)
    if resolved.get("status") != "SUCCEEDED":
        return {
            "ai_generation_status": "failed",
            "clip_ai_uri": None,
            "is_ai_simulated": True,
            "ai_task_id": task_id,
            "ai_generation_error": resolved.get("message") or "DashScope task did not succeed.",
            "ai_task_status": resolved.get("status", ""),
        }
    video_url = str(resolved.get("video_url") or "")
    if not video_url:
        return ai_error("failed", "DashScope task succeeded without output.results.video_url.", resolved)
    try:
        download_file(video_url, output_path)
    except RuntimeError as exc:
        return {
            "ai_generation_status": "download_failed",
            "clip_ai_uri": None,
            "is_ai_simulated": True,
            "ai_task_id": task_id,
            "ai_generation_error": str(exc),
            "ai_task_status": "SUCCEEDED",
        }
    return {
        "ai_generation_status": "ready",
        "clip_ai_uri": f"{output_path.parent.name}/{output_path.name}",
        "is_ai_simulated": True,
        "ai_task_id": task_id,
        "ai_task_status": "SUCCEEDED",
        "ai_generation_error": "",
    }


def poll_video_task(config: ProposalVideoConfig, task_id: str, response_path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + config.timeout_seconds
    query_url = task_query_url(config.api_url, task_id)
    last_response: dict[str, Any] = {}
    while True:
        try:
            last_response = get_json(query_url, config.api_key)
        except RuntimeError as exc:
            return {"status": "FAILED", "message": str(exc)}
        write_json(response_path, last_response)
        output = last_response.get("output", {}) if isinstance(last_response, dict) else {}
        status = str(output.get("task_status") or "")
        if status == "SUCCEEDED":
            return {
                "status": status,
                "video_url": output.get("video_url") or output.get("results", {}).get("video_url"),
                "message": "",
            }
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            return {
                "status": status,
                "message": output.get("message") or output.get("code") or status,
            }
        if time.monotonic() >= deadline:
            return {"status": status or "TIMEOUT", "message": "Timed out waiting for DashScope task."}
        time.sleep(config.poll_interval_seconds)


def post_json(url: str, api_key: str, payload: dict[str, Any], async_call: bool) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if async_call:
        headers["X-DashScope-Async"] = "enable"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        return read_json_response(request)
    except RuntimeError as exc:
        if not should_fallback_to_curl(exc):
            raise
        return curl_json("POST", url, api_key, payload, async_call)


def get_json(url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"}, method="GET")
    try:
        return read_json_response(request)
    except RuntimeError as exc:
        if not should_fallback_to_curl(exc):
            raise
        return curl_json("GET", url, api_key, None, False)


def read_json_response(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response: {text[:300]}") from exc


def download_file(url: str, output_path: Path) -> None:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            output_path.write_bytes(response.read())
    except urllib.error.URLError as exc:
        if not should_fallback_to_curl(RuntimeError(str(exc))):
            raise RuntimeError(str(exc)) from exc
        completed = run_command(["curl", "-L", "-sS", "--fail-with-body", url, "-o", str(output_path)])
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "curl download failed") from exc


def curl_json(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None,
    async_call: bool,
) -> dict[str, Any]:
    if not shutil.which("curl"):
        raise RuntimeError("SSL certificate verification failed and curl is unavailable.")
    with NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=True) as payload_file, NamedTemporaryFile(
        "r", encoding="utf-8", suffix=".json", delete=True
    ) as response_file:
        command = [
            "curl",
            "-sS",
            "--fail-with-body",
            "-X",
            method,
            "-H",
            f"Authorization: Bearer {api_key}",
        ]
        if async_call:
            command.extend(["-H", "X-DashScope-Async: enable"])
        if payload is not None:
            payload_file.write(json.dumps(payload, ensure_ascii=False))
            payload_file.flush()
            command.extend(["-H", "Content-Type: application/json", "--data-binary", f"@{payload_file.name}"])
        command.extend([url, "-o", response_file.name])
        completed = run_command(command)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "curl request failed")
        text = Path(response_file.name).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response: {text[:300]}") from exc


def should_fallback_to_curl(exc: RuntimeError) -> bool:
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def task_query_url(api_url: str, task_id: str) -> str:
    prefix = api_url.split("/api/v1/", 1)[0]
    return f"{prefix}/api/v1/tasks/{task_id}"


def normalized_i2v_duration(model: str, duration_sec: float) -> int:
    duration = max(2, min(5, round(duration_sec)))
    if model == "wan2.5-i2v-preview":
        return 5
    return duration


def ai_error(status: str, message: str, raw: Any | None = None) -> dict[str, Any]:
    result = {
        "ai_generation_status": status,
        "clip_ai_uri": None,
        "is_ai_simulated": False,
        "ai_generation_error": message,
    }
    if raw is not None:
        result["ai_raw_response"] = raw
    return result
