"""flayr_core.voice_clone：达人音色克隆 + 改进话术合成。

链路（全部已实测验证）：
  1. voice_sample 选最干净口播段 → 切样本
  2. DashScope 临时上传样本 → oss:// URL
  3. CosyVoice create_voice 注册音色 → voice_id
  4. CosyVoice wss 合成改进话术 → 本地音频文件

依赖（voice clone 专属可选依赖，主 pipeline 不依赖）：
  - dashscope SDK：CosyVoice 合成只走 WebSocket，REST 不支持克隆音色。
    没装时本模块整体降级（返回 disabled），不影响主分析流程。
  - certifi：dashscope wss 在某些环境（如 python.org 的 macOS Python）
    会因缺 CA bundle 报 SSL 失败，需把 SSL_CERT_FILE 指向 certifi。

关键约束（实测踩出来的）：
  - 注册音色必须用 oss:// + X-DashScope-OssResourceResolve header（私有文件）
  - 样本质量是音色像不像的命门（见 voice_sample）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .utils import run_command
from .voice_sample import cut_voice_sample, select_voice_sample_window


ENROLL_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
UPLOAD_POLICY_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/uploads"
TARGET_MODEL = "cosyvoice-v2"
VOICE_PREFIX = "flayr"


def dashscope_available() -> bool:
    """检测 voice clone 专属依赖是否就位（dashscope SDK）。"""
    try:
        import dashscope  # noqa: F401
        from dashscope.audio.tts_v2 import SpeechSynthesizer  # noqa: F401
        return True
    except ImportError:
        return False


def clone_voice_and_synthesize(
    role_dir: Path,
    audio_path: Path,
    srt_path: Path,
    lines: list[dict[str, Any]],
    api_key: str,
) -> dict[str, Any]:
    """对一个达人完成音色克隆并合成多条改进话术音频。

    lines: [{"id": "p1", "text": "改进话术（本地语言）"}, ...]
    返回 {status, voice_id, sample, outputs: [{id, text, audio_path}|{id, error}]}。
    任何前置缺失（SDK/key/样本）→ 返回 disabled/skipped，不抛错。
    """
    if not dashscope_available():
        return _disabled("dashscope SDK 未安装（voice clone 专属可选依赖）")
    if not api_key.strip():
        return _disabled("缺少 DashScope API key")

    # [1] 选样本 + 切片
    window = select_voice_sample_window(srt_path)
    if window.get("status") == "no_srt":
        return _disabled("无 transcript.srt，无法选音色样本")
    sample_mp3 = role_dir / "voice_sample.mp3"
    if not cut_voice_sample(audio_path, window, sample_mp3):
        return _disabled("音色样本切片失败（ffmpeg 或音频缺失）")

    # [2] 上传样本到 DashScope 临时存储
    oss_url = _upload_to_dashscope(sample_mp3, api_key)
    if not oss_url:
        return _failed("样本上传 DashScope 失败", window)

    # [3] 注册音色
    voice_id = _create_voice(oss_url, api_key)
    if not voice_id:
        return _failed("音色注册失败", window)

    # [4] 合成每条话术
    outputs = []
    try:
        outputs = _synthesize_lines(role_dir, voice_id, lines, api_key)
    finally:
        # 用完即删，不在账号留垃圾音色（音色配额有限）
        _delete_voice(voice_id, api_key)

    result = {
        "status": "ready",
        "voice_id": voice_id,
        "sample": window,
        "outputs": outputs,
    }
    # 样本短于理想时长时把警告提到顶层，避免"音色不像"时还要翻 sample 子字段才发现原因。
    if window.get("status") == "short_sample":
        result["warning"] = window.get("reason") or "音色样本偏短，克隆相似度可能不足"
    return result


def _upload_to_dashscope(file_path: Path, api_key: str) -> str | None:
    """走 DashScope 临时上传，返回 oss:// URL（配合 resolve header 给后续接口用）。"""
    policy = _curl_json([
        f"{UPLOAD_POLICY_ENDPOINT}?action=getPolicy&model={TARGET_MODEL}",
        "-H", f"Authorization: Bearer {api_key}",
    ])
    data = policy.get("data") if isinstance(policy, dict) else None
    if not isinstance(data, dict):
        return None
    # 校验所有必需字段都在，避免畸形/错误响应（带残缺 data）触发 KeyError 崩主流程。
    required = (
        "upload_dir", "upload_host", "oss_access_key_id", "policy",
        "signature", "x_oss_object_acl", "x_oss_forbid_overwrite",
    )
    if any(field not in data for field in required):
        return None
    key = f"{data['upload_dir']}/{file_path.name}"
    upload = run_command([
        "curl", "-sS", "--noproxy", "*", "-m", "120", data["upload_host"],
        "-F", f"OSSAccessKeyId={data['oss_access_key_id']}",
        "-F", f"policy={data['policy']}",
        "-F", f"Signature={data['signature']}",
        "-F", f"key={key}",
        "-F", f"x-oss-object-acl={data['x_oss_object_acl']}",
        "-F", f"x-oss-forbid-overwrite={data['x_oss_forbid_overwrite']}",
        "-F", "success_action_status=200",
        "-F", f"file=@{file_path}",
    ])
    if upload.returncode != 0:
        return None
    return f"oss://{key}"


