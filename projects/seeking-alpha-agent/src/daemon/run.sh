#!/usr/bin/env bash
# Seeking Alpha Agent daemon launcher (macOS / Linux).
#   ./run.sh check              show .env readiness (key names only)
#   ./run.sh smoke              30-second proof: logins, DXLink, chain, VIX, halts, SQLite, Supabase, Telegram
#   ./run.sh session            run today's session (09:20 prep → 09:25 heartbeat → 16:20 report → 16:25 exit)
#   ./run.sh forever            run every trading day (VPS mode)
#   ./run.sh replay <file>      rebuild bars from a recording
#   ./run.sh load-econ <json>   push the hand-maintained macro calendar into saa.calendar_days
# First run creates .venv and installs requirements.txt (needs internet once). Never prints a secret.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ not found. On a Mac: brew install python@3.12   (then run this again)" >&2
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "creating virtualenv with $PY ..."
  "$PY" -m venv .venv
fi
if [ ! -f .venv/.deps-ok ] || [ requirements.txt -nt .venv/.deps-ok ]; then
  echo "installing dependencies ..."
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  touch .venv/.deps-ok
fi
exec .venv/bin/python -m saa_daemon "$@"
