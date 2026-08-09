#!/usr/bin/env python3
"""Enforce the frozen batch file and line budget before a change is committed."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "references" / "bitter-lesson-frozen-spec.json"


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _matches(path: str, patterns: list[str]) -> bool:
    path_obj = Path(path)
    return any(fnmatch.fnmatchcase(path, pattern) or path_obj.match(pattern) for pattern in patterns)


def _changed_paths(base_ref: str) -> set[str]:
    changed: set[str] = set()
    for line in _git("diff", "--name-only", "--no-renames", base_ref, "--").splitlines():
        if line.strip():
            changed.add(line.strip())
    for line in _git("ls-files", "--others", "--exclude-standard").splitlines():
        if line.strip():
            changed.add(line.strip())
    return changed


def _line_counts(base_ref: str, changed: set[str]) -> tuple[int, int]:
    added = 0
    deleted = 0
    numstat = _git("diff", "--numstat", "--no-renames", base_ref, "--")
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[2] not in changed:
            continue
        plus, minus = parts[:2]
        if plus.isdigit():
            added += int(plus)
        if minus.isdigit():
            deleted += int(minus)
    for relative in sorted(changed):
        if not (ROOT / relative).is_file():
            continue
        tracked = _git("ls-files", "--", relative).strip()
        if tracked:
            continue
        try:
            added += len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            continue
    return added, deleted


def check_scope(spec: dict[str, Any], base_ref: str) -> list[str]:
    scope = spec["change_scope"]
    changed = _changed_paths(base_ref)
    if not changed:
        return []
    allowed = [str(item) for item in scope["allowed_globs"]]
    forbidden = [str(item) for item in scope["forbidden_globs"]]
    issues = []
    for path in sorted(changed):
        if _matches(path, forbidden):
            issues.append(f"forbidden path: {path}")
        elif not _matches(path, allowed):
            issues.append(f"path outside declared batch scope: {path}")
    added, deleted = _line_counts(base_ref, changed)
    if len(changed) > scope["max_files"]:
        issues.append(f"file budget exceeded: {len(changed)} > {scope['max_files']}")
    if added > scope["max_added_lines"]:
        issues.append(f"added-line budget exceeded: {added} > {scope['max_added_lines']}")
    if deleted > scope["max_deleted_lines"]:
        issues.append(f"deleted-line budget exceeded: {deleted} > {scope['max_deleted_lines']}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", default="HEAD", help="Git ref used as the batch baseline")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    args = parser.parse_args()
    spec = json.loads(args.spec.expanduser().resolve().read_text(encoding="utf-8"))
    issues = check_scope(spec, args.base_ref)
    if issues:
        print("change scope gate failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print(f"change scope gate passed: base={args.base_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