def _create_voice(oss_url: str, api_key: str) -> str | None:
    """注册克隆音色，返回 voice_id。oss:// 私有文件靠 resolve header 让 DashScope 内部读取。"""
    resp = _curl_json([
        ENROLL_ENDPOINT,
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: application/json",
        "-H", "X-DashScope-OssResourceResolve: enable",
        "-d", json.dumps({"model": "voice-enrollment", "input": {
            "action": "create_voice", "target_model": TARGET_MODEL,
            "prefix": VOICE_PREFIX, "url": oss_url}}),
    ])
    return resp.get("output", {}).get("voice_id") if isinstance(resp, dict) else None


def _delete_voice(voice_id: str, api_key: str) -> None:
    _curl_json([
        ENROLL_ENDPOINT,
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"model": "voice-enrollment", "input": {
            "action": "delete_voice", "voice_id": voice_id}}),
    ])


def _synthesize_lines(
    role_dir: Path,
    voice_id: str,
    lines: list[dict[str, Any]],
    api_key: str,
) -> list[dict[str, Any]]:
    """用 dashscope SDK 的 CosyVoice wss 合成每条话术。"""
    import certifi
    import dashscope
    # 关键：让 wss 的 ssl 用 certifi 的根证书，否则部分环境握手失败
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    # SDK 认证：环境变量有时不被 wss 客户端读取，显式赋值最稳
    dashscope.api_key = api_key
    from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat

    out_dir = role_dir / "voice_lines"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for item in lines:
        line_id = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        if not text:
            results.append({"id": line_id, "error": "空话术"})
            continue
        try:
            synth = SpeechSynthesizer(
                model=TARGET_MODEL, voice=voice_id,
                format=AudioFormat.WAV_24000HZ_MONO_16BIT,
            )
            audio = synth.call(text)
            if not audio:
                results.append({"id": line_id, "error": "合成返回空音频"})
                continue
            audio_path = out_dir / f"{line_id}.wav"
            audio_path.write_bytes(audio)
            results.append({
                "id": line_id, "text": text,
                "audio_path": str(audio_path),
            })
        except Exception as exc:  # noqa: BLE001 — wss 异常类型多，统一兜底不崩主流程
            results.append({"id": line_id, "error": str(exc)[:160]})
    return results


def _curl_json(args: list[str]) -> dict[str, Any]:
    completed = run_command(["curl", "-sS", "--noproxy", "*", "-m", "60"] + args)
    try:
        return json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}


def _disabled(reason: str) -> dict[str, Any]:
    return {"status": "disabled", "disabled_reason": reason, "voice_id": None, "outputs": []}


def _failed(reason: str, window: dict[str, Any]) -> dict[str, Any]:
    return {"status": "failed", "error": reason, "sample": window, "voice_id": None, "outputs": []}
