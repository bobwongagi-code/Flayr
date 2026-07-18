"""flayr_core.llm.api：LLM HTTP 调用底层。

只负责"和 LLM HTTP 服务对话"这件事，不包含任何业务规则、payload 构造或结果校验。
translation 等只需要 API 调用的下游模块应该直接 import 本模块，
而不是通过 llm package 拉入整套业务规则。
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..utils import run_command, write_text

LLM_CURL_MAX_TIME_SECONDS = 1800
LLM_CURL_LOW_SPEED_LIMIT_BYTES_PER_SECOND = 1
LLM_CURL_LOW_SPEED_TIME_SECONDS = 180
LLM_CURL_RETRIES = 2
LLM_MAX_OUTPUT_TOKENS = 32768
VIDEO_DATA_URL_MAX_DURATION_SECONDS = 180.0
VIDEO_DATA_URL_MAX_BYTES = 24 * 1024 * 1024
VIDEO_TRANSCODE_TIMEOUT_SECONDS = 300


def is_agent_plan_api_url(api_url: str) -> bool:
    """Return whether the endpoint is Volcano Engine Agent Plan's OpenAI-compatible API."""
    normalized = str(api_url or "").lower().rstrip("/")
    return "ark.cn-beijing.volces.com/api/plan/" in normalized


def is_doubao_model(model: str) -> bool:
    """Return whether the configured analysis model is a Doubao family model."""
    return str(model or "").strip().lower().startswith("doubao-")


def supports_standalone_audio(api_url: str) -> bool:
    """Whether chat content may contain OpenAI-style ``input_audio`` blocks."""
    # Agent Plan rejects standalone input_audio blocks. Other compatible
    # providers retain the existing behavior unless a capability test proves otherwise.
    return not is_agent_plan_api_url(api_url)


def supports_native_audio_analysis(api_url: str, model: str = "") -> bool:
    """Whether the configured analysis path can directly perceive the audio waveform.

    This differs from video transport support. Agent Plan accepts MP4 input, but the
    currently available Seed 2.0 models did not perceive embedded speech in capability
    tests and reject standalone input_audio, so they must use transcript + local QC.
    """
    return bool(str(model or "").strip()) and not is_agent_plan_api_url(api_url)


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


