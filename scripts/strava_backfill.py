"""One-shot Strava backfill: pull recent activities, append to log.jsonl.

Idempotent — skips activities whose `details.strava_id` already appears in the
log. Always fetches the full activity detail (laps + splits + best_efforts)
since the list endpoint returns summaries only.

Usage:
    ./venv/bin/python scripts/strava_backfill.py                  # last 30 days
    ./venv/bin/python scripts/strava_backfill.py --since 7d
    ./venv/bin/python scripts/strava_backfill.py --since 2026-04-01
    ./venv/bin/python scripts/strava_backfill.py --dry-run        # don't write
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from state_manager import StateManager  # noqa: E402
from strava import client, translator  # noqa: E402

DEFAULT_SINCE = "30d"


def _parse_since(s: str) -> int:
    """Return a unix timestamp for `since`. Accepts:
    '30d', '7d'  -> N days ago
    '2026-04-01' -> ISO date
    """
    m = re.match(r"^(\d+)d$", s)
    if m:
        days = int(m.group(1))
        return int(time.time()) - days * 86400
    try:
        d = date.fromisoformat(s)
        return int(datetime(d.year, d.month, d.day).timestamp())
    except ValueError:
        raise SystemExit(f"--since must be 'Nd' or 'YYYY-MM-DD' (got {s!r})")


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill Strava activities into log.jsonl")
    p.add_argument("--since", default=DEFAULT_SINCE, help="time window, e.g. 30d or 2026-04-01")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--state-dir", default=str(ROOT / "state"))
    args = p.parse_args()

    after = _parse_since(args.since)
    since_dt = datetime.fromtimestamp(after).date().isoformat()
    print(f"Fetching activities since {since_dt} (after={after})…")

    state = StateManager(Path(args.state_dir))
    seen = state.existing_strava_ids()
    print(f"  log.jsonl already has {len(seen)} Strava-tagged entries")

    try:
        summaries = client.list_activities(after=after, per_page=30)
    except Exception as e:
        print(f"List activities failed: {e}", file=sys.stderr)
        return 1

    print(f"  Strava returned {len(summaries)} activities")
    if not summaries:
        return 0

    athlete = state.load_athlete()
    hr_zones = (athlete.get("hr_zones") if isinstance(athlete, dict) else None) or {}

    new = 0
    skipped = 0
    failed = 0
    for summary in summaries:
        sid = summary.get("id")
        if sid is None:
            continue
        if int(sid) in seen:
            skipped += 1
            continue
        try:
            full = client.get_activity(int(sid))
            entry = translator.activity_to_log_entry(full, hr_zones=hr_zones)
        except Exception as e:
            failed += 1
            print(f"  ! activity {sid} failed: {e}")
            continue

        if args.dry_run:
            print(
                f"  [dry] would append {entry.get('date')} {entry.get('type')} {entry.get('miles')}mi (strava_id={sid})"
            )
        else:
            state.append_session(entry)
            print(f"  + {entry.get('date')} {entry.get('type')} {entry.get('miles')}mi (strava_id={sid})")
        new += 1
        seen.add(int(sid))

    print(
        f"\nDone. {new} new, {skipped} already-logged, {failed} failed. "
        f"{'(dry-run, no writes)' if args.dry_run else 'Appended to state/log.jsonl.'}"
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
