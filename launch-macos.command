#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1 || [[ ! -d .venv ]]; then
  printf '%s\n' 'Run setup-macos.command first.'
  exit 1
fi

"$ROOT/.venv/bin/python" -m backend.app &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..60}; do
  if curl --silent --fail http://127.0.0.1:8765/api/health >/dev/null; then
    open http://127.0.0.1:8765/obfuscation-workspace.html
    wait "$SERVER_PID"
    exit 0
  fi
  sleep 1
done
printf '%s\n' 'Blot did not start in time. Review the server error above.'
exit 1
