#!/usr/bin/env bash
# Supervisor: (re)start watcher.py ~10s after it exits. Started by ensure-running.sh.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
PY="${RIVER_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$DIR/.venv/bin/python" ]; then PY="$DIR/.venv/bin/python"; else PY="$(command -v python3)"; fi
fi
echo $$ > "$DIR/supervisor.pid"
CHILD=""
cleanup() {
  [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null && wait "$CHILD" 2>/dev/null
  rm -f "$DIR/supervisor.pid" "$DIR/watcher.pid"
  exit 0
}
trap cleanup TERM INT HUP

while true; do
  # Optional gitignored env file (e.g. DELAWARE_WEBHOOK_URL/KEY); re-read on every restart.
  if [ -f "$DIR/.env.local" ]; then set -a; . "$DIR/.env.local"; set +a; fi
  [ -z "${AISSTREAM_API_KEY:-}" ] && echo "$(date -Is) WARN AISSTREAM_API_KEY not in env" >&2
  echo "$(date -Is) supervisor: starting watcher.py"
  "$PY" "$DIR/watcher.py" &
  CHILD=$!
  echo "$CHILD" > "$DIR/watcher.pid"
  wait "$CHILD"
  rc=$?
  CHILD=""
  echo "$(date -Is) supervisor: watcher.py exited rc=$rc; restarting in 10s"
  sleep 10 &
  wait $!
done
