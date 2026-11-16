#!/usr/bin/env bash
# One-command setup + run:  ./run.sh   then open http://localhost:8765
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port "${PORT:-8765}"
