"""Per-run resource budgets for media processing and model analysis.

The budget is deliberately small and explicit.  It is shared by the whole
local run, rather than recreated by each helper, so retries and optional
stages cannot bypass the same limits.
"""

from __future__ import annotations

import base64
import contextvars
import math
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO


MIB = 1024 * 1024
GIB = 1024 * MIB

# Custom callers may tighten defaults, but cannot replace them with unbounded
# values. These are intentionally generous hard ceilings for a local worker.
_HARD_LIMITS = {
    "max_source_bytes": 2 * GIB,
    "max_extracted_frames": 1_000_000,
    "max_ocr_calls": 10_000,
    "max_llm_calls": 1_000,
    "max_single_request_bytes": 512 * MIB,
    "max_total_uploaded_bytes": 4 * GIB,
    "max_download_bytes": 4 * GIB,
    "max_report_bytes": 512 * MIB,
    "max_local_artifact_bytes": 4 * GIB,
    "max_source_duration": 24 * 60 * 60.0,
    "max_total_wall_time": 24 * 60 * 60.0,
    "max_cost_estimate": 10_000.0,
}


class ResourceBudgetExceeded(RuntimeError):
    """Raised when a run would exceed a hard resource limit."""


def finite_nonnegative(value: Any, name: str, *, maximum: float | None = None) -> float:
    """Parse a finite, non-negative number and optionally apply an upper bound."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} exceeds maximum {maximum}")
    return number


@dataclass(frozen=True)
class ResourceLimits:
    """Hard defaults for one Flayr analysis run.

    Source duration is checked per input file. Source bytes and all other
    counters are cumulative across both videos and every downstream stage.
    """

    max_source_bytes: int = 512 * MIB
    max_source_duration: float = 600.0
    max_extracted_frames: int = 1500
    max_ocr_calls: int = 80
    max_llm_calls: int = 20
    max_single_request_bytes: int = 64 * MIB
    max_total_uploaded_bytes: int = 256 * MIB
    max_download_bytes: int = 128 * MIB
    max_report_bytes: int = 64 * MIB
    max_local_artifact_bytes: int = 512 * MIB
    max_total_wall_time: float = 1800.0
    max_cost_estimate: float = 5.0

    def __post_init__(self) -> None:
        integer_fields = (
            "max_source_bytes",
            "max_extracted_frames",
            "max_ocr_calls",
            "max_llm_calls",
            "max_single_request_bytes",
            "max_total_uploaded_bytes",
            "max_download_bytes",
            "max_report_bytes",
            "max_local_artifact_bytes",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
            if value > _HARD_LIMITS[field_name]:
                raise ValueError(f"{field_name} exceeds hard ceiling {_HARD_LIMITS[field_name]}")
        for field_name in ("max_source_duration", "max_total_wall_time", "max_cost_estimate"):
            finite_nonnegative(getattr(self, field_name), field_name, maximum=_HARD_LIMITS[field_name])


_ACTIVE_BUDGET: contextvars.ContextVar[ResourceBudget | None] = contextvars.ContextVar(
    "flayr_resource_budget", default=None
)


@dataclass
class ResourceBudget:
    """Mutable accounting state for one complete run."""

    limits: ResourceLimits = field(default_factory=ResourceLimits)
    started_at: float = field(default_factory=time.monotonic)
    source_bytes: int = 0
    source_duration_seconds: float = 0.0
    extracted_frames: int = 0
    ocr_calls: int = 0
    llm_calls: int = 0
    total_uploaded_bytes: int = 0
    total_downloaded_bytes: int = 0
    report_bytes: int = 0
    local_artifact_bytes: int = 0
    cost_estimate: float = 0.0
    api_events: list[dict[str, Any]] = field(default_factory=list)

    def activate(self) -> contextvars.Token[ResourceBudget | None]:
        return _ACTIVE_BUDGET.set(self)

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def remaining_wall_seconds(self) -> float:
        return max(0.0, self.limits.max_total_wall_time - self.elapsed_seconds())

    def check_wall_time(self) -> None:
        if self.remaining_wall_seconds() <= 0:
            raise ResourceBudgetExceeded(
                f"total wall time budget exceeded ({self.limits.max_total_wall_time:.0f}s)"
            )

    def register_source(self, path: Path, duration_seconds: Any) -> float:
        """Validate one source before any expensive media operation."""
        resolved, size = self.preflight_source(path)
        try:
            duration = finite_nonnegative(
                duration_seconds,
                "source duration",
                maximum=self.limits.max_source_duration,
            )
        except ValueError as exc:
            raise ResourceBudgetExceeded(f"cannot safely accept source {resolved}: {exc}") from exc
        self.source_bytes += size
        self.source_duration_seconds += duration
        return duration

    def preflight_source(self, path: Path) -> tuple[Path, int]:
        """Check source existence and bytes before ffprobe/ffmpeg touches it."""
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ResourceBudgetExceeded(f"source file is missing: {resolved}")
        size = resolved.stat().st_size
        if size < 0 or self.source_bytes + size > self.limits.max_source_bytes:
            raise ResourceBudgetExceeded(
                f"source byte budget exceeded: {self.source_bytes + size} > "
                f"max_source_bytes={self.limits.max_source_bytes} ({resolved}, {size} bytes)"
            )
        return resolved, size

    def reserve_frames(self, count: int) -> None:
        count = _nonnegative_int(count, "extracted frames")
        if self.extracted_frames + count > self.limits.max_extracted_frames:
            raise ResourceBudgetExceeded(
                f"extracted frame budget exceeded: {self.extracted_frames + count} > "
                f"{self.limits.max_extracted_frames}"
            )
        self.extracted_frames += count

    def release_frames(self, count: int) -> None:
        self.extracted_frames = max(0, self.extracted_frames - _nonnegative_int(count, "released frames"))

    def reserve_api_call(
        self,
        request_bytes: int,
        *,
        kind: str = "llm",
        estimated_cost: float | None = None,
        request_id: str | None = None,
        attempt: int | None = None,
        retry_reason: str | None = None,
    ) -> None:
        """Reserve one actual network model attempt, including retries."""
        request_bytes = _nonnegative_int(request_bytes, "request bytes")
        if request_bytes > self.limits.max_single_request_bytes:
            raise ResourceBudgetExceeded(
                f"single request exceeds max_single_request_bytes={self.limits.max_single_request_bytes}: "
                f"{request_bytes} bytes"
            )
        if self.total_uploaded_bytes + request_bytes > self.limits.max_total_uploaded_bytes:
            raise ResourceBudgetExceeded(
                f"total upload budget exceeded: {self.total_uploaded_bytes + request_bytes} > "
                f"{self.limits.max_total_uploaded_bytes}"
            )
        if kind == "ocr":
            if self.ocr_calls + 1 > self.limits.max_ocr_calls:
                raise ResourceBudgetExceeded(
                    f"OCR call budget exceeded: {self.ocr_calls + 1} > {self.limits.max_ocr_calls}"
                )
            self.ocr_calls += 1
        else:
            if self.llm_calls + 1 > self.limits.max_llm_calls:
                raise ResourceBudgetExceeded(
                    f"LLM call budget exceeded: {self.llm_calls + 1} > {self.limits.max_llm_calls}"
                )
            self.llm_calls += 1
        self.total_uploaded_bytes += request_bytes
        cost = estimated_cost if estimated_cost is not None else (0.01 if kind == "ocr" else 0.10)
        self.reserve_cost(cost)
        self.check_wall_time()
        self.api_events.append(
            {
                "request_id": str(request_id or ""),
                "kind": str(kind),
                "attempt": (
                    attempt
                    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0
                    else None
                ),
                "retry_reason": str(retry_reason or "")[:200],
                "request_bytes": request_bytes,
                "estimated_cost": round(float(cost), 4),
            }
        )

    def reserve_upload(self, size: int, *, estimated_cost: float = 0.0) -> None:
        """Reserve a non-chat upload, such as an analysis-media input."""
        size = _nonnegative_int(size, "upload bytes")
        if size > self.limits.max_single_request_bytes:
            raise ResourceBudgetExceeded(f"single upload exceeds max_single_request_bytes: {size} bytes")
        if self.total_uploaded_bytes + size > self.limits.max_total_uploaded_bytes:
            raise ResourceBudgetExceeded("total upload budget exceeded")
        self.total_uploaded_bytes += size
        self.reserve_cost(estimated_cost)
        self.check_wall_time()

    def reserve_download(self, size: int) -> None:
        size = _nonnegative_int(size, "download bytes")
        if self.total_downloaded_bytes + size > self.limits.max_download_bytes:
            raise ResourceBudgetExceeded(
                f"download budget exceeded: {self.total_downloaded_bytes + size} > {self.limits.max_download_bytes}"
            )
        self.total_downloaded_bytes += size
        self.check_wall_time()

    def reserve_cost(self, amount: Any) -> None:
        amount = finite_nonnegative(amount, "cost estimate")
        if self.cost_estimate + amount > self.limits.max_cost_estimate + 1e-9:
            raise ResourceBudgetExceeded(
                f"cost estimate budget exceeded: {self.cost_estimate + amount:.4f} > "
                f"{self.limits.max_cost_estimate:.4f}"
            )
        self.cost_estimate += amount

    def reserve_report(self, size: int) -> None:
        size = _nonnegative_int(size, "report bytes")
        if size > self.limits.max_report_bytes:
            raise ResourceBudgetExceeded(
                f"report exceeds max_report_bytes={self.limits.max_report_bytes}: {size} bytes"
            )
        self.report_bytes = size
        self.check_wall_time()

    def reserve_local_artifact(self, size: int) -> None:
        """Account for generated media kept in the run directory."""
        size = _nonnegative_int(size, "local artifact bytes")
        if self.local_artifact_bytes + size > self.limits.max_local_artifact_bytes:
            raise ResourceBudgetExceeded(
                f"local artifact budget exceeded: {self.local_artifact_bytes + size} > "
                f"max_local_artifact_bytes={self.limits.max_local_artifact_bytes}"
            )
        self.local_artifact_bytes += size
        self.check_wall_time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": {
                key: getattr(self.limits, key)
                for key in (
                    "max_source_bytes", "max_source_duration", "max_extracted_frames", "max_ocr_calls",
                    "max_llm_calls", "max_single_request_bytes", "max_total_uploaded_bytes",
                    "max_download_bytes", "max_report_bytes", "max_total_wall_time", "max_cost_estimate",
                    "max_local_artifact_bytes",
                )
            },
            "used": {
                "source_bytes": self.source_bytes,
                "source_duration_seconds": round(self.source_duration_seconds, 3),
                "extracted_frames": self.extracted_frames,
                "ocr_calls": self.ocr_calls,
                "llm_calls": self.llm_calls,
                "total_uploaded_bytes": self.total_uploaded_bytes,
                "total_downloaded_bytes": self.total_downloaded_bytes,
                "report_bytes": self.report_bytes,
                "local_artifact_bytes": self.local_artifact_bytes,
                "cost_estimate": round(self.cost_estimate, 4),
                "elapsed_seconds": round(self.elapsed_seconds(), 3),
                "api_events": list(self.api_events),
            },
        }


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def current_budget() -> ResourceBudget | None:
    return _ACTIVE_BUDGET.get()


def encode_file_data_url(path: Path, *, max_bytes: int, expected_kind: str = "image") -> str:
    """Bounded, signature-checked base64 encoding for providers without file upload APIs.

    The file is read in chunks; the encoded string is still assembled because the
    current OpenAI-compatible providers accept a data URL, not a local path.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if not path.is_file():
        raise ResourceBudgetExceeded(f"media file is missing: {path}")
    size = path.stat().st_size
    if size < 0 or size > max_bytes:
        raise ResourceBudgetExceeded(f"media file exceeds byte limit: {path} ({size} bytes)")
    mime_type = _sniff_mime_type(path)
    if expected_kind == "image" and not mime_type.startswith("image/"):
        raise ResourceBudgetExceeded(f"media signature is not an image: {path}")
    if expected_kind == "audio" and not mime_type.startswith("audio/"):
        raise ResourceBudgetExceeded(f"media signature is not audio: {path}")
    if expected_kind == "video" and not mime_type.startswith("video/"):
        raise ResourceBudgetExceeded(f"media signature is not a video: {path}")
    parts: list[str] = []
    remainder = b""
    bytes_read = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise ResourceBudgetExceeded(f"media file grew beyond byte limit while reading: {path}")
            data = remainder + chunk
            usable = len(data) - (len(data) % 3)
            if usable:
                parts.append(base64.b64encode(data[:usable]).decode("ascii"))
            remainder = data[usable:]
    if remainder:
        parts.append(base64.b64encode(remainder).decode("ascii"))
    return f"data:{mime_type};base64,{''.join(parts)}"


def _sniff_mime_type(path: Path) -> str:
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "video/mp4"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[:2] == b"\xFF\xFB"):
        return "audio/mpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    guessed = mimetypes.guess_type(path.name)[0]
    raise ResourceBudgetExceeded(f"unsupported or unverifiable media signature: {path} ({guessed or 'unknown'})")
