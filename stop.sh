#!/usr/bin/env bash
# Stop the supervisor and the watcher.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pids="$(cat "$DIR/supervisor.pid" 2>/dev/null) $(pgrep -f "$DIR/river-watcher.sh") "
wpids="$(cat "$DIR/watcher.pid" 2>/dev/null) $(pgrep -f "$DIR/watcher.py")"
for p in $pids; do kill -TERM "$p" 2>/dev/null; done
for p in $wpids; do kill -TERM "$p" 2>/dev/null; done
for i in $(seq 1 10); do
  alive=""
  for p in $pids $wpids; do kill -0 "$p" 2>/dev/null && alive=1; done
  [ -z "$alive" ] && break
  sleep 1
done
for p in $pids $wpids; do kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null; done
rm -f "$DIR/supervisor.pid" "$DIR/watcher.pid"
echo "stopped"
