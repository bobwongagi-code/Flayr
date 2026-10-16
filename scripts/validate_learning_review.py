#!/usr/bin/env python3
"""Read-only material and optional author wording checks, not delivery approval."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


REQUIRED = ("intended_belief", "established_support", "priority_gap")
FOUNDATION_FIELDS = (
    "core_selling_points", "usage_context", "consumer_outcome",
    "observable_signal", "proof_condition",
)
BASES = {"source_claim", "user_confirmed", "analysis_proposal", "unknown"}
INTERNAL_NOTES = "## 内部审核备注，不随作者正文发送"
AUTHOR_CHECKS = (
    ("internal label", re.compile(
        r"达人|标杆|资格层|门禁|档位|"
        r"(?<![A-Za-z0-9_])(?:S[1-6]|Stage[12]|DEGRADED|prediction_unavailable)(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )),
    ("performance forecast needs review", re.compile(
        r"(?:完播率|出单率|转化率|点击率|销量|订单量|销售额|收入)"
        r"[^。！？\n]{0,24}(?:翻倍|翻[一二两三四五六七八九十\d]+倍|"
        r"(?:提升|增加|增长)[^。！？\n]{0,8}\d+(?:\.\d+)?\s*[%％]|"
        r"(?:一定|保证|必然)[^。！？\n]{0,8}(?:提升|增加|增长|上涨))"
    )),
    ("claim needs review", re.compile(
        r"真实变化最能出单|买家被吓跑了|为什么那条视频能跑出单|"
        r"同款卖爆的那条|跑得最快的一个案例|同品类最近出单最多|"
        r"(?:保证|必定|一定|肯定)(?:能|会)?(?:爆单|出单|起量|赚|多卖)|改了就能起量"
    )),
)


def validate_author_copy(text):
    """Flag limited wording issues in Markdown author prose without rewriting it."""
    errors = []
    author_lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip() == INTERNAL_NOTES:
            break
        # Keep captions and link labels; source paths are not delivery prose.
        visible = re.sub(r"(!?\[[^\]]*\])\([^\n)]*\)", r"\1", line)
        author_lines.append(visible)
        for label, pattern in AUTHOR_CHECKS:
            match = pattern.search(visible)
            if match:
                errors.append(f"line {number}: {label}: {match.group(0)}")
    if not any(line.strip() for line in author_lines):
        errors.append("author prose must be nonempty before internal notes")
    return errors


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def validate(data):
    if not isinstance(data, dict):
        return ["review must be an object"]
    errors = []
    product = data.get("product_context")
    foundation = product.get("review_foundation") if isinstance(product, dict) else None
    if not isinstance(product, dict) or not _text(product.get("name")):
        errors.append("product_context.name must be nonempty text")
    if not isinstance(foundation, dict):
        errors.append("product_context.review_foundation must be an object")
    else:
        for field in FOUNDATION_FIELDS:
            entry = foundation.get(field)
            if not isinstance(entry, dict):
                errors.append(f"review_foundation.{field} must be an object")
                continue
            for key in ("text", "source"):
                if not _text(entry.get(key)):
                    errors.append(f"review_foundation.{field}.{key} must be nonempty text")
            basis = entry.get("basis")
            if not isinstance(basis, str) or basis not in BASES:
                errors.append(f"review_foundation.{field}.basis is unsupported")
    if not _text(data.get("teaching_reference")):
        errors.append("teaching_reference must be nonempty text")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return errors + ["cases must be a nonempty list"]
    seen = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"case {index}: expected object")
            continue
        case_id = case.get("id")
        if not _text(case_id):
            errors.append(f"case {index}: missing id")
        elif case_id in seen:
            errors.append(f"case {index}: duplicate id {case_id}")
        else:
            seen.add(case_id)
        for field in ("product_question", "reference_logic"):
            if not _text(case.get(field)):
                errors.append(f"{case_id}: {field} must be nonempty text")
        points = case.get("teaching_points")
        if not isinstance(points, list) or not points or not all(_text(p) for p in points):
            errors.append(f"{case_id}: teaching_points must contain reference IDs")
        whole = case.get("whole_video")
        if not isinstance(whole, dict):
            errors.append(f"{case_id}: whole_video must be an object")
        else:
            for field in REQUIRED:
                if not _text(whole.get(field)):
                    errors.append(f"{case_id}: whole_video.{field} must be nonempty text")
        facts = case.get("facts")
        if not isinstance(facts, list) or not facts:
            errors.append(f"{case_id}: facts must contain source-labelled evidence or review limitations")
        else:
            for fact_index, fact in enumerate(facts):
                if not isinstance(fact, dict) or not all(
                    _text(fact.get(key)) for key in ("kind", "evidence", "text")
                ):
                    errors.append(f"{case_id}: fact {fact_index} requires kind/evidence/text")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, nargs="?",
                        help="Learning-review material packet; omit for author wording checks only")
    parser.add_argument("--author-copy", type=Path, action="append", default=[],
                        help="Check a current Markdown author draft; repeat for multiple drafts")
    args = parser.parse_args(argv)
    if args.cases is None and not args.author_copy:
        parser.error("Provide a learning-review packet or at least one --author-copy")
    if args.cases is not None:
        try:
            data = json.loads(args.cases.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Cannot read review: {exc}\n")
        errors = validate(data)
        if errors:
            parser.exit(1, "\n".join(errors) + "\n")
        # Resolve beside the packet so the check behaves the same from any cwd.
        teaching = Path(data["teaching_reference"])
        if not teaching.is_absolute():
            teaching = args.cases.resolve().parent / teaching
        try:
            if not teaching.read_text(encoding="utf-8").strip():
                parser.exit(1, f"Teaching reference is empty: {teaching}\n")
        except (OSError, ValueError) as exc:
            parser.exit(1, f"Cannot read teaching reference: {exc}\n")
    wording_errors = []
    for draft in args.author_copy:
        try:
            wording_errors.extend(f"{draft}: {error}" for error in
                                  validate_author_copy(draft.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            wording_errors.append(f"Cannot read author copy {draft}: {exc}")
    if wording_errors:
        parser.exit(1, "\n".join(wording_errors) + "\n")
    if args.cases is not None:
        print(f"PASS: {len(data['cases'])} cases have product, teaching, evidence and whole-video fields.")
        print("Teaching reference is readable. Reference IDs and evidence contents are not verified.")
        print("Completeness only; source truth, reasoning, priority and delivery still require human review.")
    if args.author_copy:
        print(f"PASS: {len(args.author_copy)} author drafts have no listed wording matches.")
        print("Limited wording check only; tone, factual support and delivery approval are not verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
