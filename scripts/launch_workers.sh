#!/usr/bin/env bash
# Thin wrapper for scripts/worker_loop.py — master control loop.
# Adopt-first by design: every command re-adopts live worker PIDs by session
# id before acting, so a second wave is never started for a running/reported
# or PR-open role. Budgets are set inside worker_loop.py (workers: variant
# fast, thinking off, output 8192; master/reviewers: medium, thinking, 16384).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
CMD="${1:-status}"; shift || true
exec "$PY" "$HERE/worker_loop.py" "$CMD" "$@"
