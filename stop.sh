#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${HEADSUP_PYTHON:-$DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$DIR/supervisor.py" stop
