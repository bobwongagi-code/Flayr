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
_STAGE_LABELS = {
    "S1": "吸引注意",
    "S2": "产品引出",
    "S3": "功能与使用",
    "S4": "效果呈现",
    "S5": "信任放大",
    "S6": "促购引导",
}
DECISIONS = {"pending", "confirmed", "corrected", "not_applicable", "insufficient_evidence"}
RELATIONS = {"tie", "benchmark_better", "creator_better"}
GAPS = {"none", "small", "medium", "large"}
FINAL_DECISIONS = DECISIONS - {"pending"}
SUGGESTION_DECISIONS = {"pending", "approved", "edited"}
_SUGGESTION_KEY_RE = re.compile(r"^(improvements|candidate_experiments):(\d+)$")


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
        from .review_source import degraded_source_fingerprint

        try:
            source = degraded_source_fingerprint(root)
            analysis_stage_map(_read_json(_analysis_path(root)))
            return source
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            raise HumanReviewError(f"运行目录未通过人工审核来源完整性校验：{exc}") from exc
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
        "suggestion_reviews": {},
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
    suggestion_reviews = review.get("suggestion_reviews", {})
    if not isinstance(suggestion_reviews, Mapping):
        raise HumanReviewError("suggestion_reviews 必须是对象")
    for key, item in suggestion_reviews.items():
        if not isinstance(key, str) or not _SUGGESTION_KEY_RE.fullmatch(key):
            raise HumanReviewError(f"建议审核键非法：{key!r}")
        if not isinstance(item, Mapping):
            raise HumanReviewError(f"{key} 建议审核记录必须是对象")
        decision = _safe_text(item.get("decision"))
        action = _safe_text(item.get("action"))
        if decision not in SUGGESTION_DECISIONS:
            raise HumanReviewError(f"{key} 建议审核决定非法")
        if decision in {"approved", "edited"}:
            _normalize_action_sentence(action)
        elif action:
            raise HumanReviewError(f"{key} pending 建议不能携带已批准动作")
        if not _safe_text(item.get("timestamp")):
            raise HumanReviewError(f"{key} 建议审核必须记录时间")
        history = item.get("history")
        if not isinstance(history, list):
            raise HumanReviewError(f"{key} 建议审核 history 必须是数组")
        for event in history:
            if not isinstance(event, Mapping):
                raise HumanReviewError(f"{key} 建议审核 history 项必须是对象")
            event_decision = _safe_text(event.get("decision"))
            event_action = _safe_text(event.get("action"))
            if event_decision not in SUGGESTION_DECISIONS or not _safe_text(event.get("timestamp")):
                raise HumanReviewError(f"{key} 建议审核 history 项非法")
            if event_decision in {"approved", "edited"}:
                _normalize_action_sentence(event_action)
            elif event_action:
                raise HumanReviewError(f"{key} pending history 项不能携带动作")
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
    for key in review.get("suggestion_reviews", {}):
        match = _SUGGESTION_KEY_RE.fullmatch(str(key))
        if match is None:
            raise HumanReviewError(f"建议审核键非法：{key!r}")
        collection, raw_index = match.groups()
        items = analysis.get(collection)
        index = int(raw_index)
        if not isinstance(items, list) or index >= len(items) or not isinstance(items[index], Mapping):
            raise HumanReviewError(f"{key} 不对应当前 analysis 中的建议候选")
        item = items[index]
        candidate = _needs_confirmation_candidate(item)
        if candidate is None:
            raise HumanReviewError(f"{key} 没有可审核的动作")
        record = review["suggestion_reviews"][key]
        if record["decision"] in {"approved", "edited"}:
            target = _item_stage_target(item)
            changed_codes = _changed_stage_codes(analysis, review)
            preexisting_pending = _safe_text(item.get("status")) == "needs_human_confirmation"
            if not preexisting_pending and (not changed_codes or target and target not in changed_codes):
                raise HumanReviewError(f"{key} 未被本轮阶段更正降级，不能批准交付")
            if record["decision"] == "approved" and record["action"] != _candidate_action_sentence(item):
                raise HumanReviewError(f"{key} approved 必须使用原候选首条动作句")


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
    invalidate_reviewed_reports(run_dir)
    _clear_suggestion_approvals(review)
    if was_approved:
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


