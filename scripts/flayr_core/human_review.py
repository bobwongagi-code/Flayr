"""Single-user human confirmation for completed semantic analysis runs.

The review sidecar is deliberately separate from ``analysis.json``.  It is a
small, hash-bound projection layer: the model result remains the immutable
source, while a reviewer records only the final semantic decision for each
stage.  This module does not read ground truth and does not change pipeline
state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .run_manifest import SUCCESS_MANIFEST_NAME, validate_success_manifest
from .report import stage_skipped
from .utils import write_json


SIDECAR_NAME = "human_review.json"
REVIEWED_BD_REPORT_NAME = "bd_report.reviewed.html"
REVIEWED_CREATOR_REPORT_NAME = "creator_report.reviewed.html"
REVIEW_SCHEMA_VERSION = 1
REVIEW_MODE = "draft_visible"
STAGE_CODES = tuple(f"S{index}" for index in range(1, 7))
DECISIONS = {"pending", "confirmed", "corrected", "not_applicable", "insufficient_evidence"}
RELATIONS = {"tie", "benchmark_better", "creator_better"}
GAPS = {"none", "small", "medium", "large"}
FINAL_DECISIONS = DECISIONS - {"pending"}


class HumanReviewError(ValueError):
    """A review cannot be created, changed, or projected safely."""


class StaleHumanReviewError(HumanReviewError):
    """The reviewed source artifacts changed after the sidecar was created."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HumanReviewError(f"无法读取 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise HumanReviewError(f"JSON 必须是对象：{path}")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stage_code(value: Any, index: int = 0) -> str:
    text = str(value or "").upper()
    match = re.search(r"(?<![A-Z0-9])S([1-6])(?![0-9])", text)
    if match:
        return f"S{match.group(1)}"
    return f"S{index}" if index else ""


def _safe_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value or "").strip()


def _normalize_relation(value: Any) -> str | None:
    raw = _safe_text(value).lower()
    if raw in {"tie", "equivalent", "same", "none", "equal"}:
        return "tie"
    if raw in {"benchmark_better", "benchmark", "reference_better"}:
        return "benchmark_better"
    if raw in {"creator_better", "creator", "达人更强"}:
        return "creator_better"
    return None


def _normalize_gap(value: Any) -> str | None:
    raw = _safe_text(value).lower()
    return raw if raw in GAPS else None


def _stage_not_applicable(stage: Mapping[str, Any]) -> bool:
    # Keep the code-owned report gate as the single authority.  Provider/model
    # shaped analysis_status or comparison_status values are not enough to
    # turn a stage into a structural NA.
    gate = stage.get("stage_evidence_gate")
    gate_na = isinstance(gate, Mapping) and gate.get("status") == "not_applicable"
    comparison_na = _safe_text(stage.get("comparison_status")).lower() == "not_applicable"
    contract = stage.get("comparison_contract")
    contract_na = (
        isinstance(contract, Mapping)
        and contract.get("status") == "not_applicable"
        and contract.get("status_source") == "bilateral_stage1_facts"
    )
    if not (gate_na or comparison_na or contract_na):
        return False
    candidate = dict(stage)
    if contract_na and not comparison_na:
        # stage_skipped validates this code-owned contract through its normal
        # comparison-status path; do not bypass that authority with a local
        # interpretation.
        candidate["comparison_status"] = "not_applicable"
    skipped, _ = stage_skipped(candidate)
    return skipped


def model_stage_values(stage: Mapping[str, Any]) -> dict[str, Any]:
    """Return the comparable model axes without copying them into the sidecar."""
    relation = _normalize_relation(
        stage.get("relation", stage.get("comparison_relation", stage.get("model_relation")))
    )
    # ``severity`` is the final report-facing model conclusion.  The
    # model_gap_magnitude field is an intermediate value and may be ``none``
    # while the final severity is unavailable or blocked.  Only consume that
    # ``none`` fallback when the stage itself is complete.
    gap = _normalize_gap(stage.get("severity"))
    if gap is None:
        model_gap = _normalize_gap(stage.get("model_gap_magnitude", stage.get("gap_magnitude")))
        stage_status = _safe_text(stage.get("analysis_status", stage.get("stage_state"))).lower()
        if model_gap == "none" and stage_status in {"completed", "grounded"}:
            gap = "none"
    not_applicable = _stage_not_applicable(stage)
    inconsistent_axes = (
        (relation == "tie" and gap not in {None, "none"})
        or (relation in {"benchmark_better", "creator_better"} and gap == "none")
    )
    return {
        "relation": relation,
        "gap": gap,
        "not_applicable": not_applicable,
        "prediction_unavailable": not_applicable is False and (relation is None or gap is None or inconsistent_axes),
        "reason": _safe_text(
            stage.get("judgment_reason")
            or stage.get("comparison_reason")
            or stage.get("gap")
            or stage.get("gap_summary")
        ),
        "evidence_ids": [
            str(item)
            for key in ("creator_evidence_ids", "benchmark_evidence_ids")
            for item in (stage.get(key) if isinstance(stage.get(key), list) else [])
            if str(item).strip()
        ],
    }


