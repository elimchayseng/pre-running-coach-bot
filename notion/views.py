"""Custom view specs for the four Notion mirror databases (Phase 1B.5).

Phase 1B.1 created PRE Sessions / PRE Journal / PRE Plan Changes / PRE Reviews
with only Notion's default table view. This module declares the custom views
the original plan called for — calendar, board, smart-filtered "this week",
etc. — as plain data so the bootstrap can iterate them through
``NotionClient.create_view``.

The script that consumes these (`scripts/notion_create_views.py`) wraps each
``create_view`` call in try/except so one bad payload can't stop the rest.
That matters here because the ``/v1/views`` API on 2026-03-11 has a few
payload shapes (``board.group_by``, ``calendar.date_property_id``, and
smart-filter relative-date operators) whose exact wire format is not fully
documented and may need iteration against live Notion.

The data is per-DB so the bootstrap can correlate names to env-var pairs.
Each ``ViewSpec`` is dataclass-style for readability; ``to_create_kwargs``
returns the kwargs ``NotionClient.create_view`` expects. Keeping the mapping
1:1 with the client signature means a future schema change is one edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import schema

# ---------------- spec shape ----------------


@dataclass(frozen=True)
class ViewSpec:
    """One custom view to create on a mirror database.

    Fields mirror ``NotionClient.create_view`` so the bootstrap can splat
    them directly:

      - ``name`` — matched against existing views for idempotency.
      - ``view_type`` — Notion view type (``table``, ``board``,
        ``calendar``, etc.).
      - ``filter_`` — optional filter payload (same shape as
        ``query_data_source``'s filter).
      - ``sorts`` — optional list of sort directives.
      - ``extra`` — type-specific top-level fields (e.g. ``group_by`` for
        board, ``date_property_id`` for calendar). These shapes are the
        speculative bits; see module docstring.
    """

    name: str
    view_type: str
    filter_: Optional[dict] = None
    sorts: Optional[list] = None
    extra: Optional[dict] = None
    # Free-form note flagging specs whose payload is a best-guess and may
    # need adjustment against live Notion. Surfaced in the bootstrap log.
    speculative: bool = False
    notes: str = ""

    def to_create_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"name": self.name, "view_type": self.view_type}
        if self.filter_ is not None:
            kwargs["filter_"] = self.filter_
        if self.sorts is not None:
            kwargs["sorts"] = self.sorts
        if self.extra is not None:
            kwargs["extra"] = self.extra
        return kwargs


# ---------------- filter / sort helpers ----------------
#
# The /v1/views filter shape mirrors the /v1/data_sources/:id/query filter
# shape on 2026-03-11 — same property/operator structure. The relative-date
# operators below ("this_week", "past_week") are documented for query
# filters; whether the views endpoint accepts the same keys verbatim is one
# of the things we'll verify against live Notion.


def _sort(prop: str, direction: str = "ascending") -> dict:
    return {"property": prop, "direction": direction}


def _date_this_week(prop: str) -> dict:
    """Smart filter: rows whose date falls in the current week."""
    return {"property": prop, "date": {"this_week": {}}}


def _date_past_week(prop: str) -> dict:
    """Smart filter: rows whose date falls in the past week. Used as the
    "Today"/"Recent" approximation for Journal — see PRE Journal spec."""
    return {"property": prop, "date": {"past_week": {}}}


def _select_equals(prop: str, value: str) -> dict:
    return {"property": prop, "select": {"equals": value}}


def _select_is_empty(prop: str) -> dict:
    return {"property": prop, "select": {"is_empty": True}}


def _number_less_than(prop: str, value: float) -> dict:
    return {"property": prop, "number": {"less_than": value}}


# ---------------- view specs per database ----------------
#
# Names below come straight from the issue body. The order is the order
# they'll appear in Notion (Notion sorts views by creation time).


SESSIONS_VIEWS: list[ViewSpec] = [
    ViewSpec(
        name="Calendar",
        view_type="calendar",
        extra={"calendar": {"date_property_id": "Date"}},
        speculative=True,
        notes="calendar.date_property_id payload shape not fully documented for /v1/views.",
    ),
    ViewSpec(
        name="This week",
        view_type="table",
        filter_=_date_this_week("Date"),
        sorts=[_sort("Date", "ascending")],
        speculative=True,
        notes="Relative-date smart-filter operator 'this_week' assumed identical to /v1/data_sources query filters.",
    ),
    ViewSpec(
        name="By status",
        view_type="board",
        extra={"board": {"group_by": "Status"}},
        speculative=True,
        notes="board.group_by exact payload shape (string vs object) not fully documented for /v1/views.",
    ),
    ViewSpec(
        name="Recent completed",
        view_type="table",
        filter_=_select_equals("Status", "completed"),
        sorts=[_sort("Date", "descending")],
    ),
]


# NOTE on dropped specs (post-1B.5 live verification):
# Three board specs from the original 15-view set were dropped after the live
# /v1/views POSTs revealed they couldn't be made to render correctly:
#   - JOURNAL "By tag" — Notion silently swapped the multi_select Tags group_by
#     to the first single_select it found (Stress). Multi-select board grouping
#     is not supported by /v1/views on 2026-03-11.
#   - PLAN_CHANGES "By action" — board created with configuration:null. Tried 4
#     payload shapes (string, object, typed-object, explicit property_id);
#     all produced null config. Strongly correlates with the DB having a
#     relation property, but root cause unconfirmed.
#   - REVIEWS "By status" — same failure mode as PLAN_CHANGES "By action".
# If Notion fixes /v1/views to honor these payloads, re-add the specs.


JOURNAL_VIEWS: list[ViewSpec] = [
    # The issue notes "today" may not be a supported smart-filter; we ship
    # past_week as the documented closest fit. If Notion later exposes a
    # `today` operator, swap _date_past_week for {"date": {"today": {}}}.
    ViewSpec(
        name="Today",
        view_type="table",
        filter_=_date_past_week("Date"),
        sorts=[_sort("Date", "descending")],
        speculative=True,
        notes="No documented 'today' smart-filter; using past_week as the closest supported operator.",
    ),
    ViewSpec(
        name="Recent",
        view_type="table",
        sorts=[_sort("Date", "descending")],
    ),
    ViewSpec(
        name="Sleep < 6h",
        view_type="table",
        filter_=_number_less_than("Sleep hours", 6),
        sorts=[_sort("Date", "descending")],
    ),
]


PLAN_CHANGES_VIEWS: list[ViewSpec] = [
    ViewSpec(
        name="All",
        view_type="table",
        sorts=[_sort("Date", "descending")],
    ),
    ViewSpec(
        name="This week",
        view_type="table",
        filter_=_date_this_week("Date"),
        sorts=[_sort("Date", "ascending")],
        speculative=True,
        notes="Relative-date smart-filter operator 'this_week' assumed identical to /v1/data_sources query filters.",
    ),
]


REVIEWS_VIEWS: list[ViewSpec] = [
    ViewSpec(
        name="All",
        view_type="table",
        sorts=[_sort("Date", "descending")],
    ),
    ViewSpec(
        name="Pending",
        view_type="table",
        filter_=_select_is_empty("Status"),
        sorts=[_sort("Date", "descending")],
    ),
    ViewSpec(
        name="This week",
        view_type="table",
        filter_=_date_this_week("Date"),
        sorts=[_sort("Date", "ascending")],
        speculative=True,
        notes="Relative-date smart-filter operator 'this_week' assumed identical to /v1/data_sources query filters.",
    ),
]


# ---------------- registry ----------------


@dataclass(frozen=True)
class DBViews:
    """Pairs a mirror DB's title with its view specs and env-var keys for
    db_id / data_source_id lookup."""

    db_title: str
    db_id_env: str
    ds_id_env: str
    specs: list[ViewSpec] = field(default_factory=list)


REGISTRY: list[DBViews] = [
    DBViews(
        db_title=schema.DB_SESSIONS,
        db_id_env="NOTION_SESSIONS_DB_ID",
        ds_id_env="NOTION_SESSIONS_DS_ID",
        specs=SESSIONS_VIEWS,
    ),
    DBViews(
        db_title=schema.DB_JOURNAL,
        db_id_env="NOTION_JOURNAL_DB_ID",
        ds_id_env="NOTION_JOURNAL_DS_ID",
        specs=JOURNAL_VIEWS,
    ),
    DBViews(
        db_title=schema.DB_PLAN_CHANGES,
        db_id_env="NOTION_PLAN_CHANGES_DB_ID",
        ds_id_env="NOTION_PLAN_CHANGES_DS_ID",
        specs=PLAN_CHANGES_VIEWS,
    ),
    DBViews(
        db_title=schema.DB_REVIEWS,
        db_id_env="NOTION_REVIEWS_DB_ID",
        ds_id_env="NOTION_REVIEWS_DS_ID",
        specs=REVIEWS_VIEWS,
    ),
]
