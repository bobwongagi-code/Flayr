#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python3 scripts/check_release.py
python3 -m unittest discover -s tests -v
python3 scripts/check_prompt_reachability.py
python3 scripts/verify_analysis_contracts.py
