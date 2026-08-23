#!/usr/bin/env python3
"""Small local CLI for the single-user human review sidecar."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping


SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from flayr_core.human_review import (  # noqa: E402
    GAPS,
    RELATIONS,
    STAGE_CODES,
    HumanReviewError,
    analysis_stage_map,
    init_review,
    load_analysis,
    load_review,
    model_stage_values,
    priority_key,
    review_status,
    update_stage,
    write_reviewed_reports,
)


def _stage_map(analysis: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return analysis_stage_map(analysis)


def _text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value or "").strip()


def _show_stage(code: str, stage: Mapping[str, Any], review_stage: Mapping[str, Any]) -> None:
    values = model_stage_values(stage)
    print(f"\n[{code}] 当前决定：{review_stage.get('decision', 'pending')}")
    print(f"模型 relation/gap：{values.get('relation') or '无'} / {values.get('gap') or '无'}")
    print(f"模型理由：{values.get('reason') or '无'}")
    print(f"达人摘要：{_text(stage.get('creator_summary') or stage.get('creator_key_message')) or '无'}")
    print(f"标杆摘要：{_text(stage.get('benchmark_summary') or stage.get('benchmark_key_message')) or '无'}")
    ids = values.get("evidence_ids") or []
    print(f"证据 ID：{'、'.join(ids) if ids else '无'}")
    if values.get("not_applicable"):
        print("提示：代码阶段门禁已确认未涉及，请选择 n 确认；不能用 c 确认。")
    elif values.get("prediction_unavailable"):
        print("提示：当前没有可比较的模型输出，不能用 c 确认。")


def _choose_relation() -> str | None:
    value = input("relation [tie/benchmark_better/creator_better]，q 取消：").strip()
    if value == "q":
        return None
    if value not in RELATIONS:
        print("relation 不合法。")
        return _choose_relation()
    return value


def _choose_gap() -> str | None:
    value = input("gap [none/small/medium/large]，q 取消：").strip()
    if value == "q":
        return None
    if value not in GAPS:
        print("gap 不合法。")
        return _choose_gap()
    return value


def _prompt_decision(code: str, stage: Mapping[str, Any], review_stage: Mapping[str, Any]) -> dict[str, Any] | None:
    _show_stage(code, stage, review_stage)
    while True:
        choice = input("操作：c确认 e更正 n不适用 i证据不足 s跳过 q退出：").strip().lower()
        if choice == "q":
            return None
        if choice == "s":
            return {"skip": True}
        if choice == "c":
            values = model_stage_values(stage)
            if values.get("not_applicable"):
                print("该阶段已由代码门禁标记为未涉及，请选择 n；不能用 c 确认。")
                continue
            if values.get("prediction_unavailable"):
                print("prediction_unavailable 不能确认，请选择 e/n/i。")
                continue
            return {"decision": "confirmed", "relation": None, "gap": None, "note": None}
        if choice == "e":
            relation = _choose_relation()
            if relation is None:
                continue
            gap = _choose_gap()
            if gap is None:
                continue
            note = input("简短 note：").strip()
            if not note:
                print("corrected 必须填写 note。")
                continue
            return {"decision": "corrected", "relation": relation, "gap": gap, "note": note}
        if choice in {"n", "i"}:
            note = input("简短 note：").strip()
            if not note:
                print("该决定必须填写 note。")
                continue
            return {
                "decision": "not_applicable" if choice == "n" else "insufficient_evidence",
                "relation": None,
                "gap": None,
                "note": note,
            }
        print("请输入 c、e、n、i、s 或 q。")


def _apply_decision(run_dir: Path, code: str, decision: Mapping[str, Any], *, approved_edit: bool = False) -> bool:
    if decision.get("skip"):
        return False
    kwargs = {
        "relation": decision.get("relation"),
        "gap": decision.get("gap"),
        "note": decision.get("note"),
    }
    # update_stage intentionally reopens one stage when an approved sidecar is
    # edited.  Apply the selected value again after that invalidation pass.
    update_stage(run_dir, code, str(decision["decision"]), **kwargs)
    if approved_edit:
        update_stage(run_dir, code, str(decision["decision"]), **kwargs)
    return True


def _status(run_dir: Path) -> int:
    review = init_review(run_dir)
    print(f"status: {review_status(review)}")
    for code in STAGE_CODES:
        print(f"{code}: {review['stages'][code]['decision']}")
    return 0


def _review(run_dir: Path) -> int:
    init_review(run_dir)
    review = load_review(run_dir)
    analysis = load_analysis(run_dir)
    stages = _stage_map(analysis)

    if review_status(review) == "approved":
        while True:
            code = input("已完成审核。输入 S1-S6 修订，或 q 退出：").strip().upper()
            if code == "Q" or not code:
                return 0
            if code not in STAGE_CODES:
                print("阶段必须是 S1-S6。")
                continue
            decision = _prompt_decision(code, stages.get(code, {}), review["stages"][code])
            if decision is None or decision.get("skip"):
                return 0
            try:
                _apply_decision(run_dir, code, decision, approved_edit=True)
            except HumanReviewError as exc:
                print(f"未保存：{exc}")
                continue
            review = load_review(run_dir)
            if review_status(review) != "approved":
                print("该阶段已重新打开，审核状态回到 pending；请重新运行 CLI 完成剩余阶段。")
                return 0
            paths = write_reviewed_reports(run_dir)
            print(f"修订已确认，已重新生成：{paths[0].name}、{paths[1].name}")

    pending = [
        code
        for code in STAGE_CODES
        if review["stages"][code]["decision"] == "pending" and code in stages
    ]
    pending.sort(key=lambda code: priority_key(code, stages[code]))
    for code in pending:
        review = load_review(run_dir)
        decision = _prompt_decision(code, stages[code], review["stages"][code])
        if decision is None:
            print("已退出，当前审核保留为草稿。")
            return 0
        if decision.get("skip"):
            continue
        try:
            _apply_decision(run_dir, code, decision)
        except HumanReviewError as exc:
            print(f"未保存：{exc}")
            continue

    review = load_review(run_dir)
    if review_status(review) != "approved":
        print("审核尚未完成，未生成 reviewed 报告。")
        return 0
    paths = write_reviewed_reports(run_dir)
    print(f"审核完成，已生成：{paths[0].name}、{paths[1].name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flayr 单人本地人工审核")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--status", action="store_true", help="只显示审核状态")
    args = parser.parse_args(argv)
    try:
        return _status(args.run_dir) if args.status else _review(args.run_dir)
    except (HumanReviewError, OSError) as exc:
        print(f"审核失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
