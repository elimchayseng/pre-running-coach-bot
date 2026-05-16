"""Backfill the Notion PRE Sessions mirror from SQLite (Phase 1B.2).

One-shot: every row in the SQLite ``sessions`` table is upserted into the PRE
Sessions Notion database. Idempotent via ``source_key`` — re-running updates
existing pages instead of duplicating.

Synchronous (unlike the live mirror, which is fire-and-forget): the seed
upserts row by row and reports a count, so a backfill failure is visible.

Usage:
    ./venv/bin/python scripts/notion_seed.py
    ./venv/bin/python scripts/notion_seed.py --db /path/to/coach.db
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from notion import mirror  # noqa: E402
from notion.client import NotionClient, NotionError  # noqa: E402
from state_manager import StateManager  # noqa: E402


def seed(db_path: Path) -> dict:
    sm = StateManager()
    sm.db_path = db_path
    sm.state_dir = db_path.parent
    sm._schema_applied = False

    rows = sm._rows("1 = 1", ())
    client = NotionClient()
    summary = {"total": len(rows), "ok": 0, "failed": 0}
    for row in rows:
        try:
            mirror._upsert_session(row, client)
            summary["ok"] += 1
        except NotionError as e:
            summary["failed"] += 1
            print(f"  FAIL session id={row['id']} ({row['date']}): {e}", file=sys.stderr)
    return summary


def main() -> int:
    load_dotenv()
    if not mirror.enabled():
        print("error: NOTION_TOKEN and NOTION_SESSIONS_DS_ID must be set in .env", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite path (default: $DATABASE_PATH or state/coach.db)")
    args = parser.parse_args()
    db_path = Path(args.db) if args.db else Path(os.getenv("DATABASE_PATH") or (ROOT / "state" / "coach.db"))
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1

    print(f"seeding PRE Sessions from {db_path} ...")
    summary = seed(db_path)
    print(f"done: {summary['ok']}/{summary['total']} sessions mirrored, {summary['failed']} failed")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