def _analysis_path(run_dir: Path) -> Path:
    return run_dir.expanduser().resolve() / "analysis.json"


def _manifest_path(run_dir: Path) -> Path:
    return run_dir.expanduser().resolve() / SUCCESS_MANIFEST_NAME


def _source_fingerprint(run_dir: Path) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    if not validate_success_manifest(root):
        raise HumanReviewError("运行目录未通过 _SUCCESS.json 完整性校验，不能进入人工审核")
    analysis_path = _analysis_path(root)
    manifest_path = _manifest_path(root)
    analysis_hash = _sha256_file(analysis_path)
    manifest_hash = _sha256_file(manifest_path)
    return {
        "success_manifest_sha256": manifest_hash,
        "analysis_sha256": analysis_hash,
        "review_inputs": {
            SUCCESS_MANIFEST_NAME: manifest_hash,
            "analysis.json": analysis_hash,
        },
    }


def _empty_sidecar(source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "review_mode": REVIEW_MODE,
        "source": copy.deepcopy(dict(source)),
        "status": "pending",
        "stages": {
            code: {
                "decision": "pending",
                "relation": None,
                "gap": None,
                "note": None,
            }
            for code in STAGE_CODES
        },
        "history": [],
    }


def review_status(review: Mapping[str, Any]) -> str:
    stages = review.get("stages")
    if not isinstance(stages, Mapping):
        return "pending"
    return "approved" if all(stages.get(code, {}).get("decision") in FINAL_DECISIONS for code in STAGE_CODES) else "pending"


def _validate_stage_decision(
    decision: str,
    relation: str | None,
    gap: str | None,
    note: str | None,
    model_values: Mapping[str, Any],
) -> None:
    if decision not in DECISIONS:
        raise HumanReviewError(f"未知审核决定：{decision}")
    if decision == "pending":
        if relation is not None or gap is not None or note:
            raise HumanReviewError("pending 阶段不能携带人工结论")
        return
    if decision == "confirmed":
        if model_values.get("prediction_unavailable"):
            raise HumanReviewError("模型 prediction_unavailable 时不能 confirmed")
        if relation is None or gap is None:
            raise HumanReviewError("confirmed 必须有可比较的 relation/gap")
    if decision == "corrected":
        if relation not in RELATIONS or gap not in GAPS:
            raise HumanReviewError("corrected 必须同时提供合法 relation/gap")
    if decision in {"corrected", "not_applicable", "insufficient_evidence"} and not _safe_text(note):
        raise HumanReviewError(f"{decision} 必须填写简短 note")
    if decision == "not_applicable" or decision == "insufficient_evidence":
        if relation is not None or gap is not None:
            raise HumanReviewError(f"{decision} 不得输出硬 relation/gap")
    if relation not in RELATIONS | {None} or gap not in GAPS | {None}:
        raise HumanReviewError("relation/gap 不在允许值内")
    if relation == "tie" and gap not in {None, "none"}:
        raise HumanReviewError("tie 只能对应 none")
    if relation in {"benchmark_better", "creator_better"} and gap not in {"small", "medium", "large"}:
        raise HumanReviewError("有方向 relation 只能对应 small/medium/large")


def _validate_review_shape(review: Mapping[str, Any]) -> None:
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION or review.get("review_mode") != REVIEW_MODE:
        raise HumanReviewError("human_review.json 版本或 review_mode 不受支持")
    stages = review.get("stages")
    if not isinstance(stages, Mapping) or set(stages) != set(STAGE_CODES):
        raise HumanReviewError("审核 sidecar 必须完整包含 S1-S6")
    history = review.get("history")
    if not isinstance(history, list):
        raise HumanReviewError("history 必须是数组")
    for code in STAGE_CODES:
        item = stages[code]
        if not isinstance(item, Mapping):
            raise HumanReviewError(f"{code} 审核记录必须是对象")
        decision = _safe_text(item.get("decision"))
        relation = item.get("relation")
        gap = item.get("gap")
        if relation is not None and relation not in RELATIONS:
            raise HumanReviewError(f"{code} relation 非法")
        if gap is not None and gap not in GAPS:
            raise HumanReviewError(f"{code} gap 非法")
        _validate_stage_decision(decision, relation, gap, item.get("note"), model_values={"prediction_unavailable": False})
    if review.get("status") not in {None, "pending", "approved"}:
        raise HumanReviewError("审核状态非法")
    stored_status = review.get("status")
    if stored_status is not None and stored_status != review_status(review):
        raise HumanReviewError("审核 status 必须由六个阶段决定派生，不能手工绕过")


