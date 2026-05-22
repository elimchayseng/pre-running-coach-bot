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
    """Sessions mirror is fully configured. Every Sessions entry point
    short-circuits on this, so the bot runs untouched without Notion."""
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_SESSIONS_DS_ID"))


def journal_enabled() -> bool:
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_JOURNAL_DS_ID"))


def plan_changes_enabled() -> bool:
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_PLAN_CHANGES_DS_ID"))


def reviews_enabled() -> bool:
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_REVIEWS_DS_ID"))


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


def _date_prop(value: Optional[str]) -> dict:
    return {"date": {"start": value} if value else None}


def _multi_select(values: Optional[list]) -> dict:
    return {"multi_select": [{"name": str(v)} for v in (values or [])]}


def _relation(page_ids: Optional[list]) -> dict:
    return {"relation": [{"id": pid} for pid in (page_ids or [])]}


def journal_source_key(entry: dict) -> str:
    """Stable id for a journal entry. Keys on the ``## header`` text since
    journal entries aren't yet rows with ids in SQLite. As long as the
    runner doesn't rewrite the header line, the key stays stable."""
    return "jid:" + (entry.get("title") or "").strip()


def plan_change_source_key(entry: dict) -> str:
    return "cid:" + (entry.get("timestamp") or "").strip()


def review_source_key(row: dict) -> str:
    return f"rid:{row.get('id')}"


def _journal_properties(entry: dict, source_key: str) -> dict:
    return {
        "Title": _title(entry.get("title") or "(entry)"),
        "Date": _date_prop(entry.get("date")),
        # Sleep / Stress / Tags would require body parsing — left empty for
        # 1B.3 so the mirror stays a faithful, conservative reflection.
        "Sleep hours": _number(None),
        "Stress": _select(None),
        "Tags": _multi_select(None),
        schema.SOURCE_KEY: _rich(source_key),
        "sqlite_id": _rich(None),  # placeholder for the eventual row-ified journal
    }


def _plan_change_properties(entry: dict, source_key: str) -> dict:
    ts = entry.get("timestamp") or ""
    return {
        "Title": _title(ts or "(change)"),
        "Date": _date_prop(ts[:10] if ts else None),
        "Action": _select(entry.get("action") or "planned-edit"),
        "Reason": _rich(entry.get("note")),
        # Triggered-by relation needs the changelog to track which session
        # caused each change — deferred (the changelog blob doesn't carry it).
        "Triggered by": _relation(None),
        schema.SOURCE_KEY: _rich(source_key),
    }


def _review_properties(row: dict, source_key: str, session_page_id: Optional[str] = None) -> dict:
    """Map a SQLite reviews row to PRE Reviews Notion properties.

    ``session_page_id`` is the Notion Sessions page id for the related
    session row; looked up by the mirror via ``sid:<session_id>`` source_key.
    Left empty when the session hasn't been mirrored yet (the next review
    upsert will fill the relation if the page exists by then).
    """
    return {
        "Title": _title(f"{row['date']} review"),
        "Date": _date_prop(row.get("date")),
        "Status": _select(row.get("status")),
        "Session": _relation([session_page_id] if session_page_id else None),
        schema.SOURCE_KEY: _rich(source_key),
    }


def _session_title(row: dict, data: dict) -> str:
    """Title for a Sessions Notion page.

    Two regimes by status:
      - completed / off-plan with actual miles → ``"{miles} mi ({type})"``
        (e.g. ``"8.1 mi (easy)"``). Synthesized from the structured actuals
        so the title reflects what actually happened, not what was planned.
      - everything else → the ``prescribed_workout`` text verbatim (e.g.
        ``"Easy 8mi"``), falling back to ``type`` then ``"session"``.

    Multi-session days prefix the title with a slot label (``[AM]``, ``[PM]``,
    or ``[k/N]`` for 3+ sessions) so the user can tell same-day sessions apart
    in list/board views where Date alone is ambiguous. The total-slots count
    is stamped onto the row as ``total_slots_on_date`` by the mirror's caller
    (state_manager._notify_mirror) — missing means single-session, no prefix.

    The date deliberately isn't in the title: the Date property carries it
    structurally and the Calendar view shows it on the cell. Matches the
    Google Calendar event-summary convention so the same session reads
    consistently across surfaces (issue #45).
    """
    status = row.get("status")
    miles = data.get("miles")
    sess_type = row.get("type")
    if status in ("completed", "off-plan") and isinstance(miles, (int, float)) and miles > 0:
        miles_str = f"{round(miles, 1):g}"
        base = f"{miles_str} mi ({sess_type})" if sess_type else f"{miles_str} mi"
    else:
        base = row.get("prescribed_workout") or sess_type or "session"
    label = _slot_label_from_row(row)
    return f"[{label}] {base}" if label else base


def _slot_label_from_row(row: dict) -> str:
    """Format the slot label for the row's title prefix.

    Reads ``slot`` and ``total_slots_on_date`` from the row dict (the latter
    is annotated by state_manager._notify_mirror before mirror_sessions is
    called). Falls back to no label when total_slots is missing or 1.
    """
    slot = row.get("slot")
    if not slot:
        return ""
    total = row.get("total_slots_on_date") or 0
    try:
        total = int(total)
    except (TypeError, ValueError):
        return ""
    if total <= 1:
        # Slot is set but we don't know the total — be conservative and skip
        # the label rather than guessing AM/PM.
        return ""
    # Import lazily; mirror.py imports nothing from state_manager directly.
    from state_manager import slot_display_label

    return slot_display_label(slot, total)


