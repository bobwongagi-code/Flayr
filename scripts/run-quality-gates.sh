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

# The legacy tree predates full Black and strict typing. Keep the gates focused
# on new CI helpers, contract modules, and high-confidence source regressions.
"$PYTHON_BIN" -m ruff check scripts tests --select E4,E7,E9,F821 --ignore E402
"$PYTHON_BIN" -m black --check scripts/ci_quality.py scripts/ci_tool_smoke.py tests/test_ci_quality.py
"$PYTHON_BIN" -m mypy \
  scripts/ci_quality.py \
  scripts/ci_tool_smoke.py \
  scripts/flayr_core/analysis_model.py \
  scripts/flayr_core/network.py \
  scripts/flayr_core/resources.py \
  scripts/flayr_core/run_state.py \
  --ignore-missing-imports \
  --follow-imports=skip \
  --no-error-summary
"$PYTHON_BIN" -m bandit -q -r scripts --severity-level high --confidence-level high
"$PYTHON_BIN" -m pip_audit -r requirements-dev.lock
"$PYTHON_BIN" scripts/ci_quality.py
"$PYTHON_BIN" scripts/ci_tool_smoke.py
"$PYTHON_BIN" -m coverage report --fail-under=55