def _validate_review_against_analysis(review: Mapping[str, Any], analysis: Mapping[str, Any]) -> None:
    stages = analysis_stage_map(analysis)
    for code in STAGE_CODES:
        item = review["stages"][code]
        model_values = model_stage_values(stages[code])
        decision = _safe_text(item.get("decision"))
        relation = item.get("relation")
        gap = item.get("gap")
        _validate_stage_decision(decision, relation, gap, item.get("note"), model_values)
        if decision == "confirmed" and (relation != model_values.get("relation") or gap != model_values.get("gap")):
            raise HumanReviewError(f"{code} confirmed 必须保持模型原始 relation/gap")


def load_analysis(run_dir: Path) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    _source_fingerprint(root)
    return _read_json(_analysis_path(root))


def init_review(run_dir: Path) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    source = _source_fingerprint(root)
    path = root / SIDECAR_NAME
    analysis = _read_json(_analysis_path(root))
    analysis_stage_map(analysis)
    if path.exists():
        review = _read_json(path)
        _validate_review_shape(review)
        _assert_source_current(review, source)
        _validate_review_against_analysis(review, analysis)
        return review
    review = _empty_sidecar(source)
    _validate_review_against_analysis(review, analysis)
    write_json(path, review)
    return review


def load_review(run_dir: Path) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    source = _source_fingerprint(root)
    path = root / SIDECAR_NAME
    if not path.is_file():
        raise HumanReviewError(f"缺少 {SIDECAR_NAME}，请先初始化审核")
    review = _read_json(path)
    _validate_review_shape(review)
    _assert_source_current(review, source)
    _validate_review_against_analysis(review, _read_json(_analysis_path(root)))
    return review


def _assert_source_current(review: Mapping[str, Any], source: Mapping[str, Any]) -> None:
    if review.get("source") != dict(source):
        raise StaleHumanReviewError("analysis.json 或 _SUCCESS.json 已变化，旧人工审核已失效")


def save_review(run_dir: Path, review: Mapping[str, Any]) -> Path:
    root = run_dir.expanduser().resolve()
    source = _source_fingerprint(root)
    _validate_review_shape(review)
    _assert_source_current(review, source)
    _validate_review_against_analysis(review, _read_json(_analysis_path(root)))
    data = copy.deepcopy(dict(review))
    data["source"] = copy.deepcopy(dict(source))
    data["status"] = review_status(data)
    write_json(root / SIDECAR_NAME, data)
    return root / SIDECAR_NAME


def analysis_stage_map(
    analysis: Mapping[str, Any], *, copy_values: bool = True
) -> dict[str, dict[str, Any]]:
    """Return exactly one canonical stage object for each S1-S6 code."""
    raw_stages = analysis.get("stage_analysis")
    if not isinstance(raw_stages, list) or len(raw_stages) != len(STAGE_CODES):
        raise HumanReviewError("analysis.stage_analysis 必须恰好包含 S1-S6 六个阶段")
    stages: dict[str, dict[str, Any]] = {}
    for stage in raw_stages:
        if not isinstance(stage, Mapping):
            raise HumanReviewError("analysis.stage_analysis 中的阶段必须是对象")
        code = _stage_code(stage.get("stage"))
        if code not in STAGE_CODES:
            raise HumanReviewError(f"analysis.stage_analysis 包含无法识别的阶段：{stage.get('stage')!r}")
        if code in stages:
            raise HumanReviewError(f"analysis.stage_analysis 重复包含 {code}")
        stages[code] = dict(stage) if copy_values else stage  # type: ignore[assignment]
    if set(stages) != set(STAGE_CODES):
        missing = ", ".join(sorted(set(STAGE_CODES) - set(stages)))
        raise HumanReviewError(f"analysis.stage_analysis 缺少阶段：{missing}")
    return stages


def _stage_from_analysis(analysis: Mapping[str, Any], code: str) -> dict[str, Any]:
    return analysis_stage_map(analysis).get(code, {})


