#!/usr/bin/env python3
"""Small local web service for the Flayr upload, jobs, and report flow."""

from __future__ import annotations

import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
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
    PROCESSING,
    REPORT_GENERATING,
    RunStateError,
    initialize_run_state,
    read_run_state,
    recover_run_state,
    transition_run_state,
)


WEB_ROOT = ROOT / "runs" / "_web"
JOBS_ROOT = WEB_ROOT / "jobs"
JOBS_FILE = WEB_ROOT / "jobs.json"
FRONTEND_INDEX = ROOT / "frontend" / "index.html"
FRONTEND_ROOT = ROOT / "frontend"
DEFAULT_PORT = 8787
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_FIELD_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_VIDEO_BYTES * 2 + 8 * 1024 * 1024
MAX_SERVED_ASSET_BYTES = MAX_VIDEO_BYTES
UPLOAD_CHUNK_BYTES = 64 * 1024
BOUNDARY_BYTES_LIMIT = 200
DEFAULT_WORKSPACE_ID = "local"
DEFAULT_OWNER_ID = "local"
CLIENT_COOKIE_NAME = "flayr_client_id"
CLIENT_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MARKET_CODES = {
    "马来西亚": "my",
    "泰国": "th",
    "印尼": "id",
    "其他东南亚市场": "sea",
    "未指定市场": "auto",
}


