#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
HOST="127.0.0.1"
PORT="${VIVIEEN_PORT:-8777}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/vivieen"
LOG="$STATE_DIR/backend.log"
PID_FILE="$STATE_DIR/backend.pid"

cd "$ROOT"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

if curl --fail --silent "http://$HOST:$PORT/api/meta" | grep -q 'com.vivieen.companion'; then
  echo "Vivieen is already running at http://$HOST:$PORT"
  open "http://$HOST:$PORT"
  exit 0
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is occupied by another process; set VIVIEEN_PORT to a free port" >&2
  exit 1
fi

nohup .venv/bin/python -B -W ignore -m uvicorn server.app:app \
  --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
printf '%s\n' "$!" >"$PID_FILE"
chmod 600 "$LOG" "$PID_FILE"

for _ in $(seq 1 120); do
  if curl --fail --silent "http://$HOST:$PORT/api/meta" | grep -q 'com.vivieen.companion'; then
    echo "ready -> http://$HOST:$PORT"
    open "http://$HOST:$PORT"
    exit 0
  fi
  if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Vivieen failed to start; see $LOG" >&2
    exit 1
  fi
  sleep 0.5
done

echo "Vivieen did not become ready; see $LOG" >&2
exit 1