def _session_properties(row: dict, source_key: str) -> dict:
    """Map a SQLite sessions row to PRE Sessions Notion properties."""
    data = _session_data(row)
    details = data.get("details") or {}
    sid = details.get("strava_id")
    props = {
        "Title": _title(_session_title(row, data)),
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


def _upsert_journal_entry(entry: dict, client: NotionClient) -> None:
    """Insert or update the PRE Journal page for one journal entry.
    Serialized via ``_upsert_lock`` to keep concurrent mirrors race-free."""
    data_source_id = os.environ["NOTION_JOURNAL_DS_ID"]
    source_key = journal_source_key(entry)
    props = _journal_properties(entry, source_key)
    body = (entry.get("body") or "").strip()
    with _upsert_lock:
        page_id = _query_page_id(client, data_source_id, source_key)
        if page_id:
            client.update_page(page_id, properties=props)
            client.replace_page_markdown(page_id, body)
        else:
            client.create_page(data_source_id, props, markdown=body or None)


def _upsert_plan_change(entry: dict, client: NotionClient) -> None:
    """Insert or update the PRE Plan Changes page for one changelog entry.

    Writes the before/after fenced markdown into the page body when the entry
    carries one (live writers do; the seed parses the changelog blob and has
    no body data, so historical pages stay bodyless).
    """
    data_source_id = os.environ["NOTION_PLAN_CHANGES_DS_ID"]
    source_key = plan_change_source_key(entry)
    props = _plan_change_properties(entry, source_key)
    body = (entry.get("body") or "").strip()
    with _upsert_lock:
        page_id = _query_page_id(client, data_source_id, source_key)
        if page_id:
            client.update_page(page_id, properties=props)
            # Only patch the body when we have one — don't blow away a body
            # written by a live writer just because a later re-seed has none.
            if body:
                client.replace_page_markdown(page_id, body)
        else:
            client.create_page(data_source_id, props, markdown=body or None)


def _upsert_review(row: dict, client: NotionClient) -> None:
    """Insert or update the PRE Reviews page for one SQLite review row.

    Looks up the related Sessions page via ``sid:<session_id>`` to set the
    Session relation. If the session hasn't been mirrored yet (race against
    the post-activity fire-and-forget chain), the relation is left empty —
    the next review upsert (e.g. on resolution) will fill it in.
    """
    from .markdown import render_review_body

    data_source_id = os.environ["NOTION_REVIEWS_DS_ID"]
    source_key = review_source_key(row)
    sessions_ds = os.getenv("NOTION_SESSIONS_DS_ID")
    session_page_id = (
        _query_page_id(client, sessions_ds, f"sid:{row['session_id']}")
        if sessions_ds and row.get("session_id") is not None
        else None
    )
    props = _review_properties(row, source_key, session_page_id=session_page_id)
    body = render_review_body(row.get("critique"), row.get("proposed_change"))
    with _upsert_lock:
        page_id = _query_page_id(client, data_source_id, source_key)
        if page_id:
            client.update_page(page_id, properties=props)
            if body:
                client.replace_page_markdown(page_id, body)
        else:
            client.create_page(data_source_id, props, markdown=body or None)


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
    if row and enabled():
        _spawn(_mirror_batch, [row])


def mirror_sessions(rows: list[dict]) -> None:
    """Mirror several session rows to Notion in one daemon thread."""
    rows = [r for r in (rows or []) if r]
    if rows and enabled():
        _spawn(_mirror_batch, rows)


def mirror_journal_entry(entry: dict) -> None:
    """Mirror one journal entry to Notion in a daemon thread (best-effort)."""
    if entry and journal_enabled():
        _spawn(_mirror_journal_batch, [entry])


def mirror_journal_entries(entries: list[dict]) -> None:
    entries = [e for e in (entries or []) if e]
    if entries and journal_enabled():
        _spawn(_mirror_journal_batch, entries)


def mirror_plan_change(entry: dict) -> None:
    """Mirror one changelog entry to Notion in a daemon thread (best-effort)."""
    if entry and plan_changes_enabled():
        _spawn(_mirror_plan_change_batch, [entry])


def mirror_plan_changes(entries: list[dict]) -> None:
    entries = [e for e in (entries or []) if e]
    if entries and plan_changes_enabled():
        _spawn(_mirror_plan_change_batch, entries)


def mirror_review(row: dict) -> None:
    """Mirror one review row to Notion in a daemon thread (best-effort)."""
    if row and reviews_enabled():
        _spawn(_mirror_review_batch, [row])


def mirror_reviews(rows: list[dict]) -> None:
    rows = [r for r in (rows or []) if r]
    if rows and reviews_enabled():
        _spawn(_mirror_review_batch, rows)


def _mirror_batch(rows: list[dict]) -> None:
    client = NotionClient()
    for row in rows:
        try:
            _upsert_session(row, client)
        except Exception as e:  # noqa: BLE001 — one bad row must not drop the rest
            logger.warning("Notion mirror failed for session id=%s: %s", row.get("id"), e)


def _mirror_journal_batch(entries: list[dict]) -> None:
    client = NotionClient()
    for e in entries:
        try:
            _upsert_journal_entry(e, client)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Notion mirror failed for journal entry %r: %s", e.get("title"), ex)


def _mirror_plan_change_batch(entries: list[dict]) -> None:
    client = NotionClient()
    for e in entries:
        try:
            _upsert_plan_change(e, client)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Notion mirror failed for plan change %r: %s", e.get("timestamp"), ex)


def _mirror_review_batch(rows: list[dict]) -> None:
    client = NotionClient()
    for r in rows:
        try:
            _upsert_review(r, client)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Notion mirror failed for review id=%s: %s", r.get("id"), ex)


def _spawn(fn: Any, *args: Any) -> None:
    threading.Thread(target=_guard, args=(fn, *args), daemon=True).start()


def _guard(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001 — the mirror must never crash a caller
        logger.warning("Notion mirror thread failed: %s", e)