def _clear_suggestion_approvals(review: dict[str, Any]) -> None:
    records = review.get("suggestion_reviews")
    if not isinstance(records, dict):
        return
    for record in records.values():
        if not isinstance(record, dict) or record.get("decision") not in {"approved", "edited"}:
            continue
        timestamp = _now()
        record["decision"] = "pending"
        record["action"] = None
        record["timestamp"] = timestamp
        record.setdefault("history", []).append(
            {
                "timestamp": timestamp,
                "decision": "pending",
                "action": None,
                "reason": "stage_edit",
            }
        )


def _stage_target(value: Any) -> str:
    matches = re.findall(r"(?<![A-Z0-9])S([1-6])(?![0-9])", _safe_text(value).upper())
    return f"S{matches[0]}" if len(set(matches)) == 1 else ""


def _item_stage_target(item: Mapping[str, Any]) -> str:
    targets: list[str] = []
    for key in ("target_stage", "targetStage", "stage", "stage_code"):
        value = item.get(key)
        if not _safe_text(value):
            continue
        target = _stage_target(value)
        if not target:
            return ""
        targets.append(target)
    if not targets or len(set(targets)) != 1:
        return ""
    return targets[0]


def _stage_review_changed(decision: Mapping[str, Any], model_values: Mapping[str, Any]) -> bool:
    state = _safe_text(decision.get("decision"))
    if state == "corrected":
        return (
            decision.get("relation") != model_values.get("relation")
            or decision.get("gap") != model_values.get("gap")
            or _safe_text(decision.get("note")) != _safe_text(model_values.get("reason"))
        )
    if state == "not_applicable":
        return not bool(model_values.get("not_applicable"))
    if state == "insufficient_evidence":
        # A missing model conclusion can reflect technical unavailability, not
        # a human-verified lack of evidence. The sidecar has no stronger proof.
        return True
    return False


def _review_summary(review: Mapping[str, Any], *, changed: bool) -> dict[str, str]:
    gap_labels = {"none": "无明显差距", "small": "小差距", "medium": "中等差距", "large": "明显差距"}
    relation_labels = {
        "tie": "双方相近",
        "benchmark_better": "标杆更强",
        "creator_better": "达人更强",
    }
    if not changed:
        parts: list[str] = []
        for code in STAGE_CODES:
            item = review["stages"][code]
            decision = _safe_text(item.get("decision"))
            if decision == "not_applicable":
                text = "本阶段未纳入比较"
            elif decision == "insufficient_evidence":
                text = "证据不足，无法比较"
            else:
                relation = _normalize_relation(item.get("relation"))
                gap = _normalize_gap(item.get("gap"))
                text = f"{relation_labels.get(relation, '待确认')}，{gap_labels.get(gap, '待确认')}"
            note = _safe_text(item.get("note"))
            if note:
                text += f"（人工备注：{note}）"
            parts.append(f"{code} {text}")
        verdict = "人工逐阶段结论：" + "；".join(parts)
    else:
        gap_labels = {"small": "小差距", "medium": "中等差距", "large": "明显差距"}
        ties: list[str] = []
        directional: dict[tuple[str, str], list[str]] = {}
        not_applicable: list[str] = []
        insufficient: list[str] = []
        for code in STAGE_CODES:
            item = review["stages"][code]
            decision = _safe_text(item.get("decision"))
            if decision == "not_applicable":
                not_applicable.append(_STAGE_LABELS[code])
            elif decision == "insufficient_evidence":
                insufficient.append(_STAGE_LABELS[code])
            else:
                relation = _normalize_relation(item.get("relation"))
                gap = _normalize_gap(item.get("gap"))
                if relation == "tie" and gap == "none":
                    ties.append(_STAGE_LABELS[code])
                elif relation in {"benchmark_better", "creator_better"} and gap in gap_labels:
                    direction = "标杆更强" if relation == "benchmark_better" else "达人更强"
                    directional.setdefault((direction, gap), []).append(_STAGE_LABELS[code])

        parts = []
        if ties:
            parts.append(f"{'、'.join(ties)}双方相近")
        for (direction, gap), stages in directional.items():
            parts.append(f"{'、'.join(stages)}：{direction}（{gap_labels[gap]}）")
        if not_applicable:
            parts.append(f"{'、'.join(not_applicable)}：未纳入比较")
        if insufficient:
            parts.append(f"{'、'.join(insufficient)}：证据不足，无法比较")
        verdict = "；".join(parts)
    return {
        "verdict": verdict,
        "detail": (
            "阶段结论已确认；原商业总结未随确认更新，已停用；原有建议并未随阶段确认自动获批，待确认候选需单独核实。"
            if changed
            else "阶段结论已确认，保留原稿；未逐条审核建议。"
        ),
    }


