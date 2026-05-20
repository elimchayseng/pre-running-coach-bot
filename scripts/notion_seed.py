"""Backfill the Notion mirror from SQLite (Phase 1B.2 + 1B.3).

One-shot: every row in SQLite ``sessions`` is upserted into PRE Sessions, every
journal entry into PRE Journal, every changelog entry into PRE Plan Changes.
Idempotent via ``source_key`` — re-running updates existing pages instead of
duplicating. Each collection is gated on its own ``NOTION_*_DS_ID`` so a
partially-configured workspace seeds just what it can.

Synchronous (unlike the live mirror, which is fire-and-forget): the seed
upserts entry by entry and reports counts so a backfill failure is visible.

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
from notion.client import NotionClient  # noqa: E402
from notion.entries import parse_changelog_entries, parse_journal_entries  # noqa: E402
from state_manager import StateManager  # noqa: E402


def _seed_sessions(sm: StateManager, client: NotionClient) -> dict:
    rows = sm._rows("1 = 1", ())
    summary = {"total": len(rows), "ok": 0, "failed": 0}
    for row in rows:
        try:
            mirror._upsert_session(row, client)
            summary["ok"] += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the backfill
            summary["failed"] += 1
            print(f"  FAIL session id={row['id']} ({row['date']}): {e}", file=sys.stderr)
    return summary


def _read_singleton(sm: StateManager, table: str) -> str:
    with sm._conn() as conn:
        row = conn.execute(f"SELECT content FROM {table} WHERE id = 1").fetchone()  # noqa: S608
    return row["content"] if row else ""


def _seed_journal(sm: StateManager, client: NotionClient) -> dict:
    entries = parse_journal_entries(_read_singleton(sm, "journal"))
    summary = {"total": len(entries), "ok": 0, "failed": 0}
    for e in entries:
        try:
            mirror._upsert_journal_entry(e, client)
            summary["ok"] += 1
        except Exception as ex:  # noqa: BLE001
            summary["failed"] += 1
            print(f"  FAIL journal {e.get('title')!r}: {ex}", file=sys.stderr)
    return summary


def _seed_plan_changes(sm: StateManager, client: NotionClient) -> dict:
    entries = parse_changelog_entries(_read_singleton(sm, "plan_changelog"))
    summary = {"total": len(entries), "ok": 0, "failed": 0}
    for e in entries:
        try:
            mirror._upsert_plan_change(e, client)
            summary["ok"] += 1
        except Exception as ex:  # noqa: BLE001
            summary["failed"] += 1
            print(f"  FAIL plan_change {e.get('timestamp')!r}: {ex}", file=sys.stderr)
    return summary


def _seed_reviews(sm: StateManager, client: NotionClient) -> dict:
    rows = sm.get_all_reviews()
    summary = {"total": len(rows), "ok": 0, "failed": 0}
    for row in rows:
        try:
            mirror._upsert_review(row, client)
            summary["ok"] += 1
        except Exception as ex:  # noqa: BLE001
            summary["failed"] += 1
            print(f"  FAIL review id={row.get('id')}: {ex}", file=sys.stderr)
    return summary


def seed(db_path: Path) -> dict:
    sm = StateManager()
    sm.db_path = db_path
    sm.state_dir = db_path.parent
    sm._schema_applied = False
    client = NotionClient()

    out: dict[str, dict] = {}
    if mirror.enabled():
        print("seeding PRE Sessions ...")
        out["sessions"] = _seed_sessions(sm, client)
    if mirror.journal_enabled():
        print("seeding PRE Journal ...")
        out["journal"] = _seed_journal(sm, client)
    if mirror.plan_changes_enabled():
        print("seeding PRE Plan Changes ...")
        out["plan_changes"] = _seed_plan_changes(sm, client)
    if mirror.reviews_enabled():
        print("seeding PRE Reviews ...")
        out["reviews"] = _seed_reviews(sm, client)
    return out


def main() -> int:
    load_dotenv()
    if not (mirror.enabled() or mirror.journal_enabled() or mirror.plan_changes_enabled() or mirror.reviews_enabled()):
        print(
            "error: at least one of NOTION_SESSIONS_DS_ID / NOTION_JOURNAL_DS_ID / "
            "NOTION_PLAN_CHANGES_DS_ID / NOTION_REVIEWS_DS_ID must be set in .env "
            "(with NOTION_TOKEN)",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite path (default: $DATABASE_PATH or state/coach.db)")
    args = parser.parse_args()
    db_path = Path(args.db) if args.db else Path(os.getenv("DATABASE_PATH") or (ROOT / "state" / "coach.db"))
    if not db_path.exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        return 1

    print(f"backfilling Notion from {db_path}\n")
    results = seed(db_path)
    failed_any = False
    for kind, s in results.items():
        print(f"{kind}: {s['ok']}/{s['total']} mirrored, {s['failed']} failed")
        if s["failed"]:
            failed_any = True
    return 1 if failed_any else 0


if __name__ == "__main__":
    sys.exit(main())
