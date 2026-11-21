#!/usr/bin/env python3
"""Check a local batch budget or the publication boundary of a Git range."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "references" / "bitter-lesson-frozen-spec.json"
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(*args: str, raw_paths: bool = False) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=not raw_paths,
    )
    if raw_paths:
        # NUL-delimited Git paths are unquoted bytes, regardless of locale.
        return completed.stdout.decode("utf-8", errors="surrogateescape")
    return completed.stdout


def _matches(path: str, patterns: list[str]) -> bool:
    path_obj = Path(path)
    return any(fnmatch.fnmatchcase(path, pattern) or path_obj.match(pattern) for pattern in patterns)


def _is_zero_ref(ref: str) -> bool:
    normalized = str(ref or "").strip()
    return not normalized or (len(normalized) == 40 and set(normalized) == {"0"})


def _changed_paths(base_ref: str) -> set[str]:
    changed: set[str] = set()
    if _is_zero_ref(base_ref):
        # An all-zero push SHA means there is no remote base. Compare against
        # the complete current tree, not HEAD^ or only the tip commit;
        # otherwise an initial branch push can hide older out-of-scope
        # commits from the scope gate.
        diff_args = (
            "diff",
            "--name-only",
            "--no-renames",
            EMPTY_TREE_SHA,
            "HEAD",
            "--",
        )
    else:
        diff_args = ("diff", "--name-only", "--no-renames", base_ref, "--")
    for line in _git(*diff_args).splitlines():
        if line.strip():
            changed.add(line.strip())
    for line in _git("ls-files", "--others", "--exclude-standard").splitlines():
        if line.strip():
            changed.add(line.strip())
    return changed


def _line_counts(base_ref: str, changed: set[str]) -> tuple[int, int]:
    added = 0
    deleted = 0
    if _is_zero_ref(base_ref):
        numstat = _git(
            "diff",
            "--numstat",
            "--no-renames",
            EMPTY_TREE_SHA,
            "HEAD",
            "--",
        )
    else:
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


def _publication_changes(base_ref: str, head_ref: str) -> Iterator[tuple[str, list[str]]]:
    head = _git("rev-parse", "--verify", "--end-of-options", f"{head_ref}^{{commit}}").strip()
    revisions = [head]
    if not _is_zero_ref(base_ref):
        base = _git("rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}").strip()
        revisions.append(f"^{base}")
    commits = _git("rev-list", "--reverse", "--topo-order", *revisions, "--").splitlines()
    for commit in commits:
        # Walk both sides of merges, but attribute only paths changed from
        # all parents to the merge itself, not files inherited from a parent.
        paths = _git(
            "diff-tree", "--root", "-c", "--no-commit-id", "--name-only",
            "--no-renames", "-r", "-z", commit, "--", raw_paths=True,
        )
        yield commit, sorted(filter(None, paths.split("\0")))


def check_publication(
    spec: dict[str, Any],
    base_ref: str,
    *,
    head_ref: str = "HEAD",
    approved_paths: Sequence[str] | None = None,
) -> list[str]:
    """Check paths introduced, changed or removed by each outgoing commit."""
    forbidden = [str(item) for item in spec["change_scope"]["forbidden_globs"]]
    approved = None if approved_paths is None else set(approved_paths)
    issues = []
    for commit, paths in _publication_changes(base_ref, head_ref):
        for path in paths:
            if _matches(path, forbidden):
                issues.append(f"{commit}: forbidden path: {path}")
            elif approved is not None and path not in approved:
                issues.append(f"{commit}: path outside approved publication scope: {path}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", help="Git ref used as the baseline (default: HEAD)")
    parser.add_argument("--head-ref", help="Candidate commit for --publication (default: HEAD)")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH)
    parser.add_argument("--publication", action="store_true",
                        help="Check publication paths in each outgoing commit, without batch budgets")
    parser.add_argument("--approved-path", action="append", metavar="PATH",
                        help="Exact repo-relative path approved for this push; repeat for each file")
    args = parser.parse_args()
    if args.head_ref is not None and not args.publication:
        parser.error("--head-ref requires --publication")
    if args.approved_path is not None and (not args.publication or args.base_ref is None):
        parser.error("--approved-path requires --publication and an explicit --base-ref")
    base_ref = args.base_ref if args.base_ref is not None else "HEAD"
    spec = json.loads(args.spec.expanduser().resolve().read_text(encoding="utf-8"))
    if args.publication:
        issues = check_publication(
            spec, base_ref, head_ref=args.head_ref if args.head_ref is not None else "HEAD",
            approved_paths=args.approved_path,
        )
    else:
        issues = check_scope(spec, base_ref)
    gate = "publication boundary" if args.publication else "change scope"
    if issues:
        print(f"{gate} gate failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print(f"{gate} gate passed: base={base_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
