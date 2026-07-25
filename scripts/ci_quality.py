#!/usr/bin/env python3
"""Small repository quality checks that do not require third-party scanners."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 4 * 1024 * 1024
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|token)\b"
            r"\s*[:=]\s*[\"']([A-Za-z0-9_+/=-]{20,})[\"']"
        ),
    ),
)


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(item) for item in completed.stdout.decode("utf-8").split("\0") if item]


def find_secret_matches(text: str) -> list[str]:
    """Return redacted pattern names and line numbers, never matched values."""

    matches: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                matches.append(f"{name} at line {line_number}")
    return matches


def scan_tracked_files(paths: Iterable[Path] | None = None) -> list[str]:
    issues: list[str] = []
    for relative_path in paths if paths is not None else tracked_files():
        path = ROOT / relative_path
        try:
            raw = path.read_bytes()
        except OSError as exc:
            issues.append(f"{relative_path}: cannot read tracked file: {exc}")
            continue
        if len(raw) > MAX_SCAN_BYTES:
            issues.append(
                f"{relative_path}: tracked file exceeds {MAX_SCAN_BYTES} byte scan limit"
            )
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        issues.extend(
            f"{relative_path}: {match}" for match in find_secret_matches(text)
        )
    return issues


def main() -> int:
    files = tracked_files()
    issues = scan_tracked_files(files)
    if issues:
        print("tracked-file secret scan failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"tracked-file secret scan passed: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
