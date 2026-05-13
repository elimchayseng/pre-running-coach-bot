#!/usr/bin/env bash
# Pull the prod coach.db down to a local file for inspection / QA.
#
# Requires the Railway CLI (`railway` on PATH) and a linked project.
# The volume is mounted at /app/data/ in prod.
#
# Usage:
#   ./scripts/state_pull.py                       # writes ./prod-coach.db
#   ./scripts/state_pull.py -o /tmp/coach.db      # custom output
#
# After pulling, inspect with:
#   sqlite3 prod-coach.db 'SELECT date, type FROM sessions ORDER BY date DESC LIMIT 20'
#   python scripts/state_dump.py log --db prod-coach.db
#   # or open prod-coach.db in TablePlus / Postico / DBeaver

set -euo pipefail

OUT="prod-coach.db"
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--out) OUT="$2"; shift 2;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

if ! command -v railway >/dev/null 2>&1; then
  echo "error: railway CLI not found on PATH" >&2
  echo "install: https://docs.railway.com/develop/cli" >&2
  exit 1
fi

echo "Pulling /app/data/coach.db -> $OUT ..."
railway ssh "cat /app/data/coach.db" > "$OUT"
SIZE=$(wc -c < "$OUT")
echo "Wrote $OUT ($SIZE bytes)"
echo
echo "Try:  sqlite3 $OUT 'SELECT date, type FROM sessions ORDER BY date DESC LIMIT 20'"
