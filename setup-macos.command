#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'Install uv from https://docs.astral.sh/uv/ and rerun this setup.'
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  printf '%s\n' 'Install Node.js LTS (which includes npm) and rerun this setup.'
  exit 1
fi

uv sync --inexact --extra dev --extra minilm-macos
npm ci
npm run build
printf '%s\n' 'Blot setup is complete. Open launch-macos.command to start the local app.'
