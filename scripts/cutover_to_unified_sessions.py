"""Phase 1A.2 cutover: migrate an existing DB to the unified `sessions` table.

Idempotent. The Flask app runs this at startup (see app._run_startup_cutover)
before serving traffic; it can also be run by hand via
`railway run python scripts/cutover_to_unified_sessions.py`.

Pre-cutover shape (schema v3):
    sessions   — completed-only rows (the old log)
    plan       — markdown blob
    sessions_v2, plan_meta — dormant, populated here

Post-cutover shape (schema v4):
    sessions            — unified plan-as-rows (the old sessions_v2)
    sessions_v1_archive — the old completed-only `sessions`
    plan_archive        — the old `plan` blob
    plan_meta           — plan prose

Steps for a pre-cutover DB:
    1. Ensure sessions_v2 + plan_meta exist.
    2. Populate them from `plan` + `sessions` via the idempotent 1A.1
       migration (scripts/migrate_plan_to_sessions.py).
    3. Rename sessions -> sessions_v1_archive, plan -> plan_archive,
       sessions_v2 -> sessions; normalize index names.
    4. Record schema_version 4.

A DB whose `sessions` table already has a `status` column is already cut
over — the script records v4 if missing and exits.

Usage:
    ./venv/bin/python scripts/cutover_to_unified_sessions.py
    ./venv/bin/python scripts/cutover_to_unified_sessions.py --db /path/to.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_plan_to_sessions import migrate  # noqa: E402

# DDL for the dormant tables, in case the DB predates the 1A.1 deploy.
_DORMANT_DDL = """
CREATE TABLE IF NOT EXISTS sessions_v2 (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT    NOT NULL,
    slot               TEXT,
    status             TEXT    NOT NULL
                       CHECK (status IN ('planned','completed','missed','off-plan')),
    type               TEXT,
    prescribed_workout TEXT,
    prescribed_pace    TEXT,
    prescribed_notes   TEXT,
    detail_md          TEXT,
    data               TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at       TEXT,
    UNIQUE (date, slot)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_v2_strava_id
    ON sessions_v2 (json_extract(data, '$.details.strava_id'))
    WHERE json_extract(data, '$.details.strava_id') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_v2_date_status ON sessions_v2 (date, status);
CREATE TABLE IF NOT EXISTS plan_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def cutover(db_path: Path) -> dict:
    summary = {"already_cut_over": False, "migrate": None, "renamed": [], "schema_version": 4}

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")

        # Already cut over? The unified `sessions` table has a `status` column.
        if _has_column(conn, "sessions", "status"):
            summary["already_cut_over"] = True
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
                )
                conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")
            return summary

        old_session_count = (
            conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] if _table_exists(conn, "sessions") else 0
        )

        # Step 1: ensure the dormant tables exist (DB may predate the 1A.1 deploy).
        with conn:
            conn.executescript(_DORMANT_DDL)
    finally:
        conn.close()

    # Step 2: populate sessions_v2 + plan_meta (idempotent).
    summary["migrate"] = migrate(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        v2_count = conn.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0]
        if old_session_count > 0 and v2_count == 0:
            raise RuntimeError(
                f"refusing to rename: old sessions had {old_session_count} rows "
                f"but sessions_v2 is empty after migration"
            )

        # Step 3: rename in a single transaction.
        with conn:
            if _table_exists(conn, "sessions"):
                conn.execute("ALTER TABLE sessions RENAME TO sessions_v1_archive")
                summary["renamed"].append("sessions -> sessions_v1_archive")
            if _table_exists(conn, "plan"):
                conn.execute("ALTER TABLE plan RENAME TO plan_archive")
                summary["renamed"].append("plan -> plan_archive")
            conn.execute("ALTER TABLE sessions_v2 RENAME TO sessions")
            summary["renamed"].append("sessions_v2 -> sessions")
            # Normalize index names so a migrated DB matches a fresh one.
            # Index names are global in SQLite: the old `sessions` indexes
            # followed the table into sessions_v1_archive and would otherwise
            # shadow the new names via CREATE INDEX IF NOT EXISTS.
            for stale in (
                "idx_sessions_date",  # old completed-only `sessions`
                "idx_sessions_strava_id",  # old completed-only `sessions`
                "idx_sessions_v2_strava_id",  # dormant sessions_v2
                "idx_sessions_v2_date_status",  # dormant sessions_v2
            ):
                conn.execute(f"DROP INDEX IF EXISTS {stale}")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_strava_id "
                "ON sessions (json_extract(data, '$.details.strava_id')) "
                "WHERE json_extract(data, '$.details.strava_id') IS NOT NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_date_status ON sessions (date, status)")
            # Step 4: record the new schema version.
            conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")
    finally:
        conn.close()

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite path (default: $DATABASE_PATH or state/coach.db)")
    args = parser.parse_args()
    db_path = Path(args.db) if args.db else Path(os.getenv("DATABASE_PATH") or (ROOT / "state" / "coach.db"))

    if not db_path.exists():
        # Nothing to cut over — a fresh DB gets the v4 schema directly on first
        # StateManager connect. Not an error (release step on a clean install).
        print(f"cutover: {db_path} does not exist yet — nothing to migrate")
        return 0

    print(f"cutover: {db_path}")
    summary = cutover(db_path)
    if summary["already_cut_over"]:
        print("already cut over (sessions.status present) — no-op")
        return 0
    print("done:")
    if summary["migrate"]:
        for k, v in summary["migrate"].items():
            if k != "no_op":
                print(f"  migrate.{k}: {v}")
    for r in summary["renamed"]:
        print(f"  renamed: {r}")
    print(f"  schema_version: {summary['schema_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