def call_llm_api(
    api_url: str,
    api_key: str,
    payload_path: Path,
    raw_path: Path,
    *,
    max_time_seconds: int = LLM_CURL_MAX_TIME_SECONDS,
    low_speed_time_seconds: int = LLM_CURL_LOW_SPEED_TIME_SECONDS,
    retries: int = LLM_CURL_RETRIES,
) -> str:
    """流式（SSE）调用 OpenAI 兼容 chat completions，分块拼装后合成标准 completion JSON 返回。

    为什么流式：非流式下大响应体会被网络/代理按体积静默截断（无错误、finish_reason=None、
    body 不完整），导致 JSON 残缺，且无法靠重发修复（重发也截断在同一长度）。流式分块传输
    规避"单个大 body 被切"，并能用 data:[DONE] 哨兵可靠判断是否完整；连接中途断则整次重试。
    返回值仍是标准 {"choices":[{"message":{"content":...}}]} 形状，下游无需改动。
    """
    if not shutil.which("curl"):
        raise SystemExit("LLM streaming 需要 curl，但系统未找到 curl。")

    payload = json.loads(payload_path.read_bytes())
    payload["stream"] = True
    stream_options = payload.get("stream_options")
    if not isinstance(stream_options, dict):
        stream_options = {}
    stream_options["include_usage"] = True
    payload["stream_options"] = stream_options
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # 请求体可能内嵌完整视频/音频，认证头也包含密钥。两者只用 0600 临时文件传给 curl，
    # 调用结束立即清理；运行目录只保留正式响应，避免重复载荷长期占盘或扩大敏感面。
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{raw_path.stem}.stream_req.",
        suffix=".json",
        dir=raw_path.parent,
        delete=False,
    ) as req_file:
        json.dump(payload, req_file, ensure_ascii=False)
        req_path = Path(req_file.name)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{raw_path.stem}.auth.",
        suffix=".txt",
        dir=raw_path.parent,
        delete=False,
    ) as auth_file:
        auth_file.write(f"Authorization: Bearer {api_key}\n")
        auth_path = Path(auth_file.name)

    max_time_seconds = max(1, int(max_time_seconds))
    low_speed_time_seconds = max(1, int(low_speed_time_seconds))
    retries = max(0, int(retries))
    curl_command = [
        "curl",
        "-sS",
        "--http1.1",
        "--no-buffer",
        "--fail-with-body",  # HTTP 4xx/5xx 时返回非零并把错误体写入 -o，便于区分硬错误
        "--connect-timeout",
        "30",
        "--max-time",
        str(max_time_seconds),
        "--speed-limit",
        str(LLM_CURL_LOW_SPEED_LIMIT_BYTES_PER_SECOND),
        "--speed-time",
        str(low_speed_time_seconds),
        "-H",
        f"@{auth_path}",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        f"@{req_path}",
        api_url,
    ]

    last_error = ""
    attempt_paths: list[Path] = []
    try:
        for attempt in range(retries + 1):
            with tempfile.NamedTemporaryFile(
                prefix=f".{raw_path.stem}.attempt-{attempt + 1}.",
                suffix=".sse",
                dir=raw_path.parent,
                delete=False,
            ) as attempt_file:
                attempt_sse_path = Path(attempt_file.name)
            attempt_paths.append(attempt_sse_path)
            completed = run_command([*curl_command, "-o", str(attempt_sse_path)])
            if completed.returncode != 0:
                body = attempt_sse_path.read_text(encoding="utf-8", errors="replace").strip()[:400] if attempt_sse_path.is_file() else ""
                last_error = completed.stderr.strip() or completed.stdout.strip() or "curl failed"
                if body:
                    last_error = f"{last_error}\n{body}"
                # 鉴权/请求错误等硬错误快速失败，不浪费重试。
                if not is_retryable_error(last_error):
                    break
            else:
                content, usage, complete, finish_reason = parse_sse_stream(attempt_sse_path)
                if complete and content and finish_reason != "length":
                    response: dict[str, Any] = {
                        "choices": [{"message": {"content": content}, "finish_reason": finish_reason or "stop"}]
                    }
                    if usage:
                        response["usage"] = usage
                    raw_text = json.dumps(response, ensure_ascii=False)
                    write_text(raw_path, raw_text)
                    return raw_text
                if finish_reason == "length":
                    # length 是服务端主动截断，不是可修复的残缺 JSON。先提高同一请求的输出预算再重发。
                    old_budget, new_budget = increase_output_budget(payload)
                    if new_budget > old_budget:
                        write_text(req_path, json.dumps(payload, ensure_ascii=False))
                        last_error = f"输出被 max_tokens={old_budget} 截断，已提高至 {new_budget} 后重试"
                    else:
                        last_error = f"输出在 max_tokens={old_budget} 仍被截断"
                else:
                    # 流被中途截断（无 [DONE]/finish_reason）→ 传输问题，可重试。
                    last_error = "流式响应不完整（连接在 [DONE] 前中断）" if content else "流式响应无内容"
            if attempt >= retries:
                break
            time.sleep(5 * (attempt + 1))
        raise SystemExit(f"LLM streaming request failed: {last_error}")
    finally:
        auth_path.unlink(missing_ok=True)
        req_path.unlink(missing_ok=True)
        for attempt_path in attempt_paths:
            attempt_path.unlink(missing_ok=True)


def increase_output_budget(payload: dict[str, Any]) -> tuple[int, int]:
    """length 截断时把单次输出预算翻倍，最多到服务端兼容上限。"""
    try:
        old_budget = int(payload.get("max_tokens") or 8192)
    except (TypeError, ValueError):
        old_budget = 8192
    new_budget = min(max(old_budget * 2, 16384), LLM_MAX_OUTPUT_TOKENS)
    payload["max_tokens"] = new_budget
    return old_budget, new_budget


