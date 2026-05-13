"""One-time seed of ``state/coach.db`` from the legacy ``state/*`` files.

Idempotent — singleton rows (plan, plan_changelog, journal, athlete) use
INSERT OR REPLACE; sessions inserts catch ``sqlite3.IntegrityError`` so the
partial UNIQUE index on ``details.strava_id`` deduplicates re-runs.

Usage:
    ./venv/bin/python scripts/migrate_state_to_sqlite.py [<seed-dir>] [--db <db-path>]

Defaults: seed-dir = ``state/`` under the repo root, db-path = ``state/coach.db``.

On Railway prod (volume mounted at /app/data):
    railway shell --service web
    python scripts/migrate_state_to_sqlite.py /app/state --db /app/data/coach.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from state_manager import StateManager  # noqa: E402


def migrate(seed_dir: Path, db_path: Path) -> dict:
    """Seed ``db_path`` from the legacy files under ``seed_dir``.

    Returns a small summary dict ({sessions_inserted, sessions_skipped,
    plan, athlete, journal, gcal_sync}) for the caller to print.
    """
    sm = StateManager()
    # Force the StateManager to use our explicit db_path even if DATABASE_PATH
    # is set differently in the shell.
    sm.db_path = db_path
    sm.state_dir = db_path.parent
    sm._schema_applied = False

    summary = {
        "sessions_inserted": 0,
        "sessions_skipped": 0,
        "plan": False,
        "plan_changelog": False,
        "athlete": False,
        "journal": False,
        "gcal_sync_entries": 0,
    }

    # --- athlete.yaml ---
    athlete_path = seed_dir / "athlete.yaml"
    if athlete_path.exists():
        yaml_text = athlete_path.read_text(encoding="utf-8")
        with sm._conn() as c:
            c.execute(
                "INSERT INTO athlete (id, yaml_text) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET yaml_text = excluded.yaml_text, "
                "updated_at = datetime('now')",
                (yaml_text,),
            )
        summary["athlete"] = True

    # --- plan.md ---
    plan_path = seed_dir / "plan.md"
    if plan_path.exists():
        content = plan_path.read_text(encoding="utf-8")
        with sm._conn() as c:
            c.execute(
                "INSERT INTO plan (id, content) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
                "updated_at = datetime('now')",
                (content,),
            )
        summary["plan"] = True

    # --- plan_changelog.md ---
    changelog_path = seed_dir / "plan_changelog.md"
    if changelog_path.exists():
        content = changelog_path.read_text(encoding="utf-8")
        with sm._conn() as c:
            c.execute(
                "INSERT INTO plan_changelog (id, content) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
                "updated_at = datetime('now')",
                (content,),
            )
        summary["plan_changelog"] = True

    # --- journal.md ---
    journal_path = seed_dir / "journal.md"
    if journal_path.exists():
        content = journal_path.read_text(encoding="utf-8")
        with sm._conn() as c:
            c.execute(
                "INSERT INTO journal (id, content) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
                "updated_at = datetime('now')",
                (content,),
            )
        summary["journal"] = True

    # --- log.jsonl ---
    # Clear sessions first so re-running the migration is idempotent. The
    # UNIQUE index on details.strava_id only dedupes Strava-sourced rows;
    # manual weekly_summary entries have no strava_id and would otherwise
    # accumulate on every re-run.
    log_path = seed_dir / "log.jsonl"
    if log_path.exists():
        with sm._conn() as c:
            c.execute("DELETE FROM sessions")
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                summary["sessions_skipped"] += 1
                continue
            if "date" not in entry:
                summary["sessions_skipped"] += 1
                continue
            try:
                sm.append_session(entry)
                summary["sessions_inserted"] += 1
            except sqlite3.IntegrityError:
                # Duplicate strava_id — already inserted in a previous run.
                summary["sessions_skipped"] += 1

    # --- .gcal_sync_state.json ---
    gcal_path = seed_dir / ".gcal_sync_state.json"
    if gcal_path.exists():
        try:
            data = json.loads(gcal_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sm.save_gcal_sync_state(data)
                summary["gcal_sync_entries"] = len(data)
        except json.JSONDecodeError:
            pass

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "seed_dir",
        nargs="?",
        default=str(ROOT / "state"),
        help="Directory containing legacy state files (default: %(default)s)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Destination SQLite path (default: <seed_dir>/coach.db or $DATABASE_PATH)",
    )
    args = parser.parse_args()

    seed_dir = Path(args.seed_dir)
    if not seed_dir.is_dir():
        print(f"error: seed directory not found: {seed_dir}", file=sys.stderr)
        return 1
    db_path = Path(args.db) if args.db else (seed_dir / "coach.db")

    print(f"seeding from: {seed_dir}")
    print(f"writing to:   {db_path}")
    summary = migrate(seed_dir, db_path)
    print("done:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