class RequestError(ValueError):
    """A client-side request error that should be returned as JSON."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


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
    if lifecycle_state in {COMPLETED, DEGRADED}:
        return 100, "报告生成"
    if lifecycle_state in {ANALYSIS_COMPLETED, REPORT_GENERATING}:
        return 92, "报告生成"
    state = _analysis_state(run_dir)
    if state == "completed" and (run_dir / SUCCESS_MANIFEST_NAME).is_file():
        return 100, "报告生成"
    if state == "degraded" and (run_dir / "degraded_manifest.json").is_file() and report_variants_ready(run_dir):
        return 100, "报告生成"
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
    return max(0, round(18 * 60 * (1 - max(0, min(progress, 100)) / 100)))


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


def report_variants_ready(run_dir: Path) -> bool:
    """Return true only when both audience reports are safely available."""
    return all(
        safe_asset_path(run_dir, name) is not None
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


def parse_multipart(body_path: Path, content_type: str) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
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
    temp_paths: list[Path] = []
    WEB_ROOT.mkdir(parents=True, exist_ok=True)
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
                if filename is not None:
                    safe_name = Path(filename).name or "upload.bin"
                    temp = tempfile.NamedTemporaryFile(prefix=".upload-part-", dir=WEB_ROOT, delete=False)
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


class JobStore:
    def __init__(self, root: Path = WEB_ROOT, *, workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
        self.root = root
        self.jobs_root = root / "jobs"
        self.state_path = root / "jobs.json"
        self.workspace_id = _identity_value(workspace_id, "workspace_id", DEFAULT_WORKSPACE_ID)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flayr-job")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._load()
        self._recover_incomplete()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            self.jobs = {str(key): value for key, value in payload.items() if isinstance(value, dict)}
        changed = False
        for job in self.jobs.values():
            if not job.get("workspace_id"):
                job["workspace_id"] = self.workspace_id
                changed = True
            if not job.get("visibility"):
                job["visibility"] = "private"
                changed = True
        if changed:
            self._persist_locked()

    def _persist_locked(self) -> None:
        temp = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(self.jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

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
                        "phase": "报告生成",
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

    def create(
        self,
        fields: dict[str, str],
        files: dict[str, dict[str, Any]],
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        workspace_id: str | None = None,
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
            raise RequestError(str(exc)) from exc
        owner_id = _identity_value(owner_id, "owner_id", DEFAULT_OWNER_ID)
        workspace_id = _identity_value(workspace_id, "workspace_id", self.workspace_id)
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job_root = self.jobs_root / job_id
        input_root = job_root / "inputs"
        run_dir = job_root / "run"
        input_root.mkdir(parents=True, exist_ok=False)
        moved: dict[str, Path] = {}
        try:
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
        initialize_run_state(run_dir, job_id=job_id)
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
            "estimated_remaining_seconds": 18 * 60,
            "created_at": now,
            "failure_reason": "",
            "degraded_reason": "",
            "benchmark_path": str(moved["benchmark_video"].resolve()),
            "creator_path": str(moved["creator_video"].resolve()),
            "run_dir": str(run_dir.resolve()),
            "log_path": str((job_root / "worker.log").resolve()),
        }
        with self._lock:
            self.jobs[job_id] = job
            self._persist_locked()
        self._executor.submit(self._run_job, job_id)
        return self.public(job)

    @staticmethod
    def _matches_scope(job: dict[str, Any], owner_id: str | None, workspace_id: str | None) -> bool:
        if workspace_id is not None and str(job.get("workspace_id") or DEFAULT_WORKSPACE_ID) != workspace_id:
            return False
        if owner_id is None:
            return True
        stored_owner = str(job.get("owner_id") or "")
        # Jobs created before ownership metadata was introduced can be claimed
        # by the first authenticated local browser that opens them.
        return not stored_owner or stored_owner == owner_id

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
                if owner is not None and not job.get("owner_id"):
                    job["owner_id"] = owner
                    self._persist_locked()
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
        has_bd_report = safe_asset_path(run_dir, "bd_report.html") is not None
        has_legacy_report = safe_asset_path(run_dir, "report.html") is not None
        has_creator_report = safe_asset_path(run_dir, "creator_report.html") is not None
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

    @staticmethod
    def _run_artifacts(run_dir: Path) -> tuple[str, ...]:
        names = [
            name
            for name in (
                "analysis.json",
                "validated_normalized_result.json",
                "final_derived_result.json",
                "postprocess_change_log.json",
                "bd_report.html",
                "creator_report.html",
                "report.html",
            )
            if (run_dir / name).is_file()
        ]
        return tuple(names)

    def _advance_run_state(self, run_dir: Path) -> None:
        """Advance only when the next lifecycle artifact is observable."""
        state = _run_state(run_dir)
        artifacts = self._run_artifacts(run_dir)
        try:
            if state == PROCESSING and any(
                name in artifacts
                for name in ("validated_normalized_result.json", "final_derived_result.json", "postprocess_change_log.json")
            ):
                transition_run_state(
                    run_dir,
                    ANALYSIS_COMPLETED,
                    artifacts=artifacts,
                )
                state = ANALYSIS_COMPLETED
            if state == ANALYSIS_COMPLETED and any(
                name in artifacts for name in ("bd_report.html", "creator_report.html", "report.html")
            ):
                transition_run_state(
                    run_dir,
                    REPORT_GENERATING,
                    artifacts=artifacts,
                )
        except RunStateError:
            # The terminal result path performs an explicit consistency check;
            # a transient or corrupt state must not make polling crash.
            return

    def _publish_terminal_state(
        self,
        run_dir: Path,
        job_id: str,
        target: str,
        *,
        reason: str = "",
        artifacts: tuple[str, ...] = (),
    ) -> bool:
        current = _run_state(run_dir)
        if current in {COMPLETED, DEGRADED, FAILED}:
            return current == target
        try:
            # Complete normal transitions that may have happened between two
            # five-second polling ticks before publishing the terminal state.
            if current == PROCESSING:
                transition_run_state(run_dir, ANALYSIS_COMPLETED, artifacts=artifacts)
                current = ANALYSIS_COMPLETED
            if current == ANALYSIS_COMPLETED:
                transition_run_state(run_dir, REPORT_GENERATING, artifacts=artifacts)
            transition_run_state(run_dir, target, reason=reason, artifacts=artifacts)
        except RunStateError:
            return False
        return True

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
        try:
            transition_run_state(run_dir, PROCESSING, artifacts=self._run_artifacts(run_dir))
            with log_path.open("ab") as log:
                log.write(("$ " + " ".join(command) + "\n").encode("utf-8", "replace"))
                process = subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                )
                while process.poll() is None:
                    current = self.get(job_id)
                    if current:
                        self._advance_run_state(run_dir)
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
        ]
        model = os.environ.get("FLAYR_LLM_MODEL", "").strip()
        if model:
            command.extend(
                [
                    "--llm-model",
                    model,
                    "--llm-api-url",
                    os.environ.get("FLAYR_LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
                    "--llm-api-key-env",
                    os.environ.get("FLAYR_LLM_API_KEY_ENV", "OPENAI_API_KEY"),
                ]
            )
        if os.environ.get("FLAYR_ALLOW_DEGRADED", "").strip().lower() in {"1", "true", "yes"}:
            command.append("--allow-degraded")
        whisper_model = os.environ.get("FLAYR_WHISPER_MODEL", "").strip()
        if whisper_model:
            command.extend(["--whisper-model", whisper_model])
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
            if not self._publish_terminal_state(
                run_dir,
                job_id,
                COMPLETED,
                artifacts=self._run_artifacts(run_dir) + (SUCCESS_MANIFEST_NAME,),
            ):
                returncode = 1
            else:
                self._update(
                    job_id,
                    status="completed",
                    progress=100,
                    phase="报告生成",
                    estimated_remaining_seconds=0,
                )
                return
        if returncode == 0 and state == "degraded" and (run_dir / "degraded_manifest.json").is_file() and report_variants_ready(run_dir):
            reason = "辅助产物已降级，不影响报告结论。"
            try:
                payload = json.loads((run_dir / "degraded_manifest.json").read_text(encoding="utf-8"))
                reason = str(payload.get("reason") or reason)
            except (OSError, json.JSONDecodeError):
                pass
            if self._publish_terminal_state(
                run_dir,
                job_id,
                DEGRADED,
                reason=reason[:500],
                artifacts=self._run_artifacts(run_dir) + ("degraded_manifest.json",),
            ):
                self._update(
                    job_id,
                    status="degraded",
                    progress=100,
                    phase="报告生成",
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
        self._executor.shutdown(wait=False, cancel_futures=True)


class FlayrServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], store: JobStore) -> None:
        self.store = store
        super().__init__(address, FlayrHandler)


class FlayrHandler(BaseHTTPRequestHandler):
    server: FlayrServer

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[flayr-web] " + (format % args) + "\n")

    def _client_id(self) -> str:
        cached = getattr(self, "_flayr_client_id", None)
        if cached:
            return cached
        value = ""
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(CLIENT_COOKIE_NAME).value if cookie.get(CLIENT_COOKIE_NAME) else ""
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
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        self._client_id()
        if path == "/api/jobs":
            jobs = self.server.store.all(
                owner_id=self._client_id(),
                workspace_id=self.server.store.workspace_id,
            )
            self._json(200, {"jobs": [self.server.store.public(job) for job in jobs]})
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
        path = unquote(urlsplit(self.path).path)
        self._client_id()
        if path != "/api/jobs":
            self._json(404, {"error": "资源不存在"})
            return
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._json(415, {"error": "请使用 multipart/form-data 上传视频"})
            return
        body_path: Path | None = None
        files: dict[str, dict[str, Any]] = {}
        try:
            WEB_ROOT.mkdir(parents=True, exist_ok=True)
            temp = tempfile.NamedTemporaryFile(prefix=".upload-body-", dir=WEB_ROOT, delete=False)
            body_path = Path(temp.name)
            with temp:
                self._read_request_body(temp)
            fields, files = parse_multipart(body_path, content_type)
            job = self.server.store.create(
                fields,
                files,
                owner_id=self._client_id(),
                workspace_id=self.server.store.workspace_id,
            )
            self._json(202, job)
        except RequestError as exc:
            for item in files.values():
                Path(str(item.get("path"))).unlink(missing_ok=True)
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            for item in files.values():
                Path(str(item.get("path"))).unlink(missing_ok=True)
            self._json(500, {"error": f"任务创建失败：{str(exc)[:200]}"})
        finally:
            if body_path:
                body_path.unlink(missing_ok=True)

    def _read_request_body(self, destination: Any) -> None:
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            total = 0
            while True:
                line = self.rfile.readline(128)
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
                        trailer = self.rfile.readline(64 * 1024 + 1)
                        if trailer in {b"", b"\r\n", b"\n"}:
                            return
                        if len(trailer) > 64 * 1024:
                            raise RequestError("chunked trailer 过大")
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise RequestError("上传请求提前结束")
                    total += len(chunk)
                    if total > MAX_REQUEST_BYTES:
                        raise RequestError("上传请求超过大小限制")
                    destination.write(chunk)
                    remaining -= len(chunk)
                if self.rfile.read(2) != b"\r\n":
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
            chunk = self.rfile.read(min(UPLOAD_CHUNK_BYTES, remaining))
            if not chunk:
                raise RequestError("上传请求提前结束")
            destination.write(chunk)
            remaining -= len(chunk)

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
                (safe_asset_path(run_dir, name) for name in report_names if (run_dir / name).is_file()),
                None,
            )
        elif artifact == "creator-report":
            candidate = safe_asset_path(run_dir, "creator_report.html") if (run_dir / "creator_report.html").is_file() else None
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
        try:
            size = path.stat().st_size
            if size > MAX_SERVED_ASSET_BYTES or not _servable_asset(path):
                self._json(415, {"error": "资源内容类型不匹配"})
                return
            data = None if head_only else path.read_bytes()
        except OSError:
            self._json(404, {"error": "资源不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size if data is None else len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self._send_identity_cookie()
        self.end_headers()
        if data is not None:
            self.wfile.write(data)

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._send_identity_cookie()
        self.end_headers()
        self.wfile.write(data)

    def _send_identity_cookie(self) -> None:
        if getattr(self, "_flayr_set_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{CLIENT_COOKIE_NAME}={self._client_id()}; Path=/; Max-Age={CLIENT_COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax",
            )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the local Flayr web application.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLAYR_WEB_PORT", DEFAULT_PORT)))
    args = parser.parse_args()
    store = JobStore()
    server = FlayrServer((args.host, args.port), store)
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
