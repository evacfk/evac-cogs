#!/usr/bin/env bash
# Run from the repo root (the folder that contains bumpreward/):  bash bumpreward/run_tests.sh
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=bumpreward python -m pytest -p _redbot_stub bumpreward -q "$@"
