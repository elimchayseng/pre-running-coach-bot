"""One-shot bootstrap for the Notion mirror (Phase 1B.1).

Creates the five mirror databases under the PRE Training parent page:
PRE Sessions, PRE Journal, PRE Plan Changes, PRE Reviews, PRE Health.
Sessions is created first so Plan Changes and Reviews can declare a relation
into it.

Idempotent: a database whose exact title already exists under the parent page
is reused, not recreated. Safe to re-run.

After a run it prints the database + data-source IDs as ready-to-paste .env
lines. Drop those into .env so the mirror (Phase 1B.2) and health check can
find the databases.

Usage:
    ./venv/bin/python scripts/notion_bootstrap.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from notion import schema  # noqa: E402
from notion.client import NotionClient, NotionError  # noqa: E402


def _find_database(client: NotionClient, title: str, parent_page_id: str) -> tuple[str, str] | None:
    """Return (database_id, data_source_id) for an existing DB with this exact
    title under the parent page, or None. Used for idempotency.

    Search returns ``data_source`` objects (the 2026-03-11 model). Each carries
    its database id in ``parent.database_id`` and the database's own page
    parent in ``database_parent``.
    """
    results = client.search(query=title).get("results", [])
    for obj in results:
        if obj.get("object") != "data_source":
            continue
        if obj.get("in_trash"):
            continue
        if _plain_title(obj.get("title", [])) != title:
            continue
        db_parent = obj.get("database_parent") or {}
        if _norm(db_parent.get("page_id")) != _norm(parent_page_id):
            continue
        database_id = (obj.get("parent") or {}).get("database_id")
        if database_id:
            return database_id, obj["id"]
    return None


def _plain_title(title_rich_text: list) -> str:
    return "".join(t.get("plain_text") or t.get("text", {}).get("content", "") for t in title_rich_text)


def _norm(notion_id: str | None) -> str:
    """Notion ids round-trip with or without dashes — compare normalized."""
    return (notion_id or "").replace("-", "")


def _create_or_reuse(client: NotionClient, title: str, properties: dict, parent_page_id: str) -> dict:
    existing = _find_database(client, title, parent_page_id)
    if existing:
        db_id, ds_id = existing
        _patch_missing_properties(client, ds_id, title, properties)
        print(f"  reuse  {title}: database={db_id}")
        return {"title": title, "database_id": db_id, "data_source_id": ds_id, "created": False}
    db = client.create_database(parent_page_id, title, properties)
    ds_id = (db.get("data_sources") or [{}])[0].get("id", "")
    print(f"  create {title}: database={db['id']}")
    return {"title": title, "database_id": db["id"], "data_source_id": ds_id, "created": True}


def _patch_missing_properties(client: NotionClient, ds_id: str, title: str, properties: dict) -> None:
    """Add schema properties that don't exist yet on a reused data source.

    Without this, a property added to the code schema after a database was
    first created (e.g. Reviews' "Kind") never reaches existing deployments,
    and every mirror upsert 400s — swallowed per-row in the daemon threads,
    so the mirror dies silently. Additive only: existing properties are
    never modified or removed.
    """
    try:
        current = client.retrieve_data_source(ds_id).get("properties", {})
        missing = {name: spec for name, spec in properties.items() if name not in current}
        if missing:
            client.update_data_source(ds_id, missing)
            print(f"  patch  {title}: added properties {sorted(missing)}")
    except Exception as e:  # noqa: BLE001 — bootstrap stays usable if patching fails
        print(f"  warn   {title}: could not patch properties: {e}")


def bootstrap() -> list[dict]:
    parent_page_id = os.getenv("NOTION_PARENT_PAGE_ID")
    if not os.getenv("NOTION_TOKEN") or not parent_page_id:
        raise SystemExit("error: NOTION_TOKEN and NOTION_PARENT_PAGE_ID must be set in .env")

    client = NotionClient()
    me = client.users_me()
    print(f"integration: {me.get('name')} ({me.get('id')})")
    print(f"parent page: {parent_page_id}\n")

    out: list[dict] = []
    # Sessions first — Plan Changes and Reviews relate into its data source.
    sessions = _create_or_reuse(client, schema.DB_SESSIONS, schema.SESSIONS_PROPERTIES, parent_page_id)
    out.append(sessions)
    out.append(_create_or_reuse(client, schema.DB_JOURNAL, schema.JOURNAL_PROPERTIES, parent_page_id))
    out.append(
        _create_or_reuse(
            client,
            schema.DB_PLAN_CHANGES,
            schema.plan_changes_properties(sessions["data_source_id"]),
            parent_page_id,
        )
    )
    out.append(
        _create_or_reuse(
            client,
            schema.DB_REVIEWS,
            schema.reviews_properties(sessions["data_source_id"]),
            parent_page_id,
        )
    )
    # Health has no relations — order-independent.
    out.append(_create_or_reuse(client, schema.DB_HEALTH, schema.HEALTH_PROPERTIES, parent_page_id))
    return out


_ENV_KEYS = {
    schema.DB_SESSIONS: ("NOTION_SESSIONS_DB_ID", "NOTION_SESSIONS_DS_ID"),
    schema.DB_JOURNAL: ("NOTION_JOURNAL_DB_ID", "NOTION_JOURNAL_DS_ID"),
    schema.DB_PLAN_CHANGES: ("NOTION_PLAN_CHANGES_DB_ID", "NOTION_PLAN_CHANGES_DS_ID"),
    schema.DB_REVIEWS: ("NOTION_REVIEWS_DB_ID", "NOTION_REVIEWS_DS_ID"),
    schema.DB_HEALTH: ("NOTION_HEALTH_DB_ID", "NOTION_HEALTH_DS_ID"),
}


def main() -> int:
    load_dotenv()
    try:
        results = bootstrap()
    except NotionError as e:
        print(f"error: Notion API call failed: {e}", file=sys.stderr)
        return 1

    print("\n--- add these to .env ---")
    for r in results:
        db_key, ds_key = _ENV_KEYS[r["title"]]
        print(f"{db_key}={r['database_id']}")
        print(f"{ds_key}={r['data_source_id']}")
    created = sum(1 for r in results if r["created"])
    print(f"\ndone: {created} created, {len(results) - created} reused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
