#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
python3 scripts/check_prompt_reachability.py
