#!/usr/bin/env python3
"""Inventory reads and writes of critical analysis-result fields.

The inventory remains conservative rather than pretending to be a type
checker. In ``--check`` mode, however, literal production writes are a CI
boundary: a critical field may only be mutated by its declared owner modules.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


DEFAULT_FIELDS = (
    "analysis_status",
    "benchmark_evidence_ids",
    "comparison_status",
    "creator_evidence_ids",
    "model_gap_magnitude",
    "model_severity",
    "severity",
    "severity_derivation",
    "stage2_candidate_status",
    "stage2_pipeline_status",
    "stage_analysis",
    "stage_evidence_gate",
    "stage_evidence_links",
    "stage_handoff_status",
    "video_understanding",
)
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "runs", "output"}
TEXT_SUFFIXES = {".html", ".js", ".json", ".md", ".mjs", ".sh"}
PRODUCTION_PREFIX = "scripts/flayr_core/"
PRODUCTION_WRITE_POLICY = {
    "analysis_status": {
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/stage_evidence_contracts.py",
    },
    "benchmark_evidence_ids": {
        "scripts/flayr_core/postprocess/repair_evidence.py",
        "scripts/flayr_core/postprocess/utils.py",
    },
    "creator_evidence_ids": {
        "scripts/flayr_core/postprocess/repair_evidence.py",
        "scripts/flayr_core/postprocess/utils.py",
    },
    "comparison_status": {
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/postprocess/repair_stages.py",
    },
    "model_gap_magnitude": {"scripts/flayr_core/llm/pipeline.py"},
    "model_severity": {
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/postprocess/derive.py",
    },
    "severity": {
        "scripts/flayr_core/llm/compact_eval.py",
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/postprocess/derive.py",
    },
    "severity_derivation": {
        "scripts/flayr_core/finalization/equivalence.py",
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/postprocess/derive.py",
    },
    "stage2_candidate_status": {"scripts/flayr_core/llm/pipeline.py"},
    "stage2_pipeline_status": {"scripts/flayr_core/llm/pipeline.py"},
    "stage_analysis": {
        "scripts/flayr_core/llm/parse.py",
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/postprocess/repair_claims.py",
    },
    "stage_evidence_gate": {
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/stage_evidence_contracts.py",
    },
    "stage_evidence_links": {"scripts/flayr_core/stage_evidence_contracts.py"},
    "stage_handoff_status": {"scripts/flayr_core/llm/pipeline.py"},
    "video_understanding": {
        "scripts/flayr_core/llm/pipeline.py",
        "scripts/flayr_core/postprocess/repair_claims.py",
    },
}


def _literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class FieldVisitor(ast.NodeVisitor):
    def __init__(self, fields: set[str]) -> None:
        self.fields = fields
        self.uses: list[tuple[str, str, int]] = []

    def _record(self, field: str | None, operation: str, node: ast.AST) -> None:
        if field in self.fields:
            self.uses.append((field, operation, int(getattr(node, "lineno", 0))))

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        field = _literal_string(node.slice)
        operation = "write" if isinstance(node.ctx, (ast.Store, ast.Del)) else "read"
        self._record(field, operation, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            field = _literal_string(node.args[0]) if node.args else None
            if method == "get":
                self._record(field, "read", node)
            elif method in {"pop", "setdefault"}:
                self._record(field, "write", node)
            elif method == "update" and node.args and isinstance(node.args[0], ast.Dict):
                for key in node.args[0].keys:
                    self._record(_literal_string(key), "write", node)
        self.generic_visit(node)


def _repository_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts)
        and (path.suffix == ".py" or path.suffix in TEXT_SUFFIXES)
    )


def inventory(root: Path, fields: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    tracked = set(fields)
    result: dict[str, list[dict[str, Any]]] = {field: [] for field in fields}
    for path in _repository_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError:
                continue
            visitor = FieldVisitor(tracked)
            visitor.visit(tree)
            for field, operation, line in visitor.uses:
                result[field].append({"path": relative, "line": line, "operation": operation})
        else:
            for line_number, line in enumerate(text.splitlines(), start=1):
                for field in fields:
                    if field in line:
                        result[field].append({
                            "path": relative,
                            "line": line_number,
                            "operation": "reference",
                        })
    for uses in result.values():
        uses.sort(key=lambda item: (item["path"], item["line"], item["operation"]))
    return result


def ownership_violations(result: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Reject new production writers outside the frozen layer-owner modules."""
    violations: list[str] = []
    for field, uses in result.items():
        allowed = PRODUCTION_WRITE_POLICY.get(field, set())
        for use in uses:
            path = str(use.get("path") or "")
            if use.get("operation") != "write" or not path.startswith(PRODUCTION_PREFIX):
                continue
            if path not in allowed:
                violations.append(
                    f"{field}: unauthorized production writer {path}:{use.get('line', 0)}"
                )
    return sorted(violations)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--field", action="append", dest="fields")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    fields = tuple(dict.fromkeys(args.fields or DEFAULT_FIELDS))
    result = inventory(root, fields)
    if args.check:
        violations = ownership_violations(result)
        if violations:
            print("result field ownership check failed:")
            for violation in violations:
                print(f"- {violation}")
            return 1
        print("result field ownership check passed")
        return 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
