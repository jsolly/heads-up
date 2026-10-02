#!/usr/bin/env bash
# Idempotent: start the supervisor with nohup unless it is already running. Prints running/started.
# Run it from a shell that has AISSTREAM_API_KEY (and optionally DELAWARE_WEBHOOK_URL/KEY) in env;
# the supervisor and watcher inherit that environment. A gitignored .env.local is also sourced.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDF="$DIR/supervisor.pid"
if [ -f "$PIDF" ]; then
  pid="$(cat "$PIDF" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q river-watcher.sh; then
    echo "running (supervisor pid $pid)"
    exit 0
  fi
fi
# no pidfile but maybe a stray supervisor
pid="$(pgrep -f "$DIR/river-watcher.sh" | head -n1)"
if [ -n "$pid" ]; then echo "$pid" > "$PIDF"; echo "running (supervisor pid $pid)"; exit 0; fi
cd "$DIR" || exit 1
nohup setsid bash "$DIR/river-watcher.sh" >> "$DIR/supervisor.log" 2>&1 < /dev/null &
sleep 1
echo "started (supervisor pid $(cat "$PIDF" 2>/dev/null || echo $!))"
