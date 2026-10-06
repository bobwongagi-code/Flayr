"""Private, hash-bound R2 video URLs for the native gateway transport."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ..network import validate_outbound_url
from ..utils import run_command


@dataclass(frozen=True, repr=False)
class R2VideoConfig:
    account: str
    bucket: str
    token: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", self.account):
            raise ValueError("FLAYR_R2_ACCOUNT_ID must be an explicit account ID")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise ValueError("FLAYR_R2_BUCKET must be an explicit bucket name")
        if not self.token or any(char in self.token for char in '\r\n"\\'):
            raise ValueError("FLAYR_R2_API_TOKEN is missing or malformed")

    @classmethod
    def from_environment(cls) -> R2VideoConfig:
        result = cls(*(os.environ.get(key, "").strip() for key in (
            "FLAYR_R2_ACCOUNT_ID", "FLAYR_R2_BUCKET", "FLAYR_R2_API_TOKEN")))
        return result


def _presigned_get(url: str, access: str, secret: str, now: datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]
    scope = f"{date}/auto/s3/aws4_request"
    query = urlencode(sorted({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access}/{scope}",
        "X-Amz-Date": stamp, "X-Amz-Expires": "3600", "X-Amz-SignedHeaders": "host",
    }.items()), quote_via=quote, safe="")
    parsed = urlsplit(url)
    canonical = f"GET\n{parsed.path}\n{query}\nhost:{parsed.netloc}\n\nhost\nUNSIGNED-PAYLOAD"
    to_sign = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signing_key = ("AWS4" + secret).encode()
    for value in (date, "auto", "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{url}?{query}&X-Amz-Signature={signature}"


def _request(url: str, config: str, *, budget: Any, callback: Any = None, limit: int = 65536) -> str:
    host = urlsplit(url).hostname
    validated = validate_outbound_url(url, allowed_hosts={host})
    command = ["curl", "-sS", "--noproxy", "*", "--proto", "=https", "--max-redirs", "0",
               "--fail-with-body", "--connect-timeout", "30", "--max-time", "120",
               "--max-filesize", str(limit), "--config", "-",
               "--write-out", "%{stderr}__FLAYR_R2_STATUS__%{http_code}\\n"]
    for address in validated.resolved_addresses:
        command.extend(["--resolve", f"{host}:443:[{address}]" if ":" in address else f"{host}:443:{address}"])
    # Signed URLs and credentials stay in stdin, not process arguments.
    completed = run_command(command, timeout_seconds=125, max_output_bytes=limit + 4096,
                            budget=budget, stdin_text=f'url = {json.dumps(url)}\n' + config,
                            stdout_callback=callback, capture_stdout=callback is None)
    status = re.search(r"__FLAYR_R2_STATUS__(\d{3})", completed.stderr)
    if completed.returncode or not status or not 200 <= int(status[1]) < 300:
        raise ValueError(f"R2 request failed (curl_exit={completed.returncode}, HTTP={status[1] if status else 'missing'}); private credentials and URLs withheld")
    return completed.stdout


def upload_video_url(data: bytes, config: R2VideoConfig, *, budget: Any = None) -> tuple[str, str]:
    """Upload and read back the exact MP4 bytes; never return an unchecked URL."""
    if not data or len(data) > 24 * 1024 * 1024:
        raise ValueError("R2 video is empty or exceeds the existing 24 MiB limit")
    verified = json.loads(_request(
        "https://api.cloudflare.com/client/v4/user/tokens/verify",
        f'header = "Authorization: Bearer {config.token}"\n', budget=budget))
    record = verified.get("result") or {}
    access = record.get("id", "")
    if not verified.get("success") or record.get("status") != "active" or not re.fullmatch(r"[a-f0-9]{32}", access):
        raise ValueError("R2 token verification failed")
    secret = hashlib.sha256(config.token.encode()).hexdigest()
    digest = hashlib.sha256(data).hexdigest()
    url = f"https://{config.account}.r2.cloudflarestorage.com/{config.bucket}/flayr-video/{digest}/video.mp4"
    if budget is not None:
        budget.reserve_upload(len(data))
    with tempfile.TemporaryDirectory(prefix=".flayr-r2.") as directory:
        path = Path(directory) / "video.mp4"
        path.write_bytes(data)
        _request(url, f'aws-sigv4 = "aws:amz:auto:s3"\nuser = "{access}:{secret}"\n'
                 f'upload-file = {json.dumps(str(path))}\nheader = "Content-Type: video/mp4"\n', budget=budget)
    uri = _presigned_get(url, access, secret, datetime.now(timezone.utc))
    hasher = hashlib.sha256()
    downloaded = 0

    def consume(chunk: bytes) -> None:
        nonlocal downloaded
        downloaded += len(chunk)
        if downloaded > len(data):
            raise ValueError("R2 video exceeds expected source size")
        hasher.update(chunk)
        if budget is not None:
            budget.reserve_download(len(chunk))

    _request(uri, "", budget=budget, callback=consume, limit=len(data) + 1)
    if downloaded != len(data) or hasher.hexdigest() != digest:
        raise ValueError("R2 downloaded video identity mismatch")
    return uri, digest
