"""Explicit lifecycle state for a web-managed Flayr run.

The analysis artifacts describe what was produced.  This file describes the
run lifecycle itself, so a restart can distinguish an incomplete run from a
published result without treating an arbitrary artifact as a completion flag.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

from .utils import write_json


RUN_STATE_FILE = "run_state.json"
RUN_STATE_SCHEMA_VERSION = 1

CREATED = "CREATED"
PROCESSING = "PROCESSING"
ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
REPORT_GENERATING = "REPORT_GENERATING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
DEGRADED = "DEGRADED"

RUN_STATES = frozenset(
    {
        CREATED,
        PROCESSING,
        ANALYSIS_COMPLETED,
        REPORT_GENERATING,
        COMPLETED,
        FAILED,
        DEGRADED,
    }
)
TERMINAL_RUN_STATES = frozenset({COMPLETED, FAILED, DEGRADED})
_ALLOWED_TRANSITIONS = {
    CREATED: frozenset({PROCESSING, FAILED}),
    PROCESSING: frozenset({ANALYSIS_COMPLETED, FAILED}),
    ANALYSIS_COMPLETED: frozenset({REPORT_GENERATING, FAILED}),
    REPORT_GENERATING: frozenset({COMPLETED, DEGRADED, FAILED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    DEGRADED: frozenset(),
}


class RunStateError(ValueError):
    """Raised when a lifecycle transition or state file is invalid."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _state_path(run_dir: Path) -> Path:
    return run_dir / RUN_STATE_FILE


def _clean_artifacts(artifacts: Iterable[str] | None) -> list[str]:
    if artifacts is None:
        return []
    return sorted({str(item) for item in artifacts if str(item).strip()})


def read_run_state(run_dir: Path) -> dict[str, Any] | None:
    """Read a valid lifecycle state, returning ``None`` for missing/corrupt state."""
    try:
        payload = json.loads(_state_path(run_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
        return None
    if payload.get("state") not in RUN_STATES:
        return None
    if not isinstance(payload.get("history"), list):
        return None
    return payload


def _new_state(job_id: str) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "job_id": str(job_id or ""),
        "state": CREATED,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "reason": "",
        "artifacts": [],
        "history": [{"state": CREATED, "at": now}],
    }


def initialize_run_state(run_dir: Path, *, job_id: str = "") -> dict[str, Any]:
    """Create the initial ``CREATED`` state exactly once."""
    run_dir.mkdir(parents=True, exist_ok=True)
    existing = read_run_state(run_dir)
    if existing is not None:
        if job_id and existing.get("job_id") not in {"", job_id}:
            raise RunStateError("run_state job_id 与当前任务不一致")
        return existing
    state = _new_state(job_id)
    write_json(_state_path(run_dir), state)
    return state


def reset_run_state(run_dir: Path, *, job_id: str | None = None) -> dict[str, Any]:
    """Start a fresh lifecycle while retaining the existing job identity."""
    existing = read_run_state(run_dir)
    if job_id is None:
        job_id = str(existing.get("job_id") or "") if existing else ""
    state = _new_state(job_id)
    write_json(_state_path(run_dir), state)
    return state


def _apply_state(
    current: dict[str, Any],
    target: str,
    *,
    reason: str = "",
    artifacts: Iterable[str] | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    now = _utc_now()
    next_state = dict(current)
    history = list(current.get("history") or [])
    event: dict[str, Any] = {"state": target, "at": now}
    if reason:
        event["reason"] = reason
    if recovered:
        event["recovered"] = True
        event["from_state"] = current.get("state")
    history.append(event)
    next_state["state"] = target
    next_state["updated_at"] = now
    next_state["history"] = history
    next_state["reason"] = reason
    next_state["artifacts"] = _clean_artifacts(artifacts)
    if target == PROCESSING and not next_state.get("started_at"):
        next_state["started_at"] = now
    if target in TERMINAL_RUN_STATES:
        next_state["completed_at"] = now
    return next_state


def transition_run_state(
    run_dir: Path,
    target: str,
    *,
    reason: str = "",
    artifacts: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Apply one normal lifecycle transition and persist it atomically."""
    target = str(target).strip().upper()
    if target not in RUN_STATES:
        raise RunStateError(f"未知运行状态：{target}")
    current = read_run_state(run_dir)
    if current is None:
        raise RunStateError("run_state.json 缺失或损坏")
    current_state = str(current["state"])
    if target == current_state:
        return current
    if target not in _ALLOWED_TRANSITIONS[current_state]:
        raise RunStateError(f"禁止运行状态转换：{current_state} -> {target}")
    next_state = _apply_state(current, target, reason=reason, artifacts=artifacts)
    write_json(_state_path(run_dir), next_state)
    return next_state


def begin_report_generation(
    run_dir: Path,
    *,
    job_id: str = "",
    artifacts: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Move a run into ``REPORT_GENERATING`` before report files are written."""
    current = read_run_state(run_dir)
    if current is None:
        current = initialize_run_state(run_dir, job_id=job_id)
    elif job_id and current.get("job_id") not in {"", job_id}:
        raise RunStateError("run_state job_id 与当前任务不一致")
    if current["state"] in TERMINAL_RUN_STATES:
        raise RunStateError(f"终态任务不能开始报告生成：{current['state']}")
    artifact_names = _clean_artifacts(artifacts)
    state = current["state"]
    if state == CREATED:
        current = transition_run_state(run_dir, PROCESSING, artifacts=artifact_names)
        state = current["state"]
    if state == PROCESSING:
        current = transition_run_state(run_dir, ANALYSIS_COMPLETED, artifacts=artifact_names)
        state = current["state"]
    if state == ANALYSIS_COMPLETED:
        current = transition_run_state(run_dir, REPORT_GENERATING, artifacts=artifact_names)
    elif state != REPORT_GENERATING:
        raise RunStateError(f"无法开始报告生成：当前状态为 {state}")
    return current


def recover_run_state(
    run_dir: Path,
    target: str,
    *,
    job_id: str = "",
    reason: str = "",
    artifacts: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Publish a terminal state after restart recovery.

    Recovery may close several unfinished phases at once, but it is recorded
    as a recovery event rather than silently pretending normal transitions ran.
    """
    target = str(target).strip().upper()
    if target not in TERMINAL_RUN_STATES:
        raise RunStateError("恢复只能写入终态")
    current = read_run_state(run_dir)
    if current is None:
        current = initialize_run_state(run_dir, job_id=job_id)
    elif job_id and current.get("job_id") not in {"", job_id}:
        raise RunStateError("run_state job_id 与当前任务不一致")
    if current.get("state") in TERMINAL_RUN_STATES:
        if current.get("state") != target:
            if target != FAILED:
                raise RunStateError(f"终态不一致：{current.get('state')} != {target}")
            next_state = _apply_state(
                current,
                FAILED,
                reason=reason or "恢复时发现终态与产物不一致。",
                artifacts=artifacts,
                recovered=True,
            )
            write_json(_state_path(run_dir), next_state)
            return next_state
        return current
    next_state = _apply_state(
        current,
        target,
        reason=reason,
        artifacts=artifacts,
        recovered=True,
    )
    write_json(_state_path(run_dir), next_state)
    return next_state
