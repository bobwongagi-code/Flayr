"""flayr_core.llm.api：LLM HTTP 调用底层。

只负责"和 LLM HTTP 服务对话"这件事，不包含任何业务规则、payload 构造或结果校验。
translation 等只需要 API 调用的下游模块应该直接 import 本模块，
而不是通过 llm package 拉入整套业务规则。
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..utils import run_command


def read_llm_api_key(args: argparse.Namespace) -> str:
    """优先从环境变量读取 API key，回退到 macOS Keychain。"""
    env_key = os.environ.get(args.llm_api_key_env, "")
    if env_key.strip():
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
    return completed.stdout


def call_llm_api(api_url: str, api_key: str, payload_path: Path, raw_path: Path) -> str:
    """调用 OpenAI 兼容的 chat completions endpoint，返回原始响应文本。

    优先使用 urllib，遇到 SSL 证书问题且系统有 curl 时回退到 curl。
    任何失败都抛 SystemExit，让上层 fail-loud。
    """
    payload_bytes = payload_path.read_bytes()
    request = urllib.request.Request(
        api_url,
        data=payload_bytes,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"LLM request failed: HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc) or not shutil.which("curl"):
            raise SystemExit(f"LLM request failed: {exc}") from exc

    curl_command = [
        "curl",
        "-sS",
        "--fail-with-body",
        "-H",
        f"Authorization: Bearer {api_key}",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        f"@{payload_path}",
        api_url,
        "-o",
        str(raw_path),
    ]
    completed = run_command(curl_command)
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip()
        raise SystemExit(f"LLM request failed via curl: {error_text}")
    return raw_path.read_text(encoding="utf-8")


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


def video_to_data_url(path: Path, fps: float = 3.0, max_width: int = 480) -> str | None:
    """把本地视频重编码成小体积 mp4 的 base64 data URL，供 omni 原生视频理解。

    用 ffmpeg 在客户端直接控制抽帧密度（fps）和分辨率，不依赖 API 端 fps 参数：
      - fps：较高（3~5）抓住转场/表情突变/特效高潮等变化点；
      - max_width：降到 480 宽（保持宽高比），把 base64 payload 压到几 MB，
        既够 omni 看清画面又规避大 body 走代理失败的问题；
      - 音轨完整保留（omni 据此听 BGM / 语气 / 音效）。

    ffmpeg 不可用或转码失败时返回 None，调用方应回退到抽帧+音频模式。
    """
    if not path.is_file():
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            [
                ffmpeg, "-y", "-i", str(path),
                "-vf", f"fps={fps},scale={max_width}:-2",
                "-c:v", "libx264", "-crf", "28", "-preset", "veryfast",
                "-c:a", "aac", "-b:a", "64k",
                "-movflags", "+faststart",
                tmp_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        encoded = base64.b64encode(Path(tmp_path).read_bytes()).decode("ascii")
        return f"data:video/mp4;base64,{encoded}"
    except (subprocess.CalledProcessError, OSError):
        return None
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


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