def priority_key(code: str, stage: Mapping[str, Any]) -> tuple[int, int]:
    values = model_stage_values(stage)
    if values["prediction_unavailable"]:
        return (0, int(code[1:]))
    if code == "S5":
        return (1, int(code[1:]))
    if values["relation"] == "tie" and values["gap"] == "none":
        return (2, int(code[1:]))
    return (3, int(code[1:]))


def update_stage(
    run_dir: Path,
    code: str,
    decision: str,
    *,
    relation: str | None = None,
    gap: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    if code not in STAGE_CODES:
        raise HumanReviewError(f"未知阶段：{code}")
    review = load_review(run_dir)
    analysis = load_analysis(run_dir)
    model_values = model_stage_values(_stage_from_analysis(analysis, code))
    if decision == "confirmed":
        relation = model_values["relation"]
        gap = model_values["gap"]
        note = None
    _validate_stage_decision(decision, relation, gap, note, model_values)
    previous = copy.deepcopy(review["stages"][code])
    current = {"decision": decision, "relation": relation, "gap": gap, "note": _safe_text(note) or None}
    if previous == current:
        return review
    was_approved = review_status(review) == "approved"
    if was_approved:
        invalidate_reviewed_reports(run_dir)
        # A post-approval edit begins a new review pass.  Reset the edited
        # stage first so status remains derived from stage decisions; the
        # reviewer applies the new decision on the next invocation.
        current = {"decision": "pending", "relation": None, "gap": None, "note": None}
    review["stages"][code] = current
    review.setdefault("history", []).append(
        {
            "sequence": len(review.get("history") or []) + 1,
            "timestamp": _now(),
            "stage": code,
            "before": previous,
            "after": copy.deepcopy(current),
            "note": current.get("note"),
        }
    )
    review["status"] = review_status(review)
    save_review(run_dir, review)
    return review


def invalidate_reviewed_reports(run_dir: Path) -> tuple[Path, ...]:
    """Remove approved projections after any subsequent semantic edit."""
    root = run_dir.expanduser().resolve()
    removed: list[Path] = []
    for name in (REVIEWED_BD_REPORT_NAME, REVIEWED_CREATOR_REPORT_NAME):
        path = root / name
        if path.exists():
            path.unlink()
            removed.append(path)
    return tuple(removed)


def _stage_target(value: Any) -> str:
    return _stage_code(value)


def _project_summary(stages: list[Mapping[str, Any]]) -> dict[str, str]:
    gap_labels = {"none": "无明显差距", "small": "小差距", "medium": "中等差距", "large": "明显差距"}
    relation_labels = {
        "tie": "双方相近",
        "benchmark_better": "标杆更强",
        "creator_better": "达人更强",
    }
    parts: list[str] = []
    for index, stage in enumerate(stages, start=1):
        code = _stage_code(stage.get("stage"), index)
        decision = _safe_text(stage.get("review_decision"))
        if decision == "not_applicable":
            text = "未涉及"
        elif decision == "insufficient_evidence":
            text = "证据不足，无法比较"
        else:
            relation = _normalize_relation(stage.get("relation"))
            gap = _normalize_gap(stage.get("severity"))
            if gap is None and decision in {"confirmed", "corrected"}:
                if _normalize_gap(stage.get("model_gap_magnitude")) == "none":
                    gap = "none"
            if gap is None and _safe_text(stage.get("analysis_status", stage.get("stage_state"))).lower() in {"completed", "grounded"}:
                if _normalize_gap(stage.get("model_gap_magnitude")) == "none":
                    gap = "none"
            text = f"{relation_labels.get(relation, '待确认')}，{gap_labels.get(gap, '待确认')}"
        parts.append(f"{code}{text}")
    return {
        "verdict": "人工复核已完成：" + "；".join(parts),
        "detail": "以上结论来自逐阶段人工确认；原始模型分析与证据引用保留在本运行目录中。",
    }


def project_reviewed_analysis(run_dir: Path, review: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    review = dict(review or load_review(root))
    source = _source_fingerprint(root)
    _validate_review_shape(review)
    _assert_source_current(review, source)
    _validate_review_against_analysis(review, _read_json(_analysis_path(root)))
    if review_status(review) != "approved":
        raise HumanReviewError("六个阶段尚未完成审核，不能生成 reviewed 报告")
    analysis = load_analysis(root)
    projected = copy.deepcopy(analysis)
    stage_map = analysis_stage_map(projected, copy_values=False)
    removed_codes: set[str] = set()
    preserved_review_gaps: dict[str, str] = {}
    for code in STAGE_CODES:
        stage = stage_map.get(code)
        if stage is None:
            raise HumanReviewError(f"analysis.json 缺少 {code}")
        decision = review["stages"][code]
        state = _safe_text(decision.get("decision"))
        model_values = model_stage_values(stage)
        model_relation = model_values["relation"]
        current_relation = decision.get("relation")
        if state in {"not_applicable", "insufficient_evidence"}:
            removed_codes.add(code)
        elif state == "corrected":
            # A recommendation remains valid only when the original and
            # reviewed direction both say the benchmark is stronger.  A gap
            # correction alone is safe; any direction change invalidates the
            # old recommendation rather than letting it survive by stage ID.
            if model_relation == "benchmark_better" and current_relation == "benchmark_better":
                preserved_review_gaps[code] = str(decision.get("gap"))
            else:
                removed_codes.add(code)
        elif state == "confirmed" and model_relation == "benchmark_better":
            model_gap = model_values.get("gap")
            if model_gap in GAPS:
                preserved_review_gaps[code] = model_gap
        stage["review_decision"] = state
        stage["review_note"] = decision.get("note")
        if state == "corrected":
            stage["relation"] = decision.get("relation")
            stage["model_gap_magnitude"] = decision.get("gap")
            stage["severity"] = decision.get("gap")
            stage["gap"] = decision.get("note")
            stage["gap_summary"] = [decision.get("note")]
            stage["judgment_reason"] = decision.get("note")
        elif state == "not_applicable":
            stage["relation"] = None
            stage["model_gap_magnitude"] = None
            stage["severity"] = None
            stage["comparison_status"] = "not_applicable"
            stage["gap"] = "未涉及"
            stage["gap_summary"] = ["未涉及"]
            stage["judgment_reason"] = "未涉及"
        elif state == "insufficient_evidence":
            stage["relation"] = None
            stage["model_gap_magnitude"] = None
            stage["severity"] = None
            stage["comparison_status"] = "not_comparable"
            stage["gap"] = "证据不足，无法比较"
            stage["gap_summary"] = ["证据不足，无法比较"]
            stage["judgment_reason"] = "证据不足，无法比较"
        else:
            stage["review_decision"] = "confirmed"

    def keep_item(item: Any) -> bool:
        if not isinstance(item, Mapping):
            return False
        target = _stage_target(
            item.get("target_stage")
            or item.get("targetStage")
            or item.get("stage")
            or item.get("stage_code")
        )
        # Recommendations/experiments are review-derived output.  They may
        # survive only for an explicit S1-S6 target whose original and final
        # relation both establish benchmark_better.
        return target in preserved_review_gaps and target not in removed_codes

    def reorder_reviewed(items: Any) -> list[Any]:
        original = [item for item in items or [] if keep_item(item)] if isinstance(items, list) else []
        indexed = list(enumerate(original))
        gap_rank = {"large": 0, "medium": 1, "small": 2, "none": 3}

        def sort_key(pair: tuple[int, Any]) -> tuple[int, int, int]:
            index, item = pair
            target = (
                _stage_target(
                    item.get("target_stage")
                    or item.get("targetStage")
                    or item.get("stage")
                    or item.get("stage_code")
                )
                if isinstance(item, Mapping)
                else ""
            )
            if target in preserved_review_gaps:
                return (0, gap_rank.get(preserved_review_gaps[target], 9), index)
            return (1, 9, index)

        return [item for _, item in sorted(indexed, key=sort_key)]

    projected["improvements"] = reorder_reviewed(projected.get("improvements"))
    if isinstance(projected.get("candidate_experiments"), list):
        projected["candidate_experiments"] = reorder_reviewed(projected["candidate_experiments"])
    projected["review_status"] = "approved"
    projected["review_mode"] = REVIEW_MODE
    projected["review_summary"] = _project_summary(list(stage_map.values()))
    return projected


def write_reviewed_reports(run_dir: Path) -> tuple[Path, Path]:
    """Render the two independent approved projections without changing analysis.json."""
    root = run_dir.expanduser().resolve()
    invalidate_reviewed_reports(root)
    from .bd_report import write_bd_report
    from .creator_report import write_creator_report

    try:
        projected = project_reviewed_analysis(root)
        bd_path = write_bd_report(
            root,
            projected,
            output_name=REVIEWED_BD_REPORT_NAME,
            review_status="approved",
        )
        creator_path = write_creator_report(
            root,
            projected,
            output_name=REVIEWED_CREATOR_REPORT_NAME,
            review_status="approved",
        )
        return bd_path, creator_path
    except Exception:
        # A failed second render must not leave a misleading half-approved
        # report pair behind.
        invalidate_reviewed_reports(root)
        raise
