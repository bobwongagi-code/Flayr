#!/usr/bin/env python3
"""Small local web service for the Flayr upload, jobs, and report flow."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.cookies import CookieError, SimpleCookie
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.flayr_core.market import normalize_target_market
from scripts.flayr_core.run_manifest import SUCCESS_MANIFEST_NAME, validate_success_manifest
from scripts.flayr_core.run_state import (
    ANALYSIS_COMPLETED,
    COMPLETED,
    DEGRADED,
    FAILED,
    REPORT_GENERATING,
    RunStateError,
    read_run_state,
    recover_run_state,
)
from scripts.flayr_core.utils import process_group_popen_kwargs, stop_process_group, write_json


WEB_ROOT = ROOT / "runs" / "_web"
JOBS_ROOT = WEB_ROOT / "jobs"
JOBS_FILE = WEB_ROOT / "jobs.json"
JOB_METADATA_FILE = "job.json"
FRONTEND_INDEX = ROOT / "frontend" / "index.html"
FRONTEND_ROOT = ROOT / "frontend"
DEFAULT_PORT = 8787
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_FIELD_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_VIDEO_BYTES * 2 + 8 * 1024 * 1024
MAX_SERVED_ASSET_BYTES = MAX_VIDEO_BYTES
UPLOAD_CHUNK_BYTES = 64 * 1024
ASSET_STREAM_CHUNK_BYTES = 64 * 1024
UPLOAD_STAGING_TTL_SECONDS = 60 * 60
JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_WEB_STORAGE_BYTES = 20 * 1024 * 1024 * 1024
MAX_WORKSPACE_STORAGE_BYTES = 10 * 1024 * 1024 * 1024
MIN_FREE_SPACE_BYTES = 512 * 1024 * 1024
REQUEST_HEADER_TIMEOUT_SECONDS = 15.0
UPLOAD_IDLE_TIMEOUT_SECONDS = 30.0
UPLOAD_TOTAL_TIMEOUT_SECONDS = 30 * 60
BOUNDARY_BYTES_LIMIT = 200
DEFAULT_WORKSPACE_ID = "local"
DEFAULT_OWNER_ID = "local"
CLIENT_COOKIE_NAME = "flayr_client_id"
CLIENT_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
CLIENT_COOKIE_SECRET_FILE = ".client_cookie_secret"
CLIENT_COOKIE_SECRET_ENV = "FLAYR_COOKIE_SECRET"
CLIENT_COOKIE_SECRET_BYTES = 32
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WEB_AUTH_TOKEN_ENV = "FLAYR_WEB_AUTH_TOKEN"
WEB_ALLOWED_HOSTS_ENV = "FLAYR_WEB_ALLOWED_HOSTS"
WEB_AUTH_TOKEN_MIN_BYTES = 32
PUBLIC_SUBMISSIONS_PER_MINUTE = 3
PUBLIC_MAX_ACTIVE_JOBS = 4
PUBLIC_MAX_ACTIVE_JOBS_PER_OWNER = 2
PUBLIC_MAX_DAILY_JOBS_PER_OWNER = 20

MARKET_CODES = {
    "马来西亚": "my",
    "泰国": "th",
    "印尼": "id",
    "其他东南亚市场": "sea",
    "未指定市场": "auto",
}


class RequestError(ValueError):
    """A client-side request error that should be returned as JSON."""


class AdmissionError(RequestError):
    """Raised when public-mode queue or daily capacity is exhausted."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync after a durable atomic rename."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write a small index/metadata file durably before publishing it."""
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
        _fsync_directory(path.parent)
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def relative_time(value: str) -> str:
    try:
        seconds = max(0, int((dt.datetime.now(dt.timezone.utc) - parse_iso(value)).total_seconds()))
    except (TypeError, ValueError):
        return "刚刚"
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def market_code(value: str) -> str:
    raw = str(value or "").strip()
    return normalize_target_market(MARKET_CODES.get(raw, raw or "auto"))


def _identity_value(value: Any, label: str, default: str) -> str:
    candidate = str(value or default).strip()
    if not IDENTITY_PATTERN.fullmatch(candidate):
        raise RequestError(f"{label} 无效")
    return candidate


def _is_loopback_host(host: str) -> bool:
    candidate = str(host or "").strip().strip("[]").lower()
    if candidate in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _normalize_host(value: str) -> tuple[str, int | None] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.count(":") > 1 and not raw.startswith("["):
        raw = f"[{raw}]"
    try:
        parsed = urlsplit(f"//{raw}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return hostname.rstrip(".").lower(), port


def _parse_allowed_hosts(raw: str) -> set[tuple[str, int | None]]:
    hosts: set[tuple[str, int | None]] = set()
    for value in str(raw or "").split(","):
        normalized = _normalize_host(value)
        if normalized:
            hosts.add(normalized)
    return hosts


def _host_matches(value: str, allowed_hosts: set[tuple[str, int | None]]) -> bool:
    normalized = _normalize_host(value)
    if normalized is None:
        return False
    hostname, port = normalized
    return any(
        allowed_host == hostname and (allowed_port is None or allowed_port == port)
        for allowed_host, allowed_port in allowed_hosts
    )


def _origin_matches(value: str, allowed_hosts: set[tuple[str, int | None]]) -> bool:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return _host_matches(parsed.netloc, allowed_hosts)


class SubmissionRateLimiter:
    """Small in-memory fixed-window limiter for one local server process."""

    def __init__(self, *, max_keys: int = 4096) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}
        self._max_keys = max(1, int(max_keys))

    def admit(self, keys: tuple[str, ...], *, limit: int, window_seconds: float) -> bool:
        if limit <= 0:
            return False
        now = time.monotonic()
        cutoff = now - max(1.0, float(window_seconds))
        with self._lock:
            for key, queue in list(self._events.items()):
                while queue and queue[0] <= cutoff:
                    queue.popleft()
                if not queue:
                    self._events.pop(key, None)
            queues: list[deque[float]] = []
            created_keys: list[str] = []
            for key in dict.fromkeys(keys):
                normalized_key = str(key)
                queue = self._events.get(normalized_key)
                if queue is None:
                    if len(self._events) >= self._max_keys:
                        for created_key in created_keys:
                            self._events.pop(created_key, None)
                        return False
                    queue = deque()
                    self._events[normalized_key] = queue
                    created_keys.append(normalized_key)
                while queue and queue[0] <= cutoff:
                    queue.popleft()
                if len(queue) >= limit:
                    for created_key in created_keys:
                        self._events.pop(created_key, None)
                    return False
                queues.append(queue)
            for queue in queues:
                queue.append(now)
            return True


def _is_wildcard_host(host: str) -> bool:
    return str(host or "").strip().strip("[]").lower() in {"0.0.0.0", "::"}


def _resolve_web_security(
    host: str,
    unsafe_expose: bool,
) -> tuple[bool, str, set[tuple[str, int | None]]]:
    """Resolve bind and operator-auth policy before opening a listening socket."""
    if not _is_loopback_host(host) and not unsafe_expose:
        raise ValueError("非回环监听必须显式使用 --unsafe-expose")
    if not unsafe_expose:
        return False, "", set()

    token = os.environ.get(WEB_AUTH_TOKEN_ENV, "").strip()
    if len(token.encode("utf-8")) < WEB_AUTH_TOKEN_MIN_BYTES:
        raise ValueError(f"{WEB_AUTH_TOKEN_ENV} 至少需要 {WEB_AUTH_TOKEN_MIN_BYTES} 个字节")

    raw_allowed_hosts = os.environ.get(WEB_ALLOWED_HOSTS_ENV, "").strip()
    allowed_hosts = _parse_allowed_hosts(raw_allowed_hosts)
    if raw_allowed_hosts and not allowed_hosts:
        raise ValueError(f"{WEB_ALLOWED_HOSTS_ENV} 没有可用的主机名")
    if not allowed_hosts and not _is_wildcard_host(host):
        normalized = _normalize_host(host)
        if normalized:
            allowed_hosts.add(normalized)
    if not allowed_hosts:
        raise ValueError(f"对外监听必须设置 {WEB_ALLOWED_HOSTS_ENV}")
    return True, token, allowed_hosts


def _load_client_cookie_secret(root: Path) -> bytes:
    """Load a stable signing key without exposing it through the web root."""
    configured = os.environ.get(CLIENT_COOKIE_SECRET_ENV, "").strip()
    if configured:
        if len(configured) < CLIENT_COOKIE_SECRET_BYTES:
            raise RuntimeError(f"{CLIENT_COOKIE_SECRET_ENV} 至少需要 {CLIENT_COOKIE_SECRET_BYTES} 个字符")
        return configured.encode("utf-8")

    secret_path = root / CLIENT_COOKIE_SECRET_FILE
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secret = secret_path.read_bytes()
    except FileNotFoundError:
        secret = secrets.token_bytes(CLIENT_COOKIE_SECRET_BYTES)
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            secret = secret_path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as sink:
                sink.write(secret)
    except OSError as exc:
        raise RuntimeError(f"无法读取浏览器身份签名密钥：{secret_path}") from exc
    if len(secret) < CLIENT_COOKIE_SECRET_BYTES:
        raise RuntimeError(f"浏览器身份签名密钥无效：{secret_path}")
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass
    return secret


def _signed_client_cookie(client_id: str, secret: bytes) -> str:
    signature = hmac.new(secret, client_id.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{client_id}.{signature}"


def _verified_client_cookie(value: str, secret: bytes) -> str | None:
    try:
        client_id, signature = value.rsplit(".", 1)
    except ValueError:
        return None
    if not IDENTITY_PATTERN.fullmatch(client_id) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return None
    expected = _signed_client_cookie(client_id, secret).rsplit(".", 1)[1]
    return client_id if hmac.compare_digest(signature, expected) else None


def _run_state(run_dir: Path) -> str:
    payload = read_run_state(run_dir)
    return str(payload.get("state") or "") if payload else ""


def _analysis_state(run_dir: Path) -> str:
    try:
        payload = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("analysis_run_state") or "")


def progress_for_run(run_dir: Path) -> tuple[int, str]:
    """Expose only the three coarse phases promised by the updated design."""
    lifecycle_state = _run_state(run_dir)
    if lifecycle_state == DEGRADED:
        return 100, "报告生成（部分分析能力降级）"
    if lifecycle_state == COMPLETED:
        return 100, "报告生成"
    if lifecycle_state in {ANALYSIS_COMPLETED, REPORT_GENERATING}:
        return 92, "报告生成"
    state = _analysis_state(run_dir)
    if state == "completed" and (run_dir / SUCCESS_MANIFEST_NAME).is_file():
        return 100, "报告生成"
    if state == "degraded" and (run_dir / "degraded_manifest.json").is_file() and report_variants_ready(run_dir):
        return 100, "报告生成（部分分析能力降级）"
    if (run_dir / "postprocess_change_log.json").is_file():
        return 92, "报告生成"
    if (run_dir / "final_derived_result.json").is_file() or (run_dir / "validated_normalized_result.json").is_file():
        return 84, "报告生成"
    if (run_dir / "raw_model_response.json").is_file():
        return 72, "模型对比分析"
    if any(run_dir.glob("video_facts_*.json")):
        return 58, "模型对比分析"
    if any(run_dir.glob("*/transcript*")) or any(run_dir.glob("*/zh*")):
        return 32, "素材处理与转写"
    if any(run_dir.glob("*/frames")) or any(run_dir.glob("*/preprocess_manifest.json")):
        return 18, "素材处理与转写"
    return 0, "素材处理与转写"


def estimated_remaining_seconds(progress: int) -> int:
    """Return a deliberately coarse phase bucket, not a second-precise ETA."""
    progress = max(0, min(int(progress), 100))
    if progress >= 100:
        return 0
    for threshold, remaining in (
        (92, 2 * 60),
        (84, 5 * 60),
        (72, 10 * 60),
        (58, 15 * 60),
        (32, 20 * 60),
        (18, 25 * 60),
    ):
        if progress >= threshold:
            return remaining
    return 30 * 60


def safe_asset_path(run_dir: Path, relative_path: str) -> Path | None:
    """Resolve a run-relative asset without permitting traversal or symlink escape."""
    root = run_dir.expanduser().resolve()
    requested = Path(unquote(relative_path))
    if requested.is_absolute() or any(part in {"", ".", ".."} for part in requested.parts):
        return None
    candidate = (root / requested).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _asset_magic_matches(path: Path) -> bool:
    """Reject known media/report extensions whose bytes do not match them."""
    suffix = path.suffix.lower()
    if suffix not in {".html", ".htm", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".m4v", ".webm", ".wav", ".mp3"}:
        return True
    try:
        with path.open("rb") as source:
            prefix = source.read(64)
    except OSError:
        return False
    if suffix in {".html", ".htm"}:
        start = prefix.lstrip().lower()
        return start.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
    if suffix == ".json":
        return prefix.lstrip().startswith((b"{", b"["))
    if suffix == ".png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return prefix.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
    if suffix in {".mp4", ".m4v"}:
        return b"ftyp" in prefix[:32]
    if suffix == ".webm":
        return prefix.startswith(b"\x1a\x45\xdf\xa3")
    if suffix == ".wav":
        return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE"
    if suffix == ".mp3":
        return prefix.startswith(b"ID3") or (len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0)
    return True


def _servable_asset(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_SERVED_ASSET_BYTES:
            return False
    except OSError:
        return False
    return _asset_magic_matches(path)


def _directory_size(root: Path) -> int:
    """Count regular files without following symlinks outside the managed root."""
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _safe_servable_asset(run_dir: Path, relative_path: str) -> Path | None:
    candidate = safe_asset_path(run_dir, relative_path)
    return candidate if candidate is not None and _servable_asset(candidate) else None


def report_variants_ready(run_dir: Path) -> bool:
    """Return true only when both audience reports are safely available."""
    return all(
        _safe_servable_asset(run_dir, name) is not None
        for name in ("bd_report.html", "creator_report.html")
    )


def _content_disposition(value: str) -> tuple[str, str | None]:
    match_name = re.search(r"(?:^|;)\s*name\s*=\s*(?:\"([^\"]*)\"|([^;]*))", value, re.I)
    if not match_name:
        raise RequestError("multipart 字段缺少 name")
    name = (match_name.group(1) or match_name.group(2) or "").strip()
    match_file = re.search(r"(?:^|;)\s*filename\s*=\s*(?:\"([^\"]*)\"|([^;]*))", value, re.I)
    filename = None if not match_file else (match_file.group(1) or match_file.group(2) or "").strip()
    return name, filename


def _write_limited(sink: Any, data: bytes, total: int, limit: int) -> int:
    next_total = total + len(data)
    if next_total > limit:
        raise RequestError("上传文件超过大小限制")
    sink.write(data)
    return next_total


def _copy_part_until_boundary(source: Any, boundary: bytes, sink: Any, limit: int) -> int:
    marker = b"\r\n--" + boundary
    keep = len(marker)
    buffer = bytearray()
    total = 0
    while True:
        chunk = source.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            raise RequestError("multipart 请求缺少结束边界")
        buffer.extend(chunk)
        index = buffer.find(marker)
        if index >= 0:
            total = _write_limited(sink, bytes(buffer[:index]), total, limit)
            current_position = source.tell()
            boundary_position = current_position - len(buffer) + index + len(marker)
            source.seek(boundary_position)
            return total
        if len(buffer) > keep:
            flush_length = len(buffer) - keep
            total = _write_limited(sink, bytes(buffer[:flush_length]), total, limit)
            del buffer[:flush_length]


def parse_multipart(
    body_path: Path,
    content_type: str,
    *,
    staging_root: Path | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    header = Message()
    header["content-type"] = content_type
    boundary_value = header.get_param("boundary", header="content-type")
    if not boundary_value:
        raise RequestError("multipart 请求缺少 boundary")
    boundary = str(boundary_value).encode("utf-8")
    if not boundary or len(boundary) > BOUNDARY_BYTES_LIMIT or b"\r" in boundary or b"\n" in boundary:
        raise RequestError("multipart boundary 无效")

    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    seen_names: set[str] = set()
    temp_paths: list[Path] = []
    staging_root = (staging_root or WEB_ROOT).resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    try:
        with body_path.open("rb") as source:
            first = source.readline(BOUNDARY_BYTES_LIMIT + 8)
            expected = b"--" + boundary + b"\r\n"
            if first != expected:
                raise RequestError("multipart 起始边界无效")
            while True:
                raw_headers = bytearray()
                while True:
                    line = source.readline(64 * 1024 + 1)
                    if not line:
                        raise RequestError("multipart 字段头不完整")
                    if len(line) > 64 * 1024:
                        raise RequestError("multipart 字段头过大")
                    if line == b"\r\n":
                        break
                    raw_headers.extend(line)
                header_message = Message()
                for raw_line in raw_headers.splitlines():
                    if b":" not in raw_line:
                        raise RequestError("multipart 字段头无效")
                    key, value = raw_line.split(b":", 1)
                    header_message[key.decode("ascii", "ignore").lower()] = value.decode("utf-8", "replace").strip()
                disposition = header_message.get("content-disposition")
                if not disposition:
                    raise RequestError("multipart 字段缺少 Content-Disposition")
                name, filename = _content_disposition(disposition)
                if not name:
                    raise RequestError("multipart 字段名为空")
                if name in seen_names:
                    raise RequestError(f"multipart 字段重复：{name}")
                seen_names.add(name)
                if filename is not None:
                    safe_name = Path(filename).name or "upload.bin"
                    temp = tempfile.NamedTemporaryFile(prefix=".upload-part-", dir=staging_root, delete=False)
                    temp_path = Path(temp.name)
                    temp_paths.append(temp_path)
                    with temp:
                        _copy_part_until_boundary(source, boundary, temp, MAX_VIDEO_BYTES)
                    files[name] = {"path": temp_path, "filename": safe_name}
                else:
                    sink = BytesIO()
                    _copy_part_until_boundary(source, boundary, sink, MAX_FIELD_BYTES)
                    fields[name] = sink.getvalue().decode("utf-8", "replace").strip()
                suffix = source.read(2)
                if suffix == b"--":
                    trailer = source.read()
                    if trailer not in {b"", b"\r\n"}:
                        raise RequestError("multipart 结束边界后存在异常数据")
                    break
                if suffix != b"\r\n":
                    raise RequestError("multipart 边界结束符无效")
        return fields, files
    except Exception:
        for path in temp_paths:
            path.unlink(missing_ok=True)
        raise


def cleanup_upload_files(files: dict[str, dict[str, Any]]) -> None:
    """Remove parsed upload files that were not adopted into a job."""
    for item in files.values():
        raw_path = item.get("path")
        if raw_path:
            Path(str(raw_path)).unlink(missing_ok=True)


class JobStore:
    def __init__(self, root: Path = WEB_ROOT, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
        self.root = root
        self.jobs_root = root / "jobs"
        self.state_path = root / "jobs.json"
        self.state_backup_path = root / "jobs.json.bak"
        self.workspace_id = _identity_value(workspace_id, "workspace_id", DEFAULT_WORKSPACE_ID)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flayr-job")
        self._running_processes: dict[str, subprocess.Popen] = {}
        self._shutdown_requested = False
        self._admission_reservations: dict[tuple[str, str], int] = {}
        self._storage_reservations: dict[str, int] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.recovery_warning = ""
        self._index_write_blocked = False
        self._preserve_existing_backup = False
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_upload_staging()
        self._load()
        self._recover_incomplete()
        self._garbage_collect()
        self._preserve_existing_backup = False

    def _cleanup_upload_staging(self) -> None:
        cutoff = time.time() - UPLOAD_STAGING_TTL_SECONDS
        try:
            candidates = list(self.root.iterdir())
        except OSError:
            return
        for path in candidates:
            if not path.name.startswith(".upload-"):
                continue
            try:
                if path.stat().st_mtime <= cutoff:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
            except OSError:
                continue

    def _job_root(self, job_id: str) -> Path | None:
        if not IDENTITY_PATTERN.fullmatch(str(job_id)):
            return None
        jobs_root = self.jobs_root.resolve()
        candidate = (self.jobs_root / str(job_id)).resolve()
        try:
            candidate.relative_to(jobs_root)
        except ValueError:
            return None
        return candidate

    def _garbage_collect(self) -> None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=JOB_RETENTION_SECONDS)
        expired: list[str] = []
        with self._lock:
            for job_id, job in self.jobs.items():
                if job.get("status") not in {"completed", "degraded", "failed"}:
                    continue
                try:
                    created_at = parse_iso(str(job.get("created_at") or ""))
                except (TypeError, ValueError):
                    continue
                if created_at <= cutoff:
                    expired.append(job_id)
            for job_id in expired:
                self.jobs.pop(job_id, None)
            if expired:
                self._persist_locked()
        for job_id in expired:
            job_root = self._job_root(job_id)
            if job_root:
                shutil.rmtree(job_root, ignore_errors=True)

    @staticmethod
    def _coerce_jobs_payload(payload: Any) -> dict[str, dict[str, Any]] | None:
        if not isinstance(payload, dict):
            return None
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _quarantine_corrupt_index(self) -> str | None:
        if not self.state_path.exists():
            return None
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = self.state_path.with_name(
            f"{self.state_path.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}"
        )
        try:
            os.replace(self.state_path, quarantine)
            _fsync_directory(self.root)
        except OSError as exc:
            self._index_write_blocked = True
            self.recovery_warning = f"任务索引损坏且无法隔离：{exc}。已进入只读恢复模式。"
            sys.stderr.write(f"[flayr-web] recovery warning: {self.recovery_warning}\n")
            return None
        return quarantine.name

    def _read_job_metadata(self, job_root: Path, fallback: dict[str, Any]) -> dict[str, Any]:
        metadata = self._read_json_object(job_root / JOB_METADATA_FILE)
        merged = dict(fallback)
        if metadata:
            merged.update(metadata)
        return merged

    @staticmethod
    def _input_path(job_root: Path, role: str) -> Path | None:
        input_root = job_root / "inputs"
        try:
            candidates = sorted(
                path for path in input_root.iterdir()
                if path.is_file() and path.name.startswith(f"{role}-")
            )
        except OSError:
            return None
        return candidates[0].resolve() if candidates else None

    def _rebuild_job_from_directory(
        self,
        job_root: Path,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = job_root.name
        job = self._read_job_metadata(job_root, fallback)
        run_dir = (job_root / "run").resolve()
        job["id"] = job_id
        job["run_dir"] = str(run_dir)
        job["log_path"] = str((job_root / "worker.log").resolve())
        for role in ("benchmark_video", "creator_video"):
            path = self._input_path(job_root, role)
            if path:
                job[f"{role}_path"] = str(path)
        job.setdefault("workspace_id", self.workspace_id)
        job.setdefault("visibility", "private")
        job.setdefault("product_name", "未命名分析")
        job.setdefault("market", "未指定市场")
        job.setdefault("failure_reason", "")
        job.setdefault("degraded_reason", "")
        if not job.get("created_at"):
            try:
                created = dt.datetime.fromtimestamp(job_root.stat().st_mtime, dt.timezone.utc)
                job["created_at"] = created.replace(microsecond=0).isoformat()
            except OSError:
                job["created_at"] = utc_now()

        expected_inputs = {
            role: Path(str(job[f"{role}_path"]))
            for role in ("benchmark_video", "creator_video")
            if job.get(f"{role}_path")
        }
        lifecycle = _run_state(run_dir)
        complete = run_dir.is_dir() and validate_success_manifest(run_dir, expected_inputs)
        degraded = run_dir.is_dir() and (
            (run_dir / "degraded_manifest.json").is_file() and report_variants_ready(run_dir)
        )
        if complete:
            job.update({
                "status": "completed",
                "progress": 100,
                "phase": "报告生成",
                "estimated_remaining_seconds": 0,
            })
        elif degraded and lifecycle != COMPLETED:
            job.update({
                "status": "degraded",
                "progress": 100,
                "phase": "报告生成（部分分析能力降级）",
                "estimated_remaining_seconds": 0,
            })
        elif lifecycle == FAILED or not run_dir.is_dir():
            job.update({
                "status": "failed",
                "progress": 0,
                "phase": "素材处理与转写",
                "estimated_remaining_seconds": 0,
                "failure_reason": job.get("failure_reason") or "任务索引恢复后未发现完整产物。",
            })
        else:
            job["status"] = "running"
            job["progress"], job["phase"] = progress_for_run(run_dir)
            job["estimated_remaining_seconds"] = estimated_remaining_seconds(job["progress"])
        return job

    def _rebuild_index_from_jobs(self, fallback: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        rebuilt: dict[str, dict[str, Any]] = {}
        try:
            candidates = sorted(self.jobs_root.iterdir(), key=lambda path: path.name)
        except OSError:
            candidates = []
        for job_root in candidates:
            if not job_root.is_dir() or not IDENTITY_PATTERN.fullmatch(job_root.name):
                continue
            rebuilt[job_root.name] = self._rebuild_job_from_directory(
                job_root,
                fallback.get(job_root.name, {}),
            )
        if rebuilt:
            return rebuilt
        return dict(fallback)

    def _load(self) -> None:
        payload = self._read_json_object(self.state_path)
        recovery_required = self.state_path.exists() and payload is None
        fallback = self._read_json_object(self.state_backup_path) or {}
        if recovery_required:
            quarantined = self._quarantine_corrupt_index()
            self._preserve_existing_backup = self.state_backup_path.is_file()
            self.jobs = self._rebuild_index_from_jobs(fallback)
            source = f"从 {len(self.jobs)} 个任务目录/备份记录重建"
            if quarantined:
                source += f"，原文件已隔离为 {quarantined}"
            self.recovery_warning = f"任务索引损坏，已{source}。请核对任务列表中的恢复结果。"
            sys.stderr.write(f"[flayr-web] recovery warning: {self.recovery_warning}\n")
        else:
            self.jobs = self._coerce_jobs_payload(payload or {}) or {}
            if not self.state_path.exists() and self.jobs_root.exists():
                rebuilt = self._rebuild_index_from_jobs(fallback)
                if rebuilt:
                    self.jobs = rebuilt
                    self.recovery_warning = f"任务索引缺失，已从 {len(self.jobs)} 个任务目录/备份记录重建。"
                    sys.stderr.write(f"[flayr-web] recovery warning: {self.recovery_warning}\n")
        changed = False
        for job in self.jobs.values():
            if not job.get("workspace_id"):
                job["workspace_id"] = self.workspace_id
                changed = True
            if not job.get("visibility"):
                job["visibility"] = "private"
                changed = True
        if (
            not self._index_write_blocked
            and (changed or recovery_required or self.recovery_warning)
        ):
            self._persist_locked()

    def _persist_locked(self) -> None:
        if self._index_write_blocked:
            raise OSError("任务索引处于只读恢复模式，无法安全覆盖原文件")
        payload = (json.dumps(self.jobs, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if self.state_path.is_file() and not self._preserve_existing_backup:
            _atomic_write_bytes(self.state_backup_path, self.state_path.read_bytes())
        _atomic_write_bytes(self.state_path, payload)

    def _write_job_metadata(self, job_root: Path, job: dict[str, Any]) -> None:
        write_json(job_root / JOB_METADATA_FILE, job)
        _fsync_directory(job_root)

    def _recover_incomplete(self) -> None:
        with self._lock:
            changed = False
            for job in self.jobs.values():
                if job.get("status") not in {"queued", "running"}:
                    continue
                run_dir = Path(str(job.get("run_dir") or ""))
                expected = {
                    "benchmark_video": Path(str(job.get("benchmark_path") or "")),
                    "creator_video": Path(str(job.get("creator_path") or "")),
                }
                lifecycle = _run_state(run_dir)
                complete = run_dir.is_dir() and validate_success_manifest(run_dir, expected)
                degraded = run_dir.is_dir() and (run_dir / "degraded_manifest.json").is_file() and report_variants_ready(run_dir)
                if lifecycle == COMPLETED:
                    degraded = False
                elif lifecycle == DEGRADED:
                    complete = False
                elif lifecycle == FAILED:
                    complete = False
                    degraded = False
                if complete:
                    recover_run_state(
                        run_dir,
                        COMPLETED,
                        job_id=str(job.get("id") or ""),
                        reason="服务重启后校验成功清单并恢复完成状态。",
                        artifacts=(SUCCESS_MANIFEST_NAME, "bd_report.html", "creator_report.html"),
                    )
                    job.update({
                        "status": "completed",
                        "progress": 100,
                        "phase": "报告生成",
                        "estimated_remaining_seconds": 0,
                    })
                elif degraded:
                    recover_run_state(
                        run_dir,
                        DEGRADED,
                        job_id=str(job.get("id") or ""),
                        reason="服务重启后恢复降级报告。",
                        artifacts=("degraded_manifest.json", "bd_report.html", "creator_report.html"),
                    )
                    job.update({
                        "status": "degraded",
                        "progress": 100,
                        "phase": "报告生成（部分分析能力降级）",
                        "estimated_remaining_seconds": 0,
                    })
                else:
                    reason = "服务重新启动时任务尚未完成，请重新上传后重试。"
                    if run_dir.is_dir():
                        try:
                            recover_run_state(
                                run_dir,
                                FAILED,
                                job_id=str(job.get("id") or ""),
                                reason=reason,
                            )
                        except RunStateError:
                            pass
                    job.update({
                        "status": "failed",
                        "estimated_remaining_seconds": 0,
                        "failure_reason": reason,
                    })
                changed = True
            if changed:
                self._persist_locked()

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.update(changes)
            self._persist_locked()

    def _admission_error_locked(self, owner_id: str, workspace_id: str) -> str | None:
        reservation_total = sum(self._admission_reservations.values())
        active = [
            job
            for job in self.jobs.values()
            if str(job.get("workspace_id") or self.workspace_id) == workspace_id
            and job.get("status") in {"queued", "running"}
        ]
        if len(active) + reservation_total >= PUBLIC_MAX_ACTIVE_JOBS:
            return "当前任务队列已满，请稍后重试。"
        owner_reservations = self._admission_reservations.get((workspace_id, owner_id), 0)
        owner_active = [job for job in active if str(job.get("owner_id") or "") == owner_id]
        if len(owner_active) + owner_reservations >= PUBLIC_MAX_ACTIVE_JOBS_PER_OWNER:
            return "当前浏览器已有任务在处理中，请等待完成后再提交。"
        today = utc_now()[:10]
        owner_today = sum(
            1
            for job in self.jobs.values()
            if str(job.get("workspace_id") or self.workspace_id) == workspace_id
            and str(job.get("owner_id") or "") == owner_id
            and str(job.get("created_at") or "").startswith(today)
        )
        if owner_today + owner_reservations >= PUBLIC_MAX_DAILY_JOBS_PER_OWNER:
            return "今日任务额度已用完，请明天再试。"
        return None

    def _reserve_admission(self, owner_id: str, workspace_id: str) -> None:
        with self._lock:
            admission_error = self._admission_error_locked(owner_id, workspace_id)
            if admission_error:
                raise AdmissionError(admission_error)
            key = (workspace_id, owner_id)
            self._admission_reservations[key] = self._admission_reservations.get(key, 0) + 1

    def _release_admission(self, owner_id: str, workspace_id: str) -> None:
        with self._lock:
            key = (workspace_id, owner_id)
            remaining = self._admission_reservations.get(key, 0) - 1
            if remaining > 0:
                self._admission_reservations[key] = remaining
            else:
                self._admission_reservations.pop(key, None)

    def admission_error(self, *, owner_id: str, workspace_id: str) -> str | None:
        with self._lock:
            return self._admission_error_locked(owner_id, workspace_id)

    def _storage_error_locked(self, workspace_id: str, required_bytes: int) -> str | None:
        current_bytes = _directory_size(self.root)
        reserved_bytes = sum(self._storage_reservations.values())
        if current_bytes + reserved_bytes + required_bytes > MAX_WEB_STORAGE_BYTES:
            return "服务存储空间已达到上限，请先清理历史任务。"
        # A JobStore owns one workspace root.  Keep this check separate so a
        # future multi-workspace store cannot silently skip its workspace cap.
        workspace_reserved = self._storage_reservations.get(workspace_id, 0)
        if current_bytes + workspace_reserved + required_bytes > MAX_WORKSPACE_STORAGE_BYTES:
            return "当前工作区存储空间已达到上限，请先清理历史任务。"
        try:
            free_bytes = shutil.disk_usage(self.root).free
        except OSError:
            return "无法确认可用磁盘空间，暂时不能接收上传。"
        if free_bytes < required_bytes + MIN_FREE_SPACE_BYTES:
            return "可用磁盘空间不足，请先清理后重试。"
        return None

    def reserve_upload(self, workspace_id: str, required_bytes: int) -> None:
        workspace_id = _identity_value(workspace_id, "workspace_id", self.workspace_id)
        required_bytes = max(0, int(required_bytes))
        with self._lock:
            storage_error = self._storage_error_locked(workspace_id, required_bytes)
            if storage_error:
                raise AdmissionError(storage_error)
            self._storage_reservations[workspace_id] = (
                self._storage_reservations.get(workspace_id, 0) + required_bytes
            )

    def release_upload(self, workspace_id: str, required_bytes: int) -> None:
        workspace_id = _identity_value(workspace_id, "workspace_id", self.workspace_id)
        required_bytes = max(0, int(required_bytes))
        with self._lock:
            remaining = self._storage_reservations.get(workspace_id, 0) - required_bytes
            if remaining > 0:
                self._storage_reservations[workspace_id] = remaining
            else:
                self._storage_reservations.pop(workspace_id, None)

    def delete(
        self,
        job_id: str,
        *,
        owner_id: str,
        workspace_id: str,
    ) -> bool:
        owner_id = _identity_value(owner_id, "owner_id", DEFAULT_OWNER_ID)
        workspace_id = _identity_value(workspace_id, "workspace_id", self.workspace_id)
        process: subprocess.Popen | None = None
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or not self._matches_scope(job, owner_id, workspace_id):
                return False
            process = self._running_processes.get(job_id)
            self.jobs.pop(job_id, None)
            self._persist_locked()
        if process is not None:
            stop_process_group(process, grace_seconds=5.0)
        job_root = self._job_root(job_id)
        if job_root:
            shutil.rmtree(job_root, ignore_errors=True)
        return True

    def create(
        self,
        fields: dict[str, str],
        files: dict[str, dict[str, Any]],
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        workspace_id: str | None = None,
        enforce_limits: bool = False,
    ) -> dict[str, Any]:
        if "benchmark_video" not in files or "creator_video" not in files:
            for item in files.values():
                Path(str(item.get("path"))).unlink(missing_ok=True)
            raise RequestError("请同时上传标杆视频和达人视频")
        product_name = str(fields.get("product_name") or "未命名分析").strip()[:200]
        market_label = str(fields.get("market") or "未指定市场").strip()[:80]
        try:
            normalized_market = market_code(market_label)
        except ValueError as exc:
            cleanup_upload_files(files)
            raise RequestError(str(exc)) from exc
        try:
            owner_id = _identity_value(owner_id, "owner_id", DEFAULT_OWNER_ID)
            workspace_id = _identity_value(workspace_id, "workspace_id", self.workspace_id)
        except RequestError:
            cleanup_upload_files(files)
            raise
        if enforce_limits:
            try:
                self._reserve_admission(owner_id, workspace_id)
            except AdmissionError:
                cleanup_upload_files(files)
                raise
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job_root = self.jobs_root / job_id
        input_root = job_root / "inputs"
        run_dir = job_root / "run"
        try:
            try:
                input_root.mkdir(parents=True, exist_ok=False)
                moved: dict[str, Path] = {}
                for role in ("benchmark_video", "creator_video"):
                    item = files[role]
                    original = Path(str(item["path"]))
                    filename = Path(str(item.get("filename") or f"{role}.mp4")).name or f"{role}.mp4"
                    destination = input_root / f"{role}-{filename}"
                    shutil.move(str(original), destination)
                    moved[role] = destination
            except Exception:
                for item in files.values():
                    Path(str(item.get("path"))).unlink(missing_ok=True)
                shutil.rmtree(job_root, ignore_errors=True)
                raise
            run_dir.mkdir(parents=True, exist_ok=False)
            now = utc_now()
            job = {
                "id": job_id,
                "owner_id": owner_id,
                "workspace_id": workspace_id,
                "visibility": "private",
                "product_name": product_name,
                "market": market_label,
                "market_code": normalized_market,
                "category": str(fields.get("category") or "").strip()[:200],
                "price": str(fields.get("price") or "").strip()[:100],
                "selling_point": str(fields.get("selling_point") or "").strip()[:1000],
                "status": "queued",
                "progress": 0,
                "phase": "素材处理与转写",
                "estimated_remaining_seconds": estimated_remaining_seconds(0),
                "created_at": now,
                "failure_reason": "",
                "degraded_reason": "",
                "benchmark_path": str(moved["benchmark_video"].resolve()),
                "creator_path": str(moved["creator_video"].resolve()),
                "run_dir": str(run_dir.resolve()),
                "log_path": str((job_root / "worker.log").resolve()),
            }
            with self._lock:
                self._write_job_metadata(job_root, job)
                self.jobs[job_id] = job
                self._persist_locked()
            self._executor.submit(self._run_job, job_id)
            return self.public(job)
        finally:
            cleanup_upload_files(files)
            if enforce_limits:
                self._release_admission(owner_id, workspace_id)

    @staticmethod
    def _matches_scope(job: dict[str, Any], owner_id: str | None, workspace_id: str | None) -> bool:
        if workspace_id is not None and str(job.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id:
            return False
        if owner_id is None:
            return True
        stored_owner = str(job.get("owner_id") or "")
        # Missing ownership metadata is not public.  Treating it as claimable
        # would let any browser that knows a legacy job id take it over.
        return bool(stored_owner) and stored_owner == owner_id

    def get(
        self,
        job_id: str,
        *,
        owner_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            if owner_id is not None or workspace_id is not None:
                owner = _identity_value(owner_id, "owner_id", DEFAULT_OWNER_ID) if owner_id is not None else None
                workspace = _identity_value(workspace_id, "workspace_id", self.workspace_id)
                if not self._matches_scope(job, owner, workspace):
                    return None
            self._refresh_progress_locked(job)
            return dict(job)

    def all(
        self,
        *,
        owner_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            for job in self.jobs.values():
                self._refresh_progress_locked(job)
            owner = _identity_value(owner_id, "owner_id", DEFAULT_OWNER_ID) if owner_id is not None else None
            workspace = _identity_value(workspace_id, "workspace_id", self.workspace_id) if workspace_id is not None else None
            jobs = sorted(
                (job for job in self.jobs.values() if self._matches_scope(job, owner, workspace)),
                key=lambda item: str(item.get("created_at") or ""),
                reverse=True,
            )
            return [dict(job) for job in jobs]

    def public(self, job: dict[str, Any]) -> dict[str, Any]:
        status = str(job.get("status") or "failed")
        run_dir = Path(str(job.get("run_dir") or ""))
        analysis_scope = self._read_analysis_scope(run_dir)
        reports_ready = status in {"completed", "degraded"}
        has_bd_report = _safe_servable_asset(run_dir, "bd_report.html") is not None
        has_legacy_report = _safe_servable_asset(run_dir, "report.html") is not None
        has_creator_report = _safe_servable_asset(run_dir, "creator_report.html") is not None
        has_report = has_bd_report or has_legacy_report
        workspace_id = str(job.get("workspace_id") or DEFAULT_WORKSPACE_ID)
        scoped_job_url = f"/api/workspaces/{workspace_id}/jobs/{job.get('id')}"
        report_url = (
            f"{scoped_job_url}/report"
            if reports_ready and has_report
            else ""
        )
        bd_report_url = (
            f"{scoped_job_url}/report"
            if reports_ready and has_bd_report
            else ""
        )
        creator_report_url = (
            f"{scoped_job_url}/creator-report"
            if reports_ready and has_creator_report
            else ""
        )
        return {
            "id": job.get("id"),
            "workspace_id": workspace_id,
            "job_url": scoped_job_url,
            "name": job.get("product_name") or "未命名分析",
            "market": job.get("market") or "未指定市场",
            "status": status,
            "submitted": relative_time(str(job.get("created_at") or "")),
            "submitted_at": job.get("created_at"),
            "progress": int(job.get("progress") or 0),
            "phase": job.get("phase") or "素材处理与转写",
            "estimated_remaining_seconds": int(job.get("estimated_remaining_seconds") or 0),
            "strategy_level": analysis_scope == "strategy",
            "degraded_reason": job.get("degraded_reason") or "",
            "failure_reason": job.get("failure_reason") or "",
            "report_url": report_url,
            "bd_report_url": bd_report_url,
            "creator_report_url": creator_report_url,
            "run_state": _run_state(run_dir),
            "report_kind": "audience" if (has_bd_report or has_creator_report) else ("legacy" if has_legacy_report else ""),
        }

    @staticmethod
    def _read_analysis_scope(run_dir: Path) -> str:
        try:
            payload = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        scope = payload.get("analysis_scope") if isinstance(payload, dict) else None
        return str(scope.get("level") or "") if isinstance(scope, dict) else ""

    def _refresh_progress_locked(self, job: dict[str, Any]) -> None:
        if job.get("status") not in {"queued", "running"}:
            return
        progress, phase = progress_for_run(Path(str(job.get("run_dir") or "")))
        job["progress"] = progress
        job["phase"] = phase
        job["estimated_remaining_seconds"] = estimated_remaining_seconds(progress)

    def _run_job(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        run_dir = Path(str(job["run_dir"]))
        log_path = Path(str(job["log_path"]))
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = self._command(job)
        self._update(job_id, status="running", phase="素材处理与转写")
        process: subprocess.Popen | None = None
        try:
            with log_path.open("ab") as log:
                log.write(("$ " + " ".join(command) + "\n").encode("utf-8", "replace"))
                process = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    **process_group_popen_kwargs(),
                )
                with self._lock:
                    self._running_processes[job_id] = process
                    stop_requested = self._shutdown_requested
                if stop_requested:
                    stop_process_group(process, grace_seconds=5.0)
                while process.poll() is None:
                    current = self.get(job_id)
                    if current:
                        progress, phase = progress_for_run(run_dir)
                        self._update(
                            job_id,
                            progress=progress,
                            phase=phase,
                            estimated_remaining_seconds=estimated_remaining_seconds(progress),
                        )
                    time.sleep(5)
                returncode = process.wait()
            self._finish(job_id, returncode)
        except Exception as exc:  # keep worker failures visible in the job list
            reason = f"任务启动或执行失败：{str(exc)[:240]}"
            try:
                recover_run_state(run_dir, FAILED, job_id=job_id, reason=reason)
            except RunStateError:
                pass
            self._update(
                job_id,
                status="failed",
                failure_reason=reason,
                phase="分析失败",
                estimated_remaining_seconds=0,
            )
        finally:
            if process is not None:
                with self._lock:
                    if self._running_processes.get(job_id) is process:
                        self._running_processes.pop(job_id, None)

    def _command(self, job: dict[str, Any]) -> list[str]:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "flayr.py"),
            "improve",
            "--benchmark-video",
            str(job["benchmark_path"]),
            "--creator-video",
            str(job["creator_path"]),
            "--product-name",
            str(job["product_name"]),
            "--product-category",
            str(job["category"]),
            "--product-price",
            str(job["price"] or "未填写"),
            "--target-market",
            str(job["market_code"]),
            "--core-selling-points",
            str(job["selling_point"]),
            "--output-dir",
            str(job["run_dir"]),
            "--verification-stage",
            "production",
        ]
        judgment_model = os.environ.get("FLAYR_JUDGMENT_MODEL", "").strip()
        vision_model = os.environ.get("FLAYR_VISION_MODEL", "").strip()
        legacy_model = os.environ.get("FLAYR_LLM_MODEL", "").strip()
        if judgment_model or vision_model:
            if not judgment_model or not vision_model:
                raise RuntimeError(
                    "FLAYR_JUDGMENT_MODEL and FLAYR_VISION_MODEL must be configured together"
                )
            command.extend(
                [
                    "--judgment-model",
                    judgment_model,
                    "--vision-model",
                    vision_model,
                    "--llm-api-url",
                    os.environ.get("FLAYR_LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
                    "--llm-api-key-env",
                    os.environ.get("FLAYR_LLM_API_KEY_ENV", "OPENAI_API_KEY"),
                ]
            )
        elif legacy_model:
            command.extend(
                [
                    "--llm-model",
                    legacy_model,
                    "--llm-api-url",
                    os.environ.get("FLAYR_LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
                    "--llm-api-key-env",
                    os.environ.get("FLAYR_LLM_API_KEY_ENV", "OPENAI_API_KEY"),
                ]
            )
        if os.environ.get("FLAYR_ALLOW_DEGRADED", "").strip().lower() in {"1", "true", "yes"}:
            command.append("--allow-degraded")
        asr_url = os.environ.get("FLAYR_ASR_API_URL", "").strip()
        if asr_url:
            command.extend(["--asr-api-url", asr_url])
        asr_model = os.environ.get("FLAYR_ASR_MODEL", "").strip()
        if asr_model:
            command.extend(["--asr-model", asr_model])
        asr_key_env = os.environ.get("FLAYR_ASR_API_KEY_ENV", "").strip()
        if asr_key_env:
            command.extend(["--asr-api-key-env", asr_key_env])
        return command

    def _finish(self, job_id: str, returncode: int) -> None:
        job = self.get(job_id)
        if not job:
            return
        run_dir = Path(str(job["run_dir"]))
        expected = {
            "benchmark_video": Path(str(job["benchmark_path"])),
            "creator_video": Path(str(job["creator_path"])),
        }
        state = _analysis_state(run_dir)
        if returncode == 0 and state == "completed" and validate_success_manifest(run_dir, expected):
            if _run_state(run_dir) == COMPLETED:
                self._update(
                    job_id,
                    status="completed",
                    progress=100,
                    phase="报告生成",
                    estimated_remaining_seconds=0,
                )
                return
            returncode = 1
        if returncode == 0 and state == "degraded" and (run_dir / "degraded_manifest.json").is_file() and report_variants_ready(run_dir):
            reason = "辅助产物已降级，不影响报告结论。"
            try:
                payload = json.loads((run_dir / "degraded_manifest.json").read_text(encoding="utf-8"))
                reason = str(payload.get("reason") or reason)
            except (OSError, json.JSONDecodeError):
                pass
            if _run_state(run_dir) == DEGRADED:
                self._update(
                    job_id,
                    status="degraded",
                    progress=100,
                    phase="报告生成（部分分析能力降级）",
                    estimated_remaining_seconds=0,
                    degraded_reason=reason[:500],
                )
                return
            returncode = 1
        reason = self._log_failure(Path(str(job["log_path"])), returncode)
        try:
            recover_run_state(run_dir, FAILED, job_id=job_id, reason=reason[:500])
        except RunStateError:
            pass
        self._update(
            job_id,
            status="failed",
            phase="分析失败",
            estimated_remaining_seconds=0,
            failure_reason=reason[:500],
        )

    @staticmethod
    def _log_failure(log_path: Path, returncode: int) -> str:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        meaningful = [line.strip() for line in lines if line.strip() and not line.startswith("$")]
        if meaningful:
            return f"分析未完成（退出码 {returncode}）：{meaningful[-1]}"
        return f"分析未完成（退出码 {returncode}），请重新上传后重试。"

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_requested = True
            running = list(self._running_processes.values())
        for process in running:
            stop_process_group(process, grace_seconds=5.0)
        self._executor.shutdown(wait=True, cancel_futures=True)


class FlayrServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: JobStore,
        *,
        public_mode: bool = False,
        auth_token: str = "",
        allowed_hosts: set[tuple[str, int | None]] | None = None,
    ) -> None:
        self.store = store
        self.public_mode = bool(public_mode)
        self.auth_token = auth_token
        self.allowed_hosts = set(allowed_hosts or ())
        if self.public_mode:
            if len(self.auth_token.encode("utf-8")) < WEB_AUTH_TOKEN_MIN_BYTES:
                raise ValueError(f"{WEB_AUTH_TOKEN_ENV} 至少需要 {WEB_AUTH_TOKEN_MIN_BYTES} 个字节")
            if not self.allowed_hosts:
                raise ValueError("对外监听必须设置允许的 Host")
        self.submission_limiter = SubmissionRateLimiter()
        self.client_cookie_secret = _load_client_cookie_secret(store.root)
        super().__init__(address, FlayrHandler)


class FlayrHandler(BaseHTTPRequestHandler):
    server: FlayrServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_HEADER_TIMEOUT_SECONDS)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[flayr-web] " + (format % args) + "\n")

    def _authorize_request(self, *, state_changing: bool = False) -> bool:
        if not self.server.public_mode:
            return True
        if not _host_matches(self.headers.get("Host", ""), self.server.allowed_hosts):
            self._json(403, {"error": "Host 不在允许列表"})
            return False
        scheme, separator, token = self.headers.get("Authorization", "").partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(token.strip(), self.server.auth_token)
        ):
            self._json(
                401,
                {"error": "需要操作者认证"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return False
        if state_changing and not _origin_matches(self.headers.get("Origin", ""), self.server.allowed_hosts):
            self._json(403, {"error": "请求来源不被允许"})
            return False
        return True

    def _client_id(self) -> str:
        cached = getattr(self, "_flayr_client_id", None)
        if cached:
            return cached
        value = ""
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            raw_value = cookie.get(CLIENT_COOKIE_NAME).value if cookie.get(CLIENT_COOKIE_NAME) else ""
            value = _verified_client_cookie(raw_value, self.server.client_cookie_secret) or ""
        except (CookieError, KeyError, ValueError):
            value = ""
        if not IDENTITY_PATTERN.fullmatch(value):
            value = uuid.uuid4().hex
            self._flayr_set_cookie = True
        self._flayr_client_id = value
        return value

    def _workspace_id(self, value: str | None = None) -> str | None:
        workspace_id = value or self.server.store.workspace_id
        try:
            workspace_id = _identity_value(workspace_id, "workspace_id", self.server.store.workspace_id)
        except RequestError:
            return None
        return workspace_id if workspace_id == self.server.store.workspace_id else None

    def _get_job(self, job_id: str, workspace_id: str | None = None) -> dict[str, Any] | None:
        workspace = self._workspace_id(workspace_id)
        if workspace is None:
            return None
        return self.server.store.get(
            job_id,
            owner_id=self._client_id(),
            workspace_id=workspace,
        )

    def do_GET(self) -> None:
        if not self._authorize_request():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        self._client_id()
        if path == "/api/jobs":
            jobs = self.server.store.all(
                owner_id=self._client_id(),
                workspace_id=self.server.store.workspace_id,
            )
            self._json(
                200,
                {
                    "jobs": [self.server.store.public(job) for job in jobs],
                    "recovery_warning": self.server.store.recovery_warning,
                },
            )
            return
        match = re.fullmatch(r"/api/workspaces/([^/]+)/jobs/([^/]+)/(analysis|report|creator-report)", path)
        if match:
            self._serve_job_artifact(match.group(2), match.group(3), workspace_id=match.group(1))
            return
        match = re.fullmatch(r"/api/workspaces/([^/]+)/jobs/([^/]+)/assets/(.+)", path)
        if match:
            self._serve_job_asset(match.group(2), match.group(3), workspace_id=match.group(1))
            return
        match = re.fullmatch(r"/api/workspaces/([^/]+)/jobs/([^/]+)", path)
        if match:
            job = self._get_job(match.group(2), match.group(1))
            if not job:
                self._json(404, {"error": "任务不存在"})
                return
            self._json(200, self.server.store.public(job))
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if match:
            job = self._get_job(match.group(1))
            if not job:
                self._json(404, {"error": "任务不存在"})
                return
            self._json(200, self.server.store.public(job))
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)/(analysis|report|creator-report)", path)
        if match:
            self._serve_job_artifact(match.group(1), match.group(2))
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)/assets/(.+)", path)
        if match:
            self._serve_job_asset(match.group(1), match.group(2))
            return
        if path in {"/", "/index.html"}:
            self._serve_file(FRONTEND_INDEX, "text/html; charset=utf-8")
            return
        frontend_asset = safe_asset_path(FRONTEND_ROOT, path.lstrip("/"))
        if frontend_asset:
            content_type = mimetypes.guess_type(frontend_asset.name)[0] or "application/octet-stream"
            self._serve_file(frontend_asset, content_type)
            return
        self._json(404, {"error": "资源不存在"})

    def do_HEAD(self) -> None:
        if not self._authorize_request():
            return
        path = unquote(urlsplit(self.path).path)
        self._client_id()
        match = re.fullmatch(r"/api/workspaces/([^/]+)/jobs/([^/]+)/(report|creator-report)", path)
        if match:
            self._serve_job_artifact(match.group(2), match.group(3), workspace_id=match.group(1), head_only=True)
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)/(report|creator-report)", path)
        if match:
            self._serve_job_artifact(match.group(1), match.group(2), head_only=True)
            return
        self.send_error(404, "资源不存在")

    def do_POST(self) -> None:
        if not self._authorize_request(state_changing=True):
            return
        path = unquote(urlsplit(self.path).path)
        if path != "/api/jobs":
            self._json(404, {"error": "资源不存在"})
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._json(415, {"error": "请使用 multipart/form-data 上传视频"})
            return
        owner_id = self._client_id()
        if self.server.public_mode:
            client_ip = str(self.client_address[0])
            if not self.server.submission_limiter.admit(
                (f"owner:{owner_id}", f"ip:{client_ip}"),
                limit=PUBLIC_SUBMISSIONS_PER_MINUTE,
                window_seconds=60.0,
            ):
                self._json(429, {"error": "提交过于频繁，请稍后重试。"}, extra_headers={"Connection": "close"})
                return
            admission_error = self.server.store.admission_error(
                owner_id=owner_id,
                workspace_id=self.server.store.workspace_id,
            )
            if admission_error:
                self._json(503, {"error": admission_error}, extra_headers={"Connection": "close"})
                return
        body_path: Path | None = None
        files: dict[str, dict[str, Any]] = {}
        reserved_storage_bytes = 0
        try:
            requested_storage_bytes = self._upload_storage_reservation_bytes()
            self.server.store.reserve_upload(self.server.store.workspace_id, requested_storage_bytes)
            reserved_storage_bytes = requested_storage_bytes
            upload_root = self.server.store.root
            upload_root.mkdir(parents=True, exist_ok=True)
            temp = tempfile.NamedTemporaryFile(prefix=".upload-body-", dir=upload_root, delete=False)
            body_path = Path(temp.name)
            with temp:
                self._read_request_body(temp)
            fields, files = parse_multipart(body_path, content_type, staging_root=upload_root)
            job = self.server.store.create(
                fields,
                files,
                owner_id=owner_id,
                workspace_id=self.server.store.workspace_id,
                enforce_limits=self.server.public_mode,
            )
            self._json(202, job)
        except AdmissionError as exc:
            self._json(503, {"error": str(exc)})
        except RequestError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"任务创建失败：{str(exc)[:200]}"})
        finally:
            cleanup_upload_files(files)
            if body_path:
                body_path.unlink(missing_ok=True)
            if reserved_storage_bytes:
                self.server.store.release_upload(
                    self.server.store.workspace_id,
                    reserved_storage_bytes,
                )

    def do_DELETE(self) -> None:
        if not self._authorize_request(state_changing=True):
            return
        path = unquote(urlsplit(self.path).path)
        match = re.fullmatch(r"/api/workspaces/([^/]+)/jobs/([^/]+)", path)
        if match:
            job_id = match.group(2)
            workspace_id = self._workspace_id(match.group(1))
        else:
            match = re.fullmatch(r"/api/jobs/([^/]+)", path)
            if not match:
                self._json(404, {"error": "资源不存在"})
                return
            job_id = match.group(1)
            workspace_id = self._workspace_id()
        if workspace_id is None or not self.server.store.delete(
            job_id,
            owner_id=self._client_id(),
            workspace_id=workspace_id,
        ):
            self._json(404, {"error": "任务不存在"})
            return
        self.send_response(204)
        self._send_identity_cookie()
        self.end_headers()

    def _upload_storage_reservation_bytes(self) -> int:
        try:
            content_length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            content_length = -1
        if content_length < 0:
            content_length = MAX_REQUEST_BYTES
        content_length = min(content_length, MAX_REQUEST_BYTES)
        return max(1, content_length) * 2

    def _read_request_body(self, destination: Any) -> None:
        started_at = time.monotonic()
        deadline = started_at + UPLOAD_TOTAL_TIMEOUT_SECONDS
        previous_timeout = self.connection.gettimeout()

        def read_with_deadline(reader: Any) -> bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestError("上传读取超时")
            self.connection.settimeout(min(UPLOAD_IDLE_TIMEOUT_SECONDS, remaining))
            try:
                result = reader()
            except socket.timeout as exc:
                raise RequestError("上传读取超时") from exc
            if time.monotonic() > deadline:
                raise RequestError("上传读取超时")
            return result

        try:
            transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
            if "chunked" in transfer_encoding:
                total = 0
                while True:
                    line = read_with_deadline(lambda: self.rfile.readline(128))
                    if not line or len(line) > 127:
                        raise RequestError("chunked 上传请求无效")
                    try:
                        size = int(line.split(b";", 1)[0].strip(), 16)
                    except ValueError as exc:
                        raise RequestError("chunked 上传请求无效") from exc
                    if size < 0:
                        raise RequestError("chunked 上传请求无效")
                    if size == 0:
                        while True:
                            trailer = read_with_deadline(lambda: self.rfile.readline(64 * 1024 + 1))
                            if trailer in {b"", b"\r\n", b"\n"}:
                                return
                            if len(trailer) > 64 * 1024:
                                raise RequestError("chunked trailer 过大")
                    remaining = size
                    while remaining:
                        chunk = read_with_deadline(
                            lambda: self.rfile.read(min(UPLOAD_CHUNK_BYTES, remaining))
                        )
                        if not chunk:
                            raise RequestError("上传请求提前结束")
                        total += len(chunk)
                        if total > MAX_REQUEST_BYTES:
                            raise RequestError("上传请求超过大小限制")
                        destination.write(chunk)
                        remaining -= len(chunk)
                    if read_with_deadline(lambda: self.rfile.read(2)) != b"\r\n":
                        raise RequestError("chunked 上传请求无效")
            try:
                content_length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                content_length = -1
            if content_length < 0:
                raise RequestError("上传请求必须包含 Content-Length 或 chunked 编码")
            if content_length > MAX_REQUEST_BYTES:
                raise RequestError("上传请求超过大小限制")
            remaining = content_length
            while remaining:
                chunk = read_with_deadline(lambda: self.rfile.read(min(UPLOAD_CHUNK_BYTES, remaining)))
                if not chunk:
                    raise RequestError("上传请求提前结束")
                destination.write(chunk)
                remaining -= len(chunk)
        finally:
            self.connection.settimeout(previous_timeout)

    def _serve_job_artifact(
        self,
        job_id: str,
        artifact: str,
        head_only: bool = False,
        workspace_id: str | None = None,
    ) -> None:
        job = self._get_job(job_id, workspace_id)
        if not job:
            self._json(404, {"error": "任务不存在"})
            return
        if artifact in {"report", "creator-report"} and job.get("status") not in {"completed", "degraded"}:
            self._json(409, {"error": "报告尚未生成"})
            return
        run_dir = Path(str(job.get("run_dir") or ""))
        if artifact == "report":
            report_names = ("bd_report.html", "report.html")
            candidate = next(
                (_safe_servable_asset(run_dir, name) for name in report_names),
                None,
            )
        elif artifact == "creator-report":
            candidate = _safe_servable_asset(run_dir, "creator_report.html")
        else:
            candidate = safe_asset_path(run_dir, "analysis.json")
        if not candidate:
            self._json(404, {"error": "产物不存在"})
            return
        content_type = "text/html; charset=utf-8" if artifact in {"report", "creator-report"} else "application/json; charset=utf-8"
        self._serve_file(candidate, content_type, head_only=head_only)

    def _serve_job_asset(
        self,
        job_id: str,
        relative_path: str,
        workspace_id: str | None = None,
    ) -> None:
        job = self._get_job(job_id, workspace_id)
        if not job:
            self._json(404, {"error": "任务不存在"})
            return
        candidate = safe_asset_path(Path(str(job.get("run_dir") or "")), relative_path)
        if not candidate:
            self._json(404, {"error": "资源不存在"})
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._serve_file(candidate, content_type)

    def _serve_file(self, path: Path, content_type: str, head_only: bool = False) -> None:
        source = None
        try:
            size = path.stat().st_size
            if size > MAX_SERVED_ASSET_BYTES or not _servable_asset(path):
                self._json(415, {"error": "资源内容类型不匹配"})
                return
            if not head_only:
                source = path.open("rb")
                size = os.fstat(source.fileno()).st_size
                if size > MAX_SERVED_ASSET_BYTES:
                    source.close()
                    source = None
                    self._json(415, {"error": "资源内容类型不匹配"})
                    return
        except OSError:
            if source is not None:
                source.close()
            self._json(404, {"error": "资源不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self._send_identity_cookie()
        self.end_headers()
        if source is not None:
            try:
                remaining = size
                while remaining:
                    chunk = source.read(min(ASSET_STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            finally:
                source.close()

    def _json(
        self,
        status: int,
        payload: Any,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self._send_identity_cookie()
        self.end_headers()
        self.wfile.write(data)

    def _send_identity_cookie(self) -> None:
        if getattr(self, "_flayr_set_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{CLIENT_COOKIE_NAME}={_signed_client_cookie(self._client_id(), self.server.client_cookie_secret)}; "
                f"Path=/; Max-Age={CLIENT_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax",
            )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the local Flayr web application.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLAYR_WEB_PORT", DEFAULT_PORT)))
    parser.add_argument(
        "--unsafe-expose",
        action="store_true",
        help="允许非本机访问；必须同时配置操作者 Bearer token 和允许的 Host。",
    )
    args = parser.parse_args()
    try:
        public_mode, auth_token, allowed_hosts = _resolve_web_security(args.host, args.unsafe_expose)
    except ValueError as exc:
        parser.error(str(exc))
    store = JobStore()
    server = FlayrServer(
        (args.host, args.port),
        store,
        public_mode=public_mode,
        auth_token=auth_token,
        allowed_hosts=allowed_hosts,
    )
    print(f"Flayr web app: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        store.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
