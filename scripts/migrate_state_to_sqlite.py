"""One-time seed of ``state/coach.db`` from the legacy ``state/*`` files.

Default behaviour is **non-destructive**:
  - Singleton rows (plan, plan_changelog, journal, athlete) are upserted —
    safe to re-run since they replace in place.
  - Session inserts catch ``sqlite3.IntegrityError`` so the partial UNIQUE
    index on ``details.strava_id`` dedupes Strava-sourced rows.
  - Non-Strava sessions (weekly_summary, manual log_session entries with no
    strava_id) are inserted blindly. Re-running would accumulate duplicates.

Pass ``--reset`` to wipe the ``sessions`` table before seeding. **Use this
only on the initial seed** — running ``--reset`` against a DB that has
accepted webhook writes will destroy them.

Usage:
    # First-time seed (clean DB):
    ./venv/bin/python scripts/migrate_state_to_sqlite.py --reset

    # Topping up later from new committed seed files (rarely needed once the
    # bot is writing to the DB directly — the UNIQUE index handles dedup):
    ./venv/bin/python scripts/migrate_state_to_sqlite.py

On Railway prod (volume mounted at /app/data, initial seed):
    railway shell --service web
    python scripts/migrate_state_to_sqlite.py /app/state --db /app/data/coach.db --reset
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


def _safe_load(text: str) -> dict:
    """Parse a JSON blob from the sessions table; return {} on garbage."""
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}


def migrate(seed_dir: Path, db_path: Path, reset_sessions: bool = False) -> dict:
    """Seed ``db_path`` from the legacy files under ``seed_dir``.

    If ``reset_sessions`` is True, ``DELETE FROM sessions`` runs before the
    log import — destructive, intended for the initial seed only.

    Returns a small summary dict for the caller to print.
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
        "sessions_reset": False,
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
    # Default: non-destructive. Strava-sourced rows dedupe via the UNIQUE
    # index; non-Strava rows (weekly_summary, manual entries) are skipped if
    # an identical (date, type, data) row already exists. Only `--reset`
    # wipes the table — see the module docstring for why this matters.
    log_path = seed_dir / "log.jsonl"
    if log_path.exists():
        if reset_sessions:
            with sm._conn() as c:
                c.execute("DELETE FROM sessions")
            summary["sessions_reset"] = True
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
                # Non-strava entries: skip if an identical row already exists.
                # The UNIQUE index catches strava_id dupes; this catches the
                # weekly_summary case the index can't see. Compare via parsed
                # dict equality so JSON-formatting differences (key order,
                # whitespace) don't cause false misses.
                if not (entry.get("details") or {}).get("strava_id"):
                    with sm._conn() as c:
                        candidates = c.execute(
                            "SELECT data FROM sessions WHERE date = ? AND type = ?",
                            (entry["date"], entry.get("type", "")),
                        ).fetchall()
                    duplicate = any(_safe_load(row["data"]) == entry for row in candidates)
                    if duplicate:
                        summary["sessions_skipped"] += 1
                        continue
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
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Wipe the sessions table before seeding. DESTRUCTIVE — only use "
            "this on the initial seed. Running --reset against a DB that has "
            "accepted webhook writes will destroy them."
        ),
    )
    args = parser.parse_args()

    seed_dir = Path(args.seed_dir)
    if not seed_dir.is_dir():
        print(f"error: seed directory not found: {seed_dir}", file=sys.stderr)
        return 1
    db_path = Path(args.db) if args.db else (seed_dir / "coach.db")

    print(f"seeding from: {seed_dir}")
    print(f"writing to:   {db_path}")
    if args.reset:
        print("mode:         --reset (sessions table will be cleared)")
    summary = migrate(seed_dir, db_path, reset_sessions=args.reset)
    print("done:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
