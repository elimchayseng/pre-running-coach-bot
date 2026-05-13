"""Print the contents of a state table from coach.db for inspection.

Replaces ``cat state/log.jsonl`` etc. now that state lives in SQLite. Reads
the same DB file the bot does — local default ``state/coach.db``, or set
``DATABASE_PATH``.

Usage:
    python scripts/state_dump.py log                  # JSONL of sessions
    python scripts/state_dump.py log --since 2026-05-01
    python scripts/state_dump.py plan                 # plan markdown
    python scripts/state_dump.py athlete              # athlete YAML
    python scripts/state_dump.py journal              # journal markdown
    python scripts/state_dump.py gcal_sync            # gcal sync state JSON
    python scripts/state_dump.py --all                # everything, labeled
    python scripts/state_dump.py log --db /tmp/coach.db   # custom DB path

Reading prod via railway CLI (no script changes needed):
    railway ssh "sqlite3 /app/data/coach.db 'SELECT date, type FROM sessions ORDER BY date DESC LIMIT 20'"

Or pull a snapshot down and use this script against it:
    railway ssh "cat /app/data/coach.db" > /tmp/prod-coach.db
    python scripts/state_dump.py log --db /tmp/prod-coach.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KNOWN = {"log", "plan", "plan_changelog", "athlete", "journal", "gcal_sync", "all"}


def _resolve_db(arg_db: str | None) -> Path:
    if arg_db:
        return Path(arg_db)
    env = os.getenv("DATABASE_PATH")
    if env:
        return Path(env)
    return ROOT / "state" / "coach.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def dump_log(conn: sqlite3.Connection, since: str | None) -> None:
    if since:
        rows = conn.execute("SELECT data FROM sessions WHERE date >= ? ORDER BY date, id", (since,)).fetchall()
    else:
        rows = conn.execute("SELECT data FROM sessions ORDER BY date, id").fetchall()
    for r in rows:
        print(r["data"])


def dump_singleton(conn: sqlite3.Connection, table: str, column: str = "content") -> None:
    row = conn.execute(f"SELECT {column} FROM {table} WHERE id = 1").fetchone()
    if row is not None:
        print(row[column])


def dump_gcal_sync(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT event_id, hash, last_synced_at, completed, last_completed_at, off_plan "
        "FROM gcal_sync_state ORDER BY event_id"
    ).fetchall()
    out = {}
    for r in rows:
        entry = {}
        if r["hash"] is not None:
            entry["hash"] = r["hash"]
        if r["last_synced_at"] is not None:
            entry["last_synced_at"] = r["last_synced_at"]
        if r["completed"]:
            entry["completed"] = True
        if r["last_completed_at"] is not None:
            entry["last_completed_at"] = r["last_completed_at"]
        if r["off_plan"]:
            entry["off_plan"] = True
        out[r["event_id"]] = entry
    print(json.dumps(out, indent=2, sort_keys=True))


def dump_all(conn: sqlite3.Connection) -> None:
    for label, fn in [
        ("athlete.yaml", lambda: dump_singleton(conn, "athlete", "yaml_text")),
        ("plan.md", lambda: dump_singleton(conn, "plan")),
        ("plan_changelog.md", lambda: dump_singleton(conn, "plan_changelog")),
        ("journal.md", lambda: dump_singleton(conn, "journal")),
        ("log.jsonl", lambda: dump_log(conn, None)),
        (".gcal_sync_state.json", lambda: dump_gcal_sync(conn)),
    ]:
        print(f"\n=== {label} ===")
        fn()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "table",
        nargs="?",
        choices=sorted(KNOWN),
        help="Which state to dump (or --all).",
    )
    parser.add_argument("--all", action="store_true", help="Dump every table.")
    parser.add_argument("--since", help="Filter sessions to date >= YYYY-MM-DD (log table only).")
    parser.add_argument("--db", help="Path to the DB file (default: $DATABASE_PATH or state/coach.db).")
    args = parser.parse_args()

    if not args.table and not args.all:
        parser.print_help(sys.stderr)
        return 1

    db_path = _resolve_db(args.db)
    conn = _connect(db_path)
    try:
        if args.all or args.table == "all":
            dump_all(conn)
        elif args.table == "log":
            dump_log(conn, args.since)
        elif args.table == "plan":
            dump_singleton(conn, "plan")
        elif args.table == "plan_changelog":
            dump_singleton(conn, "plan_changelog")
        elif args.table == "athlete":
            dump_singleton(conn, "athlete", "yaml_text")
        elif args.table == "journal":
            dump_singleton(conn, "journal")
        elif args.table == "gcal_sync":
            dump_gcal_sync(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