_PERFORMANCE_FORECAST_RE = re.compile(
    r"(?ix)(?:"
    r"(?:点击率|完播率|转化率|GMV|销量|CTR|CVR|completion\s+rate|conversion\s+rate|sales)"
    r".{0,24}(?:提升|提高|增加|增长|预计|预期|改善|翻倍|increase|improve|grow|boost|expect|project)"
    r".{0,16}(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:-|至|到|~|～)\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)?\s*(?:%|％|倍|x|×)"
    r"|"
    r"(?:提升|提高|增加|增长|预计|预期|改善|翻倍|increase|improve|grow|boost|expect|project)"
    r".{0,24}(?:点击率|完播率|转化率|GMV|销量|CTR|CVR|completion\s+rate|conversion\s+rate|sales)"
    r".{0,16}(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:-|至|到|~|～)\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)?\s*(?:%|％|倍|x|×)"
    r")"
)
_EXPLICIT_FORECAST_BOUNDARY_RE = re.compile(
    r"\s*(?:as\s+well\s+as|并且|并|同时|而且|以及|且|和|及|and|also)\s*"
    r"(?=(?:预计|预期|预估|估计|expect(?:ed)?|project(?:ed)?))",
    re.IGNORECASE,
)


def _split_explicit_forecast_clauses(value: str) -> list[str]:
    pieces: list[str] = []
    cursor = 0
    for boundary in _EXPLICIT_FORECAST_BOUNDARY_RE.finditer(value):
        if boundary.start() < cursor:
            continue
        tail = value[boundary.end() :]
        sentence_end = re.search(r"[。！？!?；;\n]", tail)
        candidate = tail[: sentence_end.start()] if sentence_end else tail
        if not _PERFORMANCE_FORECAST_RE.search(candidate):
            continue
        pieces.append(value[cursor : boundary.start()])
        cursor = boundary.end()
    pieces.append(value[cursor:])
    return pieces


def _remove_performance_forecast_sentences(value: Any) -> Any:
    if isinstance(value, str):
        clauses = _split_explicit_forecast_clauses(value)
        parts = [
            part
            for clause in clauses
            for part in re.split(r"(?<!\d)(?<=[，,。！？!?；;\n])(?!\d)", clause)
        ]
        removed = any(_PERFORMANCE_FORECAST_RE.search(part) for part in parts)
        retained = "".join(part for part in parts if not _PERFORMANCE_FORECAST_RE.search(part))
        if removed:
            retained = re.sub(r"[，,；;]+(?=[。！？!?]?\s*$)", "", retained)
        return retained.strip()
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _remove_performance_forecast_sentences(item)) not in (None, "", [])
        ]
    if isinstance(value, Mapping):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _remove_performance_forecast_sentences(item)) not in (None, "", [])
        }
    return copy.deepcopy(value)


