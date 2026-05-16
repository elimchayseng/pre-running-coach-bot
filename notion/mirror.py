"""One-way mirror of SQLite session rows into the PRE Sessions Notion DB.

SQLite is the source of truth. Every mirror write is best-effort and runs in
a daemon thread (``mirror_session`` / ``mirror_sessions``): a Notion outage,
a bad token, or a rate-limit storm logs a warning and is dropped — it never
breaks a bot turn or a webhook.

Idempotency: every Notion page carries a hidden ``source_key`` (``sid:{id}``).
The upsert queries on it — hit → update, miss → insert — so re-running the
seed or re-mirroring the same row never duplicates.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Optional

from . import schema
from .client import NotionClient
from .markdown import _session_data, render_session_body

logger = logging.getLogger("pre_coach.notion.mirror")

# Notion caps a single rich-text object at 2000 characters.
_TEXT_CAP = 2000
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Serializes all mirror upserts. The query-then-insert in _upsert_session is
# not atomic at the Notion API (no uniqueness constraint on source_key), so
# two daemon threads mirroring the same row could both miss the query and
# double-insert a page. A single global lock is fine here: mirror writes are
# best-effort and off every request path, so serializing them is invisible.
_upsert_lock = threading.Lock()


def enabled() -> bool:
    """True when the mirror is fully configured. Every public entry point
    short-circuits on this, so the bot runs untouched without Notion."""
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_SESSIONS_DS_ID"))


# ---------- property-value builders ----------
#
# Every builder always emits its property (even for an empty source value) so
# an UPDATE faithfully clears a value that was removed in SQLite.


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": _BOLD_RE.sub(r"\1", text)[:_TEXT_CAP]}}]}


def _rich(text: Optional[str]) -> dict:
    text = (text or "").strip()
    return {"rich_text": [{"text": {"content": text[:_TEXT_CAP]}}] if text else []}


def _select(value: Optional[str]) -> dict:
    return {"select": {"name": str(value)} if value else None}


def _number(value: Any) -> dict:
    return {"number": value if isinstance(value, (int, float)) else None}


def _url(value: Optional[str]) -> dict:
    return {"url": value or None}


def _session_properties(row: dict, source_key: str) -> dict:
    """Map a SQLite sessions row to PRE Sessions Notion properties."""
    data = _session_data(row)
    details = data.get("details") or {}
    label = row.get("prescribed_workout") or row.get("type") or "session"
    sid = details.get("strava_id")
    props = {
        "Title": _title(f"{row['date']} — {label}"),
        "Date": {"date": {"start": row["date"]}},
        "Slot": _select(row.get("slot")),
        "Status": _select(row.get("status")),
        "Type": _select(row.get("type")),
        "Prescribed": _rich(row.get("prescribed_workout")),
        "Pace target": _rich(row.get("prescribed_pace")),
        "Plan notes": _rich(row.get("prescribed_notes")),
        "Miles": _number(data.get("miles")),
        "Avg pace": _rich(data.get("pace_avg") or data.get("pace")),
        "Avg HR": _number(data.get("hr_avg") or data.get("avg_hr")),
        "Strava ID": _number(sid),
        "Strava URL": _url(f"https://www.strava.com/activities/{sid}" if sid else None),
        "Coach notes": _rich(data.get("notes")),
        schema.SOURCE_KEY: _rich(source_key),
    }
    return props


# ---------- upsert ----------


def _query_page_id(client: NotionClient, data_source_id: str, source_key: str) -> Optional[str]:
    res = client.query_data_source(
        data_source_id,
        {"property": schema.SOURCE_KEY, "rich_text": {"equals": source_key}},
    )
    results = res.get("results", [])
    return results[0]["id"] if results else None


def _upsert_session(row: dict, client: NotionClient) -> None:
    """Insert or update the PRE Sessions page for one SQLite session row.

    The query → insert/update is held under ``_upsert_lock`` so two threads
    mirroring the same source_key can't both miss the query and create
    duplicate pages. Payload building is pure and stays outside the lock.
    """
    data_source_id = os.environ["NOTION_SESSIONS_DS_ID"]
    source_key = schema.session_key(row["id"])
    props = _session_properties(row, source_key)
    body = render_session_body(row)
    with _upsert_lock:
        page_id = _query_page_id(client, data_source_id, source_key)
        if page_id:
            client.update_page(page_id, properties=props)
            # Always sync the body, even to empty, so a removed detail is cleared.
            client.replace_page_markdown(page_id, body or "")
        else:
            client.create_page(data_source_id, props, markdown=body)


# ---------- fire-and-forget entry points ----------


def mirror_session(row: dict) -> None:
    """Mirror one session row to Notion in a daemon thread (best-effort)."""
    if row:
        _spawn(_mirror_batch, [row])


def mirror_sessions(rows: list[dict]) -> None:
    """Mirror several session rows to Notion in one daemon thread."""
    rows = [r for r in (rows or []) if r]
    if rows:
        _spawn(_mirror_batch, rows)


def _mirror_batch(rows: list[dict]) -> None:
    client = NotionClient()
    for row in rows:
        try:
            _upsert_session(row, client)
        except Exception as e:  # noqa: BLE001 — one bad row must not drop the rest
            logger.warning("Notion mirror failed for session id=%s: %s", row.get("id"), e)


def _spawn(fn: Any, *args: Any) -> None:
    if not enabled():
        return
    threading.Thread(target=_guard, args=(fn, *args), daemon=True).start()


def _guard(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 — the mirror must never crash a caller
        logger.warning("Notion mirror thread failed: %s", e)