def is_retryable_error(error_text: str) -> bool:
    """传输/服务端瞬时错误才重试；鉴权/请求错误（401/400/403 等）不重试，快速失败。"""
    lowered = error_text.lower()
    if any(marker in lowered for marker in ("401", "403", "400", "unauthorized", "forbidden", "invalid api", "bad request")):
        return False
    return any(
        marker in lowered
        for marker in (
            "timed out", "timeout", "connection reset", "recv failure", "empty reply",
            "transfer closed with outstanding read data", "curl: (18)",
            "framing layer", "http/2", "429", "too many requests",
            "500", "502", "503", "504", "internal_server_error", "backend",
            "could not resolve", "connection refused",
            "ssl_error_syscall", "ssl_connect", "tls handshake",
        )
    )


def parse_sse_stream(sse_path: Path) -> tuple[str, dict[str, Any] | None, bool, str | None]:
    """解析 SSE 文件，拼装 delta.content；返回 (内容, usage, 是否完整, finish_reason)。

    完整判据：出现 data:[DONE] 或某 chunk 带 finish_reason。两者都没有 = 流被中途截断。
    finish_reason 用于区分 stop（正常）与 length（输出预算截断，调用方应提高预算后重发）。
    """
    if not sse_path.is_file():
        return "", None, False, None
    parts: list[str] = []
    usage: dict[str, Any] | None = None
    complete = False
    finish_reason: str | None = None
    for raw_line in sse_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            complete = True
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        choices = chunk.get("choices") or []
        if choices:
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
            reason = choices[0].get("finish_reason")
            if reason:
                complete = True
                finish_reason = reason
        if chunk.get("usage"):
            usage = chunk["usage"]
    return "".join(parts), usage, complete, finish_reason


def extract_chat_completion_text(response: dict[str, Any]) -> str:
    """从 OpenAI 兼容响应中提取文本内容，兼容 chat.completions 和 responses 两种 schema。"""
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


def image_to_data_url(path: Path) -> str:
    """把本地图片读成 base64 data URL，供多模态 LLM 输入。"""
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def video_to_data_url(
    path: Path,
    fps: float = 3.0,
    max_width: int = 480,
    start: float | None = None,
    duration: float | None = None,
    max_duration_seconds: float = VIDEO_DATA_URL_MAX_DURATION_SECONDS,
    max_data_bytes: int = VIDEO_DATA_URL_MAX_BYTES,
    timeout_seconds: int = VIDEO_TRANSCODE_TIMEOUT_SECONDS,
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
    if not path.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    if duration is None:
        media_duration = probe_media_duration_seconds(path)
        if media_duration is not None and media_duration > max_duration_seconds:
            return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
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
            "-movflags", "+faststart",
            tmp_path,
        ]
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        if Path(tmp_path).stat().st_size > max_data_bytes:
            return None
        encoded = base64.b64encode(Path(tmp_path).read_bytes()).decode("ascii")
        return f"data:video/mp4;base64,{encoded}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def probe_media_duration_seconds(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
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
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def audio_to_mp3_data_url(
    path: Path,
    start: float | None = None,
    duration: float | None = None,
) -> str | None:
    """把本地音频（通常是 audio.wav）转成 mp3 的 base64 data URL，供 omni 模型听音轨。

    wav 体积大，先用 ffmpeg 压成 64k mp3 再内联，避免 payload 过大。
    传入 start/duration 时只切对应时间窗（秒），用于阶段二按变化点切片、声画对齐。
    ffmpeg 不可用或转码失败时返回 None，调用方应跳过音频输入（降级处理）。
    """
    if not path.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        command = [ffmpeg, "-y"]
        if start is not None:
            command += ["-ss", str(max(0.0, start))]
        if duration is not None:
            command += ["-t", str(max(0.1, duration))]
        command += ["-i", str(path), "-vn", "-acodec", "libmp3lame", "-b:a", "64k", tmp_path]
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        encoded = base64.b64encode(Path(tmp_path).read_bytes()).decode("ascii")
        return f"data:audio/mp3;base64,{encoded}"
    except (subprocess.CalledProcessError, OSError):
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