def _has_action(item: Mapping[str, Any]) -> bool:
    def has_text(value: Any) -> bool:
        if isinstance(value, list):
            return any(has_text(entry) for entry in value)
        return bool(_safe_text(value))

    return any(has_text(item.get(key)) for key in ("actions", "action", "suggestion"))


def _needs_confirmation_candidate(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not _has_action(item):
        return None
    actions: dict[str, Any] = {}
    for key in ("actions", "action", "suggestion"):
        if key not in item:
            continue
        value = _remove_performance_forecast_sentences(item[key])
        if value not in (None, "", []):
            actions[key] = value
    if not any(_safe_text(value) or isinstance(value, list) and any(_safe_text(entry) for entry in value) for value in actions.values()):
        return None
    target = _item_stage_target(item)
    candidate: dict[str, Any] = {
        "title": f"{target} 动作候选（待人工确认）" if target else "动作候选（待人工确认）",
        "problem": "原建议尚未依据本轮人工结论重新核验。",
        "observation": "此动作仅保留为待确认候选，必要性、方向与预期效果尚未重新核实。",
        "suggestion_basis": "待人工确认阶段依据；不表示方向或商业效果已验证。",
        "status": "needs_human_confirmation",
    }
    experiment_id = _safe_text(item.get("experiment_id"))
    if experiment_id:
        candidate["experiment_id"] = experiment_id
    if target:
        candidate["target_stage"] = target
    candidate.update(actions)
    return candidate


def _first_action_sentence(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    cleaned = _remove_performance_forecast_sentences(text)
    if not isinstance(cleaned, str):
        return ""
    match = re.search(r"[。！？!?;；\n]", cleaned)
    return cleaned[: match.end()].strip() if match else cleaned.strip()


def _candidate_action_sentence(item: Mapping[str, Any]) -> str:
    candidate = _needs_confirmation_candidate(item)
    if candidate is None:
        return ""
    for key in ("action", "suggestion", "actions"):
        value = candidate.get(key)
        values = value if isinstance(value, list) else [value]
        for entry in values:
            sentence = _first_action_sentence(entry)
            if sentence:
                return sentence
    return ""


def _normalize_action_sentence(value: Any) -> str:
    """Validate and sanitize the one action sentence approved for delivery."""
    text = _safe_text(value)
    cleaned = _remove_performance_forecast_sentences(text)
    if not isinstance(cleaned, str) or not cleaned.strip():
        raise HumanReviewError("动作句不能为空或仅包含业绩预测")
    sentences = [
        part.strip()
        for part in re.split(r"[。！？!?;；\n]+|(?<!\d)\.(?!\d)", cleaned)
        if part.strip()
    ]
    if len(sentences) != 1:
        raise HumanReviewError("只能填写一句动作")
    return cleaned.strip()


def normalize_action_sentence(value: Any) -> str:
    return _normalize_action_sentence(value)


def _changed_stage_codes(analysis: Mapping[str, Any], review: Mapping[str, Any]) -> set[str]:
    stages = analysis_stage_map(analysis)
    return {
        code
        for code in STAGE_CODES
        if _stage_review_changed(review["stages"][code], model_stage_values(stages[code]))
    }


def reviewable_suggestion_actions(
    run_dir: Path,
    review: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return action candidates that stage review actually demoted."""
    root = run_dir.expanduser().resolve()
    current_review = dict(review or load_review(root))
    _assert_source_current(current_review, _source_fingerprint(root))
    analysis = load_analysis(root)
    _validate_review_shape(current_review)
    _validate_review_against_analysis(current_review, analysis)
    if review_status(current_review) != "approved":
        return []
    changed_codes = _changed_stage_codes(analysis, current_review)
    reviewed = current_review.get("suggestion_reviews", {})
    pending: list[dict[str, str]] = []
    for collection in ("improvements", "candidate_experiments"):
        items = analysis.get(collection)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            target = _item_stage_target(item)
            preexisting_pending = _safe_text(item.get("status")) == "needs_human_confirmation"
            if not preexisting_pending and not changed_codes:
                continue
            if target and target not in changed_codes and not preexisting_pending:
                continue
            candidate = _needs_confirmation_candidate(item)
            action = _candidate_action_sentence(item)
            if candidate is None or not action:
                continue
            key = f"{collection}:{index}"
            record = reviewed.get(key, {}) if isinstance(reviewed, Mapping) else {}
            current_decision = _safe_text(record.get("decision")) if isinstance(record, Mapping) else "pending"
            pending.append(
                {
                    "key": key,
                    "collection": collection,
                    "title": _safe_text(item.get("title") or item.get("name")) or f"{target or '未绑定'} 动作候选",
                    "target_stage": target,
                    "action": action,
                    "decision": current_decision or "pending",
                    "reviewed_action": _safe_text(record.get("action")) if isinstance(record, Mapping) else "",
                }
            )
    return pending


def update_suggestion_review(
    run_dir: Path,
    key: str,
    decision: str,
    *,
    action: str | None = None,
) -> bool:
    """Record a candidate action review; return whether a report projection changed."""
    if decision not in SUGGESTION_DECISIONS:
        raise HumanReviewError(f"未知建议审核决定：{decision}")
    review = load_review(run_dir)
    eligible = {item["key"]: item for item in reviewable_suggestion_actions(run_dir, review)}
    candidate = eligible.get(key)
    if candidate is None:
        raise HumanReviewError(f"{key} 不是本轮被降级的待确认动作")

    if decision == "approved":
        final_action = candidate["action"]
    elif decision == "edited":
        final_action = normalize_action_sentence(action)
    else:
        if _safe_text(action):
            raise HumanReviewError("pending 建议不能携带动作")
        final_action = ""

    records = review.setdefault("suggestion_reviews", {})
    previous = records.get(key)
    previous_decision = previous.get("decision") if isinstance(previous, Mapping) else None
    previous_action = _safe_text(previous.get("action")) if isinstance(previous, Mapping) else ""
    if previous_decision == decision and previous_action == final_action:
        return False

    timestamp = _now()
    record = dict(previous) if isinstance(previous, Mapping) else {}
    history = list(record.get("history") or [])
    record.update({"decision": decision, "action": final_action or None, "timestamp": timestamp})
    history.append({"timestamp": timestamp, "decision": decision, "action": final_action or None})
    record["history"] = history
    records[key] = record

    report_changed = decision in {"approved", "edited"} or previous_decision in {"approved", "edited"}
    if report_changed:
        invalidate_reviewed_reports(run_dir)
    save_review(run_dir, review)
    return report_changed


def _review_suggestions(
    items: Any,
    changed_codes: set[str],
    review: Mapping[str, Any],
    collection: str,
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    output: list[dict[str, Any]] = []
    decisions = review.get("suggestion_reviews")
    decisions = decisions if isinstance(decisions, Mapping) else {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        target = _item_stage_target(item)
        if not target or target in changed_codes or _safe_text(item.get("status")) == "needs_human_confirmation":
            candidate = _needs_confirmation_candidate(item)
            if candidate is not None:
                record = decisions.get(f"{collection}:{index}")
                if isinstance(record, Mapping) and record.get("decision") in {"approved", "edited"}:
                    final_action = _normalize_action_sentence(record.get("action"))
                    candidate["status"] = "human_approved_action"
                    candidate["title"] = (
                        f"{target} 人工确认的拍摄动作" if target else "人工确认的拍摄动作"
                    )
                    candidate["problem"] = "原模型的问题描述未经过人工确认。"
                    candidate["observation"] = "拍摄动作已人工确认；视频依据和商业效果仍需核实。"
                    candidate["suggestion_basis"] = "仅拍摄动作经人工确认，不证明标杆示范或效果。"
                    if collection == "improvements":
                        candidate["suggestion"] = final_action
                        candidate.pop("action", None)
                        candidate.pop("actions", None)
                    else:
                        candidate["action"] = final_action
                        candidate.pop("suggestion", None)
                        candidate.pop("actions", None)
                output.append(candidate)
        else:
            output.append(_remove_performance_forecast_sentences(dict(item)))
    return output


_CHANGED_STAGE_CONTEXT_FIELDS = (
    "comparison_reason",
    "reference_relevance",
    "reference_observation",
    "benchmark_reference",
    "communication_strategy",
    "communication_advice",
    "talking_point",
    "communication",
    "_communication",
    "confidence_note",
    "confidence_notes",
    "evidence_confidence",
    "retain",
    "creator_retain",
    "creator_retention",
)


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
    changed_codes: set[str] = set()
    for code in STAGE_CODES:
        stage = stage_map.get(code)
        if stage is None:
            raise HumanReviewError(f"analysis.json 缺少 {code}")
        decision = review["stages"][code]
        state = _safe_text(decision.get("decision"))
        model_values = model_stage_values(stage)
        changed = _stage_review_changed(decision, model_values)
        if changed:
            changed_codes.add(code)
        if not changed:
            continue
        for key in _CHANGED_STAGE_CONTEXT_FIELDS:
            stage.pop(key, None)
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
            stage["gap"] = decision.get("note")
            stage["gap_summary"] = [decision.get("note")]
            stage["judgment_reason"] = decision.get("note")
        elif state == "insufficient_evidence":
            stage["relation"] = None
            stage["model_gap_magnitude"] = None
            stage["severity"] = None
            stage["comparison_status"] = "not_comparable"
            stage["gap"] = decision.get("note")
            stage["gap_summary"] = [decision.get("note")]
            stage["judgment_reason"] = decision.get("note")
    delivery_changed = bool(changed_codes)
    if delivery_changed:
        for code in STAGE_CODES:
            stage = stage_map[code]
            decision = review["stages"][code]
            stage["review_decision"] = _safe_text(decision.get("decision"))
            stage["review_note"] = _safe_text(decision.get("note")) or None
            stage.pop("linked_experiment_id", None)
    projected["review_delivery"] = {
        "summary_status": "replaced_after_stage_review" if delivery_changed else "preserved_after_no_semantic_change",
        "changed_stages": [code for code in STAGE_CODES if code in changed_codes],
    }
    projected["review_summary"] = _review_summary(review, changed=delivery_changed)
    if delivery_changed:
        projected["improvements"] = _review_suggestions(
            projected.get("improvements"), changed_codes, review, "improvements"
        )
        for key in (
            "one_line_summary",
            "commercial_priority_summary",
            "global_diagnosis",
            "commercial_priorities",
            "key_conclusions",
            "holistic_assessment",
            "highlights",
            "creator_highlights",
            "content_intent",
            "creator_content_intent",
            "retained_points",
            "creator_retain",
        ):
            projected.pop(key, None)
        projected["one_line_verdict"] = projected["review_summary"]["verdict"]
        projected["executive_summary"] = projected["review_summary"]["detail"]
    candidates = projected.get("candidate_experiments")
    if isinstance(candidates, list) and (delivery_changed or any(
        isinstance(item, Mapping) and _safe_text(item.get("status")) == "needs_human_confirmation"
        for item in candidates
    )):
        projected["candidate_experiments"] = _review_suggestions(
            candidates, changed_codes, review, "candidate_experiments"
        )
        for item in projected["candidate_experiments"]:
            if item.get("status") == "human_approved_action":
                approved = copy.deepcopy(item)
                approved["suggestion"] = approved.pop("action", "")
                if approved["suggestion"] and approved not in projected.setdefault("improvements", []):
                    projected["improvements"].append(approved)
    projected["review_status"] = "approved"
    projected["review_mode"] = REVIEW_MODE
    if source.get("source_run_state") == "DEGRADED":
        projected["review_delivery"]["source_run_state"] = "DEGRADED"
        projected["review_delivery"]["model_completed"] = False
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
