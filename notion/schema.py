"""The four Notion mirror databases (2026-03-11 data-sources model).

This module is the single source of truth for the mirror's Notion schema:

  - the database titles (used by the idempotent bootstrap to find-or-create),
  - the property definitions passed to ``POST /v1/databases``,
  - the property-name constants and ``source_key`` helpers the mirror uses
    to build upsert payloads (Phase 1B.2+).

SQLite stays authoritative; Notion is a one-way reflection.
"""

from __future__ import annotations

# Database titles. The four live under the PRE Training parent page; the
# bootstrap matches on these exact titles for idempotency.
DB_SESSIONS = "PRE Sessions"
DB_JOURNAL = "PRE Journal"
DB_PLAN_CHANGES = "PRE Plan Changes"
DB_REVIEWS = "PRE Reviews"

# Hidden idempotency key carried on every mirrored row. The mirror queries
# on it to decide insert-vs-update. (Notion has no per-property "hidden"
# flag at the data-source level — it is simply omitted from custom views.)
SOURCE_KEY = "source_key"


# source_key prefixes — one stable key per row, per source SQLite table.
def session_key(sessions_id: int) -> str:
    return f"sid:{sessions_id}"


def journal_key(journal_id: int) -> str:
    return f"jid:{journal_id}"


def plan_change_key(changelog_id: int) -> str:
    return f"cid:{changelog_id}"


def review_key(review_id: int) -> str:
    return f"rid:{review_id}"


# ---------- property-definition helpers ----------


def _select(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


def _multi_select(*names: str) -> dict:
    return {"multi_select": {"options": [{"name": n} for n in names]}}


def _relation(data_source_id: str) -> dict:
    """A one-way relation into another data source (no synced back-relation)."""
    return {"relation": {"data_source_id": data_source_id, "single_property": {}}}


# ---------- database property schemas ----------

# Sessions — one row per workout, mirroring the unified `sessions` table.
SESSIONS_PROPERTIES: dict = {
    "Title": {"title": {}},
    "Date": {"date": {}},
    "Slot": _select("am", "pm"),
    "Status": _select("planned", "completed", "missed", "off-plan"),
    "Type": _select("easy", "workout", "long", "race", "cross", "strength", "rest"),
    "Prescribed": {"rich_text": {}},
    "Pace target": {"rich_text": {}},
    "Plan notes": {"rich_text": {}},
    "Miles": {"number": {"format": "number"}},
    "Avg pace": {"rich_text": {}},
    "Avg HR": {"number": {"format": "number"}},
    "Strava ID": {"number": {"format": "number"}},
    "Strava URL": {"url": {}},
    "Coach notes": {"rich_text": {}},
    SOURCE_KEY: {"rich_text": {}},
}

# Journal — one row per journal entry.
JOURNAL_PROPERTIES: dict = {
    "Title": {"title": {}},
    "Date": {"date": {}},
    "Sleep hours": {"number": {"format": "number"}},
    "Stress": _select("1", "2", "3", "4", "5"),
    "Tags": _multi_select("travel", "illness", "soreness", "life", "decision"),
    SOURCE_KEY: {"rich_text": {}},
    "sqlite_id": {"rich_text": {}},  # placeholder for Phase 2 echo prevention
}


def plan_changes_properties(sessions_data_source_id: str) -> dict:
    """Plan changes — one row per changelog entry. Relates to Sessions, so
    the Sessions data source must already exist when this is created."""
    return {
        "Title": {"title": {}},
        "Date": {"date": {}},
        "Action": _select("planned-edit", "completed", "missed", "meta-edit"),
        "Reason": {"rich_text": {}},
        "Triggered by": _relation(sessions_data_source_id),
        SOURCE_KEY: {"rich_text": {}},
    }


def reviews_properties(sessions_data_source_id: str) -> dict:
    """Reviews — one row per post-activity review. Relates to Sessions."""
    return {
        "Title": {"title": {}},
        "Session": _relation(sessions_data_source_id),
        "Date": {"date": {}},
        "Status": _select("approved", "rejected", "expired", "no-op"),
        SOURCE_KEY: {"rich_text": {}},
    }
