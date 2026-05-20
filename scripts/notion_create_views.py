"""Idempotent bootstrap for custom Notion mirror views (Phase 1B.5).

Phase 1B.1's bootstrap created the four mirror databases with just the
default table view. This script iterates the view specs declared in
``notion/views.py`` and creates any that don't yet exist (matched on
``name``).

Idempotency: per-DB it lists existing views and only creates a spec whose
name isn't already present. Safe to re-run.

Per-view try/except: a malformed payload (the issue calls out
``board.group_by`` / ``calendar.date_property_id`` / smart-filter shapes as
not fully documented for ``/v1/views``) logs an error and the loop moves on
— one bad spec never blocks the rest.

Usage:
    ./venv/bin/python scripts/notion_create_views.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from notion import views as views_mod  # noqa: E402
from notion.client import NotionClient, NotionError  # noqa: E402

# ---------------- result rows ----------------


def _existing_view_names(client: NotionClient, database_id: str) -> set[str]:
    """Return the set of view names already present on the database. Empty
    on API failure so we fall back to "try-and-let-Notion-reject" instead
    of pretending the DB has no views."""
    try:
        resp = client.list_views(database_id)
    except NotionError as e:
        print(f"  warn  could not list existing views ({e}); proceeding without dedupe")
        return set()
    names: set[str] = set()
    for v in resp.get("results", []):
        name = v.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def _create_for_db(client: NotionClient, db_views: views_mod.DBViews) -> dict[str, int]:
    """Iterate specs for one DB. Returns a tally of created/skipped/errored."""
    database_id = os.getenv(db_views.db_id_env, "")
    data_source_id = os.getenv(db_views.ds_id_env, "")
    if not database_id or not data_source_id:
        print(f"  skip  {db_views.db_title}: {db_views.db_id_env}/{db_views.ds_id_env} not set in env")
        return {"created": 0, "skipped": len(db_views.specs), "errored": 0}

    print(f"\n{db_views.db_title} (database={database_id}):")
    existing = _existing_view_names(client, database_id)

    tally = {"created": 0, "skipped": 0, "errored": 0}
    for spec in db_views.specs:
        if spec.name in existing:
            print(f"  skip   {spec.name}: already present")
            tally["skipped"] += 1
            continue
        # Per-view guard: one bad payload (likely the speculative ones —
        # board.group_by, calendar.date_property_id, smart-filter operators)
        # must not block the rest.
        try:
            client.create_view(
                database_id=database_id,
                data_source_id=data_source_id,
                **spec.to_create_kwargs(),
            )
        except NotionError as e:
            marker = " (speculative payload — needs verification)" if spec.speculative else ""
            print(f"  ERROR  {spec.name}: {e}{marker}")
            if spec.notes:
                print(f"         note: {spec.notes}")
            tally["errored"] += 1
            continue
        except Exception as e:  # noqa: BLE001 — best-effort; never let one spec kill the run
            print(f"  ERROR  {spec.name}: unexpected {type(e).__name__}: {e}")
            tally["errored"] += 1
            continue
        marker = " (speculative)" if spec.speculative else ""
        print(f"  create {spec.name}{marker}")
        tally["created"] += 1
    return tally


def create_views(client: NotionClient | None = None) -> dict[str, int]:
    """Iterate every DB in the registry. Returns aggregated tally."""
    client = client or NotionClient()
    totals = {"created": 0, "skipped": 0, "errored": 0}
    for db_views in views_mod.REGISTRY:
        sub = _create_for_db(client, db_views)
        for k, v in sub.items():
            totals[k] += v
    return totals


def main() -> int:
    load_dotenv()
    if not os.getenv("NOTION_TOKEN"):
        print("error: NOTION_TOKEN must be set in .env", file=sys.stderr)
        return 1

    totals = create_views()
    print(f"\ndone: {totals['created']} created, {totals['skipped']} skipped, {totals['errored']} errored")
    # Non-zero exit only on hard config error; per-view errors are logged
    # but expected for the speculative payloads until they're verified.
    return 0


if __name__ == "__main__":
    sys.exit(main())
