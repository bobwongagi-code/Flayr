#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    PYTHON_BIN="python"
  fi
fi

"$PYTHON_BIN" scripts/check_release.py
if [[ "${FLAYR_COVERAGE:-0}" == "1" ]]; then
  "$PYTHON_BIN" -m coverage run --branch -m unittest discover -s tests -v
else
  "$PYTHON_BIN" -m unittest discover -s tests -v
fi
"$PYTHON_BIN" scripts/check_prompt_reachability.py
"$PYTHON_BIN" scripts/verify_analysis_contracts.py
