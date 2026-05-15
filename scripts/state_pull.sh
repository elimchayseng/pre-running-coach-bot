#!/usr/bin/env bash
# Pull the prod coach.db down to a local file for inspection / QA.
#
# Requires the Railway CLI (`railway` on PATH) and a linked project.
# The volume is mounted at /app/data/ in prod.
#
# The prod DB runs in WAL mode and `railway ssh` allocates a PTY, so a naive
# `cat coach.db` both (a) misses un-checkpointed pages still in the -wal file
# and (b) corrupts the binary — the PTY translates every 0x0A byte to 0x0D0A,
# which shifts the file and yields "database disk image is malformed".
#
# Instead we run a tiny Python snippet on prod: it takes a consistent sqlite
# backup (checkpoints the WAL) and base64-encodes it. base64 is PTY-safe, and
# marker tokens let us strip any SSH banner noise around the payload.
#
# Usage:
#   ./scripts/state_pull.sh                       # writes ./prod-coach.db
#   ./scripts/state_pull.sh -o /tmp/coach.db      # custom output
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

# Runs on prod: consistent backup → base64, wrapped in marker tokens.
REMOTE_PY='
import sqlite3, base64, sys, os
snap = "/tmp/_state_pull_snap.db"
if os.path.exists(snap):
    os.remove(snap)
src = sqlite3.connect("/app/data/coach.db")
dst = sqlite3.connect(snap)
src.backup(dst)
dst.close()
src.close()
blob = open(snap, "rb").read()
os.remove(snap)
sys.stdout.write("B64START" + base64.b64encode(blob).decode() + "B64END")
'

# Ship the snippet itself base64-encoded so no quoting survives the round trip
# through the local shell, the railway CLI, and the remote shell.
SCRIPT_B64=$(printf '%s' "$REMOTE_PY" | base64 | tr -d '\n')
RAW=$(railway ssh "python3 -c \"import base64;exec(base64.b64decode('$SCRIPT_B64').decode())\"")

case "$RAW" in
  *B64START*B64END*) : ;;
  *)
    echo "error: no payload received from prod. Raw output:" >&2
    echo "$RAW" >&2
    exit 1
    ;;
esac

PAYLOAD=${RAW#*B64START}
PAYLOAD=${PAYLOAD%B64END*}
# base64 decode tolerates the PTY's stray \r — they're not in the alphabet.
printf '%s' "$PAYLOAD" \
  | python3 -c "import sys,base64; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read()))" \
  > "$OUT"

SIZE=$(wc -c < "$OUT")
echo "Wrote $OUT ($SIZE bytes)"

if python3 -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('SELECT count(*) FROM sqlite_master').fetchone()" "$OUT" 2>/dev/null; then
  echo "Integrity: OK"
else
  echo "error: pulled file is not a valid SQLite database" >&2
  exit 1
fi

echo
echo "Try:  sqlite3 $OUT 'SELECT date, type FROM sessions ORDER BY date DESC LIMIT 20'"
