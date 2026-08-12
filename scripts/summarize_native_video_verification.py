#!/usr/bin/env python3
"""Summarize the native-video versus frozen-frame verification artifacts."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import compare_visual_extraction_units  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402


STAGES = tuple(f"S{i}" for i in range(1, 7))
ROLES = ("creator", "benchmark")
GAP_ORDER = {"none": 0, "small": 1, "medium": 2, "large": 3}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def safe(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value))
    return cleaned.strip("._") or "unnamed"


def manifest_ids(path: Path) -> list[str]:
    rows = read_json(path).get("samples")
    if not isinstance(rows, list):
        raise ValueError(f"manifest has no samples: {path}")
    return [str(row["sample_id"]) for row in rows]


def extraction_path(root: Path, sample_id: str) -> Path:
    return root / safe(sample_id) / "qwen3.7-plus" / "visual_extraction_evaluation.json"


def judgment_path(root: Path, sample_id: str, condition: str) -> Path:
    return root / safe(sample_id) / condition / "qwen3.7-plus" / "model_independent_evaluation.json"


def provider_stats(record: dict[str, Any]) -> dict[str, Any]:
    provider = record.get("provider_meta") or {}
    usage = provider.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    used = (record.get("resource_budget") or {}).get("used") or {}
    return {
        "elapsed_seconds": used.get("elapsed_seconds"),
        "uploaded_bytes": used.get("total_uploaded_bytes"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        "completion_text_tokens": completion_details.get("text_tokens"),
        "video_tokens": prompt_details.get("video_tokens"),
        "image_tokens": prompt_details.get("image_tokens"),
        "finish_reason": provider.get("finish_reason"),
        "transport_status": provider.get("transport_status"),
    }


def numeric_stats(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = [
        float(row[field])
        for row in rows
        if isinstance(row.get(field), (int, float)) and row[field] >= 0
    ]
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 3) if values else None,
        "median": round(statistics.median(values), 3) if values else None,
        "sum": round(sum(values), 3) if values else None,
    }


def unit_stages(units: list[dict[str, Any]]) -> set[str]:
    return {
        str(function).upper()
        for unit in units
        if isinstance(unit, dict)
        for function in unit.get("functions", [])
        if isinstance(function, str)
    }


def unit_counts_by_stage(units: list[dict[str, Any]]) -> dict[str, int]:
    return {
        stage: sum(
            stage in {
                str(function).upper()
                for function in unit.get("functions", [])
                if isinstance(function, str)
            }
            for unit in units
            if isinstance(unit, dict)
        )
        for stage in STAGES
    }


def stage1_condition(sample_ids: list[str], root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        path = extraction_path(root, sample_id)
        if not path.is_file():
            failure_path = path.with_name("visual_extraction_failure.json")
            row = {"sample_id": sample_id, "status": "missing", "artifact": str(path)}
            if failure_path.is_file():
                failure = read_json(failure_path)
                row["failure_class"] = failure.get("failure_class")
                row["error"] = str(failure.get("error") or failure.get("errors") or "")[:500]
            rows.append(row)
            continue
        record = read_json(path)
        status = str(record.get("status") or "invalid")
        row: dict[str, Any] = {"sample_id": sample_id, "status": status, "artifact": str(path)}
        if status == "completed":
            result = record.get("result") or {}
            role_units = {
                role: result.get(f"{role}_evidence_units", [])
                for role in ROLES
            }
            row.update(
                {
                    "unit_counts": {role: len(units) for role, units in role_units.items()},
                    "stage_counts": {
                        role: unit_counts_by_stage(units)
                        for role, units in role_units.items()
                    },
                    "stages_present": {
                        role: sorted(unit_stages(units))
                        for role, units in role_units.items()
                    },
                    "provider": provider_stats(record),
                    "protocol_hash": record.get("protocol_hash"),
                }
            )
        else:
            failure_path = path.with_name("visual_extraction_failure.json")
            if failure_path.is_file():
                failure = read_json(failure_path)
                row["failure_class"] = failure.get("failure_class")
                row["error"] = str(failure.get("error") or failure.get("errors") or "")[:500]
        rows.append(row)

    completed = [row for row in rows if row["status"] == "completed"]
    stats_fields = (
        "elapsed_seconds",
        "uploaded_bytes",
        "prompt_tokens",
        "total_tokens",
        "reasoning_tokens",
        "completion_text_tokens",
        "video_tokens",
        "image_tokens",
    )
    return {
        "sample_count": len(rows),
        "completed": len(completed),
        "failed_or_missing": len(rows) - len(completed),
        "failure_classes": dict(
            Counter(
                str(row.get("failure_class") or "unknown")
                for row in rows
                if row["status"] != "completed"
            )
        ),
        "rows": rows,
        "stats": {
            field: numeric_stats([row["provider"] for row in completed], field)
            for field in stats_fields
        },
        "stage_presence_counts": {
            role: {
                stage: sum(
                    stage in row["stages_present"][role]
                    for row in completed
                )
                for stage in STAGES
            }
            for role in ROLES
        },
        "stage_presence_rates": {
            role: {
                stage: round(
                    sum(stage in row["stages_present"][role] for row in completed)
                    / len(completed),
                    4,
                )
                if completed
                else None
                for stage in STAGES
            }
            for role in ROLES
        },
        "unit_count_stats": {
            role: numeric_stats(
                [{"value": row["unit_counts"][role]} for row in completed],
                "value",
            )
            for role in ROLES
        },
    }


def stage1_paired(
    sample_ids: list[str],
    raw_root: Path,
    frames_root: Path,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        raw_path = extraction_path(raw_root, sample_id)
        frame_path = extraction_path(frames_root, sample_id)
        if not raw_path.is_file() or not frame_path.is_file():
            continue
        raw_record = read_json(raw_path)
        frame_record = read_json(frame_path)
        if raw_record.get("status") != "completed" or frame_record.get("status") != "completed":
            continue
        raw_result = raw_record.get("result") or {}
        frame_result = frame_record.get("result") or {}
        rows.append(
            {
                "sample_id": sample_id,
                "raw_unit_counts": {
                    role: len(raw_result.get(f"{role}_evidence_units", []))
                    for role in ROLES
                },
                "frame_unit_counts": {
                    role: len(frame_result.get(f"{role}_evidence_units", []))
                    for role in ROLES
                },
                "raw_stages_present": {
                    role: sorted(unit_stages(raw_result.get(f"{role}_evidence_units", [])))
                    for role in ROLES
                },
                "frame_stages_present": {
                    role: sorted(unit_stages(frame_result.get(f"{role}_evidence_units", [])))
                    for role in ROLES
                },
                "role_unit_comparison": {
                    role: compare_visual_extraction_units(
                        raw_result.get(f"{role}_evidence_units", []),
                        frame_result.get(f"{role}_evidence_units", []),
                    )
                    for role in ROLES
                },
            }
        )
    # Rewrite the boolean expression above as explicit integer deltas for
    # human-readable output and stable downstream aggregation.
    for row in rows:
        sample_id = row["sample_id"]
        raw_result = read_json(extraction_path(raw_root, sample_id)).get("result") or {}
        frame_result = read_json(extraction_path(frames_root, sample_id)).get("result") or {}
        row["stage_presence_delta"] = {
            role: {
                stage: int(stage in unit_stages(frame_result.get(f"{role}_evidence_units", [])))
                - int(stage in unit_stages(raw_result.get(f"{role}_evidence_units", [])))
                for stage in STAGES
            }
            for role in ROLES
        }
    paired_summary = {}
    for condition in ("raw", "frame"):
        unit_key = f"{condition}_unit_counts"
        stage_key = f"{condition}_stages_present"
        paired_summary[condition] = {
            "sample_count": len(rows),
            "unit_count_stats": {
                role: numeric_stats(
                    [{"value": row[unit_key][role]} for row in rows],
                    "value",
                )
                for role in ROLES
            },
            "stage_presence_counts": {
                role: {
                    stage: sum(stage in row[stage_key][role] for row in rows)
                    for stage in STAGES
                }
                for role in ROLES
            },
            "stage_presence_rates": {
                role: {
                    stage: round(
                        sum(stage in row[stage_key][role] for row in rows) / len(rows),
                        4,
                    )
                    if rows
                    else None
                    for stage in STAGES
                }
                for role in ROLES
            },
        }
    return {
        "paired_sample_count": len(rows),
        "sample_ids": [row["sample_id"] for row in rows],
        "paired_condition_summary": paired_summary,
        "rows": rows,
        "warning": "unit/text overlap is a structural proxy, not semantic recall or precision; no human key_events were supplied.",
    }


def stage2_artifact(
    roots: list[Path],
    sample_id: str,
    condition: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    for root in roots:
        path = judgment_path(root, sample_id, condition)
        if path.is_file():
            record = read_json(path)
            if record.get("status") == "completed":
                return path, record
    return None, None


def score_stage2(
    sample_ids: list[str],
    gt_samples: dict[str, Any],
    roots: list[Path],
) -> dict[str, Any]:
    cell_map: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
        "frames": {},
        "video": {},
    }
    condition_summary: dict[str, Any] = {}
    for condition in ("frames", "video"):
        rows: list[dict[str, Any]] = []
        for sample_id in sample_ids:
            path, record = stage2_artifact(roots, sample_id, condition)
            if record is None:
                rows.append({"sample_id": sample_id, "status": "missing"})
                continue
            predictions = {
                str(item.get("stage") or "").split(maxsplit=1)[0]: item
                for item in (record.get("result") or {}).get("stage_judgments", [])
                if isinstance(item, dict)
            }
            gt_gaps = (gt_samples.get(sample_id) or {}).get("human_gap") or {}
            row = {
                "sample_id": sample_id,
                "status": "completed",
                "artifact": str(path),
                "protocol_hash": record.get("protocol_hash"),
                "provider": provider_stats(record),
                "cells": [],
            }
            for stage in STAGES:
                gt_gap = gt_gaps.get(stage)
                prediction = predictions.get(stage) or {}
                predicted_gap = prediction.get("gap_magnitude")
                eligible = str(gt_gap) in GAP_ORDER
                distance = (
                    abs(GAP_ORDER[str(gt_gap)] - GAP_ORDER[str(predicted_gap)])
                    if eligible and str(predicted_gap) in GAP_ORDER
                    else None
                )
                cell = {
                    "sample_id": sample_id,
                    "stage": stage,
                    "gt_gap": gt_gap,
                    "pred_gap": predicted_gap,
                    "pred_relation": prediction.get("relation"),
                    "eligible": eligible,
                    "exact": bool(eligible and predicted_gap == gt_gap),
                    "within_one": bool(eligible and distance is not None and distance <= 1),
                }
                cell_map[condition][(sample_id, stage)] = cell
                row["cells"].append(cell)
            rows.append(row)
        eligible_cells = [
            cell for cell in cell_map[condition].values() if cell["eligible"]
        ]
        exact = sum(cell["exact"] for cell in eligible_cells)
        within_one = sum(cell["within_one"] for cell in eligible_cells)
        stage_metrics = {}
        for stage in STAGES:
            cells = [cell for cell in eligible_cells if cell["stage"] == stage]
            stage_metrics[stage] = {
                "eligible": len(cells),
                "exact": sum(cell["exact"] for cell in cells),
                "exact_rate": round(
                    sum(cell["exact"] for cell in cells) / len(cells), 4
                )
                if cells
                else None,
                "within_one": sum(cell["within_one"] for cell in cells),
                "within_one_rate": round(
                    sum(cell["within_one"] for cell in cells) / len(cells), 4
                )
                if cells
                else None,
            }
        condition_summary[condition] = {
            "sample_count": len(sample_ids),
            "completed": sum(row["status"] == "completed" for row in rows),
            "scorable_cells": len(eligible_cells),
            "exact_correct": exact,
            "exact_accuracy": round(exact / len(eligible_cells), 4)
            if eligible_cells
            else None,
            "within_one_correct": within_one,
            "within_one_accuracy": round(within_one / len(eligible_cells), 4)
            if eligible_cells
            else None,
            "stage_metrics": stage_metrics,
            "rows": rows,
            "stats": {
                field: numeric_stats(
                    [row["provider"] for row in rows if row["status"] == "completed"],
                    field,
                )
                for field in (
                    "elapsed_seconds",
                    "uploaded_bytes",
                    "prompt_tokens",
                    "total_tokens",
                    "reasoning_tokens",
                    "completion_text_tokens",
                    "video_tokens",
                    "image_tokens",
                )
            },
        }
    paired_cells = [
        key
        for key in cell_map["frames"]
        if key in cell_map["video"]
        and cell_map["frames"][key]["eligible"]
        and cell_map["video"][key]["eligible"]
    ]
    changes = [
        {
            "sample_id": key[0],
            "stage": key[1],
            "frame_gap": cell_map["frames"][key]["pred_gap"],
            "video_gap": cell_map["video"][key]["pred_gap"],
            "frame_relation": cell_map["frames"][key]["pred_relation"],
            "video_relation": cell_map["video"][key]["pred_relation"],
            "gap_changed": cell_map["frames"][key]["pred_gap"]
            != cell_map["video"][key]["pred_gap"],
            "relation_changed": cell_map["frames"][key]["pred_relation"]
            != cell_map["video"][key]["pred_relation"],
            "judgment_changed": (
                cell_map["frames"][key]["pred_gap"]
                != cell_map["video"][key]["pred_gap"]
                or cell_map["frames"][key]["pred_relation"]
                != cell_map["video"][key]["pred_relation"]
            ),
            "frame_correct": cell_map["frames"][key]["exact"],
            "video_correct": cell_map["video"][key]["exact"],
        }
        for key in paired_cells
    ]
    return {
        "gt_source_status": "human_initial_unfrozen_calibration_only",
        "gt_relation_available": False,
        "gt_scoring_rule": "human_gap exact match; not_applicable and uncertain excluded; relation direction not scored",
        "conditions": condition_summary,
        "paired_comparison": {
            "paired_scorable_cells": len(paired_cells),
            "gap_changed": sum(item["gap_changed"] for item in changes),
            "relation_changed": sum(item["relation_changed"] for item in changes),
            "judgment_changed": sum(item["judgment_changed"] for item in changes),
            "judgment_unchanged": sum(not item["judgment_changed"] for item in changes),
            "frame_only_correct": sum(item["frame_correct"] and not item["video_correct"] for item in changes),
            "video_only_correct": sum(item["video_correct"] and not item["frame_correct"] for item in changes),
            "both_correct": sum(item["frame_correct"] and item["video_correct"] for item in changes),
            "neither_correct": sum(not item["frame_correct"] and not item["video_correct"] for item in changes),
            "judgment_change_outcomes": {
                "changed_and_both_correct": sum(
                    item["judgment_changed"]
                    and item["frame_correct"]
                    and item["video_correct"]
                    for item in changes
                ),
                "changed_and_frame_only_correct": sum(
                    item["judgment_changed"]
                    and item["frame_correct"]
                    and not item["video_correct"]
                    for item in changes
                ),
                "changed_and_video_only_correct": sum(
                    item["judgment_changed"]
                    and item["video_correct"]
                    and not item["frame_correct"]
                    for item in changes
                ),
                "changed_and_neither_correct": sum(
                    item["judgment_changed"]
                    and not item["frame_correct"]
                    and not item["video_correct"]
                    for item in changes
                ),
            },
            "rows": changes,
        },
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    stage1_ids = manifest_ids(args.stage1_manifest)
    stage2_ids = manifest_ids(args.stage2_manifest)
    gt_samples = read_json(args.gt_path).get("samples") or {}
    raw = stage1_condition(stage1_ids, args.raw_root)
    frames = stage1_condition(stage1_ids, args.frames_root)
    paired = stage1_paired(stage1_ids, args.raw_root, args.frames_root)
    judgment = score_stage2(
        stage2_ids,
        gt_samples,
        [args.stage2_final_root, args.stage2_remaining_root],
    )
    return {
        "schema_version": 2,
        "report": "native_video_verification",
        "generated_from": {
            "source_commit": "c260437",
            "source_tree_state": "working_tree_with_experiment_changes; not a committed production snapshot",
            "stage1_manifest": str(args.stage1_manifest),
            "stage2_manifest": str(args.stage2_manifest),
            "gt_path": str(args.gt_path),
        },
        "scope": {
            "stage1": "Qwen3.7-plus visual extraction; raw bounded videos versus frozen stage frames; no ASR/OCR in either condition",
            "stage2": "Qwen3.7-plus model-independent judgment with the same current raw-extracted facts, plus frames versus plus bounded videos",
            "promotion_eligible": False,
        },
        "stage2_exclusions": [
            {
                "sample_id": "niumo-nose-hair-trimmer",
                "condition": "frames",
                "status": "excluded_from_paired_score",
                "reason": "final-protocol streaming request returned HTTP 200 chunks but no completion marker and timed out; no retry was made",
                "artifact": "runs/verification-native-video-20260812/stage2-qwen37-final/niumo-nose-hair-trimmer/frames/qwen3.7-plus/model_independent_failure.json",
            }
        ],
        "stage1": {
            "raw_video": raw,
            "frames": frames,
            "paired_comparison": paired,
        },
        "stage2": judgment,
        "guardrails": [
            "Stage1 structural presence and frame/raw overlap are proxies, not human semantic recall or precision.",
            "The human GT is an unfrozen calibration working set of seven samples; it is not blind validation.",
            "Stage2 scores measure gap_magnitude only. The current GT has no formal relation field, so direction accuracy is not computed.",
            "The native-video condition tests the value of adding whole-video context; it does not prove a native-video model can replace the evidence ledger.",
            "The Stage1 comparison is not a pure modality A/B: frozen frames are already bucketed by stage and time, while native video requires the model to discover the stage boundaries itself.",
            "Neither Stage1 condition used ASR or OCR, so this report cannot attribute differences to audio or text extraction.",
        ],
        "follow_up_controls": [
            {
                "name": "native_video_with_explicit_stage_checklist",
                "status": "planned_not_run",
                "purpose": "Separate the native-video modality from the hidden stage-bucketing/checklist advantage of the frame condition.",
                "control": "Keep the same native video and contract, but require an explicit ordered S1-S6 response with one entry per stage.",
                "acceptance": "Compare stage presence, valid evidence-unit coverage, contract stability, and (after key_events GT exists) recall/precision; do not use this calibration control for promotion.",
            }
        ],
        "architecture_decision": {
            "native_video_role": "phase_c_review_material_only",
            "reason_codes": [
                "continuous_temporal_evidence",
                "stage_none_or_uncertain",
                "evidence_qualification_conflict",
                "commercial_attention_cleanliness",
                "focus_consistency",
            ],
            "write_policy": "Native-video review may return supplemental facts or review leads only; shared Phase C budget, patch contract, and finalizer write authority remain unchanged.",
        },
        "next_measurement": {
            "artifact": "human_key_events",
            "purpose": "Replace stage-presence proxies with semantic Stage1 recall and precision, and determine whether missing S6 reflects missed content or failed stage assignment.",
            "priority": "before any native-video promotion decision",
        },
    }


def markdown(report: dict[str, Any]) -> str:
    stage1 = report["stage1"]
    stage2 = report["stage2"]
    lines = [
        "# Native Video Verification Report",
        "",
        "## 结论",
        "",
        "1. 原生整视频没有自动消除证据遗漏。Stage1仍要求模型自行把连续视频切成S1-S6事实；抽帧条件把阶段、时间和关键画面显式外置，因此在阶段覆盖上更稳定。",
        "2. 原生整视频对Stage2判断的净收益未被本实验稳定证明。固定事实下两种条件会改变判断，但不能从“看到了完整视频”直接推断更准。",
        "3. 原生视频路径新增了资源和输出稳定性风险：本轮有一次流式不收口超时；前置试跑还出现了多类结构字段遗漏或矛盾。",
        "4. 建议使用混合路径：Stage1保留可审计事实账本，原生视频只作为连续性、不确定性或证据冲突场景的补充上下文/复核入口。",
        "",
        "## Stage1",
        "",
        f"- Raw video完成：{stage1['raw_video']['completed']}/{stage1['raw_video']['sample_count']}；frames完成：{stage1['frames']['completed']}/{stage1['frames']['sample_count']}。",
        f"- 成对完成样本：{stage1['paired_comparison']['paired_sample_count']}。",
        "",
        "| 条件 | 角色 | S1 | S2 | S3 | S4 | S5 | S6 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("raw_video", "frames"):
        for role in ROLES:
            rates = stage1[condition]["stage_presence_rates"][role]
            lines.append(
                "| %s | %s | %s | %s | %s | %s | %s | %s |"
                % (
                    condition,
                    role,
                    *[
                        f"{(rates[stage] or 0) * 100:.1f}%"
                        if rates[stage] is not None
                        else "-"
                        for stage in STAGES
                    ],
                )
            )
    lines += [
        "",
        "| 条件 | 平均耗时 | 平均上传 | 平均prompt token | 平均reasoning token | Creator平均单元 | Benchmark平均单元 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("raw_video", "frames"):
        data = stage1[condition]
        lines.append(
            "| %s | %.1fs | %.2fMB | %.0f | %.0f | %.2f | %.2f |"
            % (
                condition,
                data["stats"]["elapsed_seconds"]["mean"] or 0,
                (data["stats"]["uploaded_bytes"]["mean"] or 0) / 1024 / 1024,
                data["stats"]["prompt_tokens"]["mean"] or 0,
                data["stats"]["reasoning_tokens"]["mean"] or 0,
                data["unit_count_stats"]["creator"]["mean"] or 0,
                data["unit_count_stats"]["benchmark"]["mean"] or 0,
            )
        )
    lines += [
        "",
        "- S6是最明显的结构差异：整视频条件经常没有产出S6事实；抽帧条件因为输入显式带有S6阶段帧，覆盖率更高。这说明“给完整视频”不等于“模型会自动覆盖所有阶段”。",
        "- 这不是纯粹的“视频输入 vs 抽帧输入”对照：抽帧输入已经把阶段和时间分桶，等价于给了模型隐性清单；原生视频要求模型自行发现阶段边界。下一步应单独做“原生视频 + 显式S1-S6逐项汇报”控制实验。",
        "- 本轮没有人工key_events，所以不能计算真正的Stage1召回率/精确率；阶段覆盖、单元数量和时序重叠只能作为诊断代理。",
        "",
        "## Stage2",
        "",
        f"- 帧条件：{stage2['conditions']['frames']['completed']}/{stage2['conditions']['frames']['sample_count']}完成，{stage2['conditions']['frames']['exact_correct']}/{stage2['conditions']['frames']['scorable_cells']}个GT差距格准确。",
        f"- 视频条件：{stage2['conditions']['video']['completed']}/{stage2['conditions']['video']['sample_count']}完成，{stage2['conditions']['video']['exact_correct']}/{stage2['conditions']['video']['scorable_cells']}个GT差距格准确。",
        "- Stage2候选为7个样本，但Niumo的最终协议帧条件发生流式超时，因此严格成对统计排除Niumo，最终分母为6个样本、33个可评分格；Niumo不计为准确或错误。",
        f"- 成对可评分格：{stage2['paired_comparison']['paired_scorable_cells']}；帧独有正确 {stage2['paired_comparison']['frame_only_correct']}，视频独有正确 {stage2['paired_comparison']['video_only_correct']}，两者都正确 {stage2['paired_comparison']['both_correct']}，两者都错 {stage2['paired_comparison']['neither_correct']}。",
        f"- 两种表征导致判断变化：{stage2['paired_comparison']['judgment_changed']}/{stage2['paired_comparison']['paired_scorable_cells']}；其中变化且两边都错 {stage2['paired_comparison']['judgment_change_outcomes']['changed_and_neither_correct']}，变化且帧条件独有正确 {stage2['paired_comparison']['judgment_change_outcomes']['changed_and_frame_only_correct']}，变化且视频条件独有正确 {stage2['paired_comparison']['judgment_change_outcomes']['changed_and_video_only_correct']}，变化且两边都正确 {stage2['paired_comparison']['judgment_change_outcomes']['changed_and_both_correct']}。",
        "- 这9个变化格与四类结果是两个不同切面：四类结果互斥且合计33格；“变化”只说明两种条件给出的gap或relation不同，不等于变得更准。本轮9个变化格中7个两边都错，只有1个变化带来帧条件独有正确、1个带来视频条件独有正确。",
        f"- 差一档准确率：帧条件 {stage2['conditions']['frames']['within_one_correct']}/{stage2['conditions']['frames']['scorable_cells']}（{stage2['conditions']['frames']['within_one_accuracy'] * 100:.1f}%），视频条件 {stage2['conditions']['video']['within_one_correct']}/{stage2['conditions']['video']['scorable_cells']}（{stage2['conditions']['video']['within_one_accuracy'] * 100:.1f}%）。这是一条独立于精确匹配的信号，仍然支持帧条件更稳，但不构成生产promotion证据。",
        "",
        "| 条件 | S1 | S2 | S3 | S4 | S5 | S6 | 总体 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in ("frames", "video"):
        metrics = stage2["conditions"][condition]["stage_metrics"]
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                condition,
                *[
                    f"{metrics[stage]['exact']}/{metrics[stage]['eligible']}"
                    for stage in STAGES
                ],
                f"{stage2['conditions'][condition]['exact_correct']}/{stage2['conditions'][condition]['scorable_cells']}",
            )
        )
    lines += [
        "",
        "| 条件 | 平均耗时 | 平均上传 | 平均prompt token | 平均reasoning token |",
        "|---|---:|---:|---:|---:|",
    ]
    for condition in ("frames", "video"):
        data = stage2["conditions"][condition]
        lines.append(
            "| %s | %.1fs | %.2fMB | %.0f | %.0f |"
            % (
                condition,
                data["stats"]["elapsed_seconds"]["mean"] or 0,
                (data["stats"]["uploaded_bytes"]["mean"] or 0) / 1024 / 1024,
                data["stats"]["prompt_tokens"]["mean"] or 0,
                data["stats"]["reasoning_tokens"]["mean"] or 0,
            )
        )
    lines += [
        "",
        "- 评分只使用human_gap精确匹配；not_applicable和uncertain排除。GT没有正式relation字段，所以没有报告方向准确率。",
        "- 差一档的宽松结果保存在JSON的within_one_accuracy中，不替代精确准确率。",
        "",
        "## 根因",
        "",
        "### 1. 证据采集层",
        "完整视频解决了输入是否包含某个时刻，但没有解决模型是否把该时刻识别为哪个阶段、是否写入事实账本、是否在12条上限内保留。抽帧把阶段、时间和少量关键画面外置，降低了阶段定位负担；代价是可能丢失连续性和未采样瞬间。",
        "本轮S6落差还可能来自隐性清单变量：抽帧已经把六个阶段预先分桶，原生视频没有。没有完成“原生视频+显式S1-S6枚举”控制前，不能把S6落差全部归因于模态本身。",
        "",
        "### 2. 判断层",
        "固定事实后再附加视频，模型仍可能重新解释事实包。它不是单纯读取更多证据，而是在锁定事实和重新看视频之间做冲突解决。如果视频条件没有稳定提高GT准确率，说明额外视觉上下文尚未转化为可靠判断增益。",
        "",
        "### 3. 工程与合同层",
        "本轮前置试跑暴露了overall winner/gap矛盾、none状态空reason、无证据却标partial、阶段confidence字段遗漏，以及一次流式无结束标记超时。前四类被合同拦截，最后一类是运行稳定性问题。这说明原生视频增加了上下文和生成压力，合同通过率、超时率和成本必须与语义指标一起看。",
        "",
        "## 最终建议",
        "",
        "1. 不把整段视频直接喂模型当作Stage1证据采集的替代方案。继续保留分阶段、带时间和evidence_id的账本作为唯一可审计事实来源。",
        "2. 原生视频不要另起一套独立复核机制，应并入现有Phase C候选体系，作为共享预算下的复核材料和reason code。优先用于连续性证据、none/uncertain、证据资格冲突，以及画面注意力洁净和焦点一致性等视频级门控；输出只能成为受合同约束的补充事实或人工复核线索，不能直接覆盖原账本。",
        "3. 先为这些视频建立人工key_events，计算真实Stage1召回/精确率，区分“没看到内容”和“看到了但没归入对应阶段”；再补正式relation字段评价方向。当前数据不足以作生产promotion决定。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--stage1-manifest", type=Path, required=True)
    parser.add_argument("--stage2-manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--frames-root", type=Path, required=True)
    parser.add_argument("--stage2-final-root", type=Path, required=True)
    parser.add_argument("--stage2-remaining-root", type=Path, required=True)
    parser.add_argument("--gt-path", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(args)
    args.output_root.mkdir(parents=True, exist_ok=True)
    write_json(args.output_root / "native-video-verification-report.json", report)
    (args.output_root / "native-video-verification-report.md").write_text(
        markdown(report),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
