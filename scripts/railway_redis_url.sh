#!/usr/bin/env bash
# Print the Railway Redis public URL (REDIS_PUBLIC_URL) for the one-command
# prod re-auth / status Make targets. Exits non-zero with a clear message if
# the railway CLI is missing, not logged in / linked, or the variable can't be
# found. Shared by gcal-reauth-prod, gcal-status-prod, coros-reauth-prod, and
# coros-status-prod — the railway preamble + extraction was copy-pasted 4x
# before (issue #57).
set -euo pipefail

command -v railway >/dev/null 2>&1 || {
  echo "ERROR: railway CLI not found. Install: https://docs.railway.app/guides/cli" >&2
  exit 1
}
railway whoami >/dev/null 2>&1 || {
  echo "ERROR: railway not logged in / linked. Run: railway login && railway link" >&2
  exit 1
}

railway variables --service Redis --json | ./venv/bin/python -c \
  'import sys,json; u=json.load(sys.stdin).get("REDIS_PUBLIC_URL"); sys.exit("ERROR: REDIS_PUBLIC_URL not found on the Redis service") if not u else print(u)'
