#!/usr/bin/env python3
"""Inventory reads and writes of critical analysis-result fields.

This is a conservative migration aid, not a type checker. It reports every
literal Python access and every literal reference in non-Python repository
files so a field cannot be declared unused from a local call-path reading
alone.
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
        relative = str(path.relative_to(root))
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--field", action="append", dest="fields")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    fields = tuple(dict.fromkeys(args.fields or DEFAULT_FIELDS))
    print(json.dumps(inventory(root, fields), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
