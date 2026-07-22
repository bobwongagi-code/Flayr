#!/usr/bin/env python3
"""Validate the source-release version and dependency lock contracts."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise SystemExit(f"invalid VERSION: {version!r}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        raise SystemExit(f"CHANGELOG.md is missing the VERSION entry: {version}")

    lock_entries = []
    for line in (ROOT / "requirements-dev.lock").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "==" not in stripped or stripped.startswith(("-", ".", "/")):
            raise SystemExit(f"dependency is not an exact package pin: {stripped}")
        lock_entries.append(stripped)
    if not lock_entries:
        raise SystemExit("requirements-dev.lock must contain at least one exact pin")
    forbidden = ("certifi", "cosyvoice", "wan2.")
    for entry in lock_entries:
        if any(token in entry.lower() for token in forbidden):
            raise SystemExit(f"removed media/voice dependency is still locked: {entry}")

    print(f"release checks passed: version={version}, locked_dependencies={len(lock_entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
