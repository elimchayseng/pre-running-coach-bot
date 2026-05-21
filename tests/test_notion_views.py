"""Tests for the custom Notion view specs and the create_views bootstrap.

No live API — the Notion client is stubbed. The interesting properties:

  - every spec is well-formed (has a name, a view_type, and a payload
    shape that ``create_view`` can splat),
  - the registry covers all four mirror DBs declared in ``notion.schema``,
  - the bootstrap is idempotent (skips views whose name already exists),
  - one spec raising ``NotionError`` doesn't stop the rest (the per-view
    try/except is load-bearing because some payloads are speculative).
"""

from __future__ import annotations

import pytest

from notion import schema
from notion import views as views_mod
from notion.client import NotionError
from notion.views import (
    JOURNAL_VIEWS,
    PLAN_CHANGES_VIEWS,
    REGISTRY,
    REVIEWS_VIEWS,
    SESSIONS_VIEWS,
    ViewSpec,
)
from scripts.notion_create_views import _create_for_db, create_views

# ---------------- spec data ----------------


_VALID_VIEW_TYPES = {"table", "board", "calendar", "list", "gallery", "timeline"}


class TestViewSpec:
    def test_to_create_kwargs_only_includes_set_fields(self):
        bare = ViewSpec(name="N", view_type="table")
        assert bare.to_create_kwargs() == {"name": "N", "view_type": "table"}

    def test_to_create_kwargs_includes_filter_sorts_extra(self):
        spec = ViewSpec(
            name="X",
            view_type="board",
            filter_={"property": "Status", "select": {"equals": "completed"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            extra={"board": {"group_by": "Status"}},
        )
        out = spec.to_create_kwargs()
        assert out["filter_"] == {"property": "Status", "select": {"equals": "completed"}}
        assert out["sorts"][0]["direction"] == "descending"
        assert out["extra"]["board"]["group_by"] == "Status"


class TestSpecsWellFormed:
    @pytest.mark.parametrize(
        "specs",
        [SESSIONS_VIEWS, JOURNAL_VIEWS, PLAN_CHANGES_VIEWS, REVIEWS_VIEWS],
        ids=["sessions", "journal", "plan_changes", "reviews"],
    )
    def test_each_spec_has_name_and_known_view_type(self, specs):
        assert specs, "each DB must declare at least one custom view"
        for spec in specs:
            assert isinstance(spec.name, str) and spec.name.strip()
            assert spec.view_type in _VALID_VIEW_TYPES, f"{spec.name}: unknown view_type {spec.view_type}"

    @pytest.mark.parametrize(
        "specs",
        [SESSIONS_VIEWS, JOURNAL_VIEWS, PLAN_CHANGES_VIEWS, REVIEWS_VIEWS],
        ids=["sessions", "journal", "plan_changes", "reviews"],
    )
    def test_names_are_unique_per_db(self, specs):
        names = [s.name for s in specs]
        assert len(names) == len(set(names)), f"duplicate view names: {names}"

    @pytest.mark.parametrize(
        "specs",
        [SESSIONS_VIEWS, JOURNAL_VIEWS, PLAN_CHANGES_VIEWS, REVIEWS_VIEWS],
        ids=["sessions", "journal", "plan_changes", "reviews"],
    )
    def test_board_views_declare_group_by(self, specs):
        for spec in specs:
            if spec.view_type == "board":
                assert spec.extra and "board" in spec.extra, f"{spec.name}: board view must declare extra.board"
                assert "group_by" in spec.extra["board"], f"{spec.name}: board view must declare group_by"

    def test_calendar_views_declare_date_property(self):
        for spec in SESSIONS_VIEWS:
            if spec.view_type == "calendar":
                assert spec.extra and "calendar" in spec.extra
                assert "date_property_id" in spec.extra["calendar"]


class TestSpecsMatchIssue:
    """The names below come straight from the issue body. A typo here would
    create a phantom view in Notion that doesn't match the spec the user
    has been promised — keep the names checked verbatim."""

    def test_sessions_view_names(self):
        names = [s.name for s in SESSIONS_VIEWS]
        assert names == ["Calendar", "This week", "By status", "Recent completed"]

    def test_journal_view_names(self):
        names = [s.name for s in JOURNAL_VIEWS]
        assert names == ["Today", "Recent", "By tag", "Sleep < 6h"]

    def test_plan_changes_view_names(self):
        names = [s.name for s in PLAN_CHANGES_VIEWS]
        assert names == ["All", "This week", "By action"]

    def test_reviews_view_names(self):
        names = [s.name for s in REVIEWS_VIEWS]
        assert names == ["All", "Pending", "This week", "By status"]

    def test_reviews_pending_is_status_empty(self):
        pending = next(s for s in REVIEWS_VIEWS if s.name == "Pending")
        assert pending.filter_ == {"property": "Status", "select": {"is_empty": True}}

    def test_sleep_under_6h_is_number_filter(self):
        sleep = next(s for s in JOURNAL_VIEWS if s.name == "Sleep < 6h")
        assert sleep.filter_ == {"property": "Sleep hours", "number": {"less_than": 6}}


class TestRegistry:
    def test_registry_covers_all_four_dbs(self):
        titles = {entry.db_title for entry in REGISTRY}
        assert titles == {
            schema.DB_SESSIONS,
            schema.DB_JOURNAL,
            schema.DB_PLAN_CHANGES,
            schema.DB_REVIEWS,
        }

    def test_registry_env_keys_match_bootstrap(self):
        """The env var names must line up with what scripts/notion_bootstrap.py
        prints into .env — a mismatch would silently skip every DB at runtime."""
        from scripts.notion_bootstrap import _ENV_KEYS

        for entry in REGISTRY:
            expected = _ENV_KEYS[entry.db_title]
            assert (entry.db_id_env, entry.ds_id_env) == expected


# ---------------- bootstrap script ----------------


class _FakeClient:
    """Records create_view calls. Optionally raises on a specific spec name."""

    def __init__(self, existing: list[str] | None = None, fail_on: set[str] | None = None):
        self._existing = existing or []
        self._fail_on = fail_on or set()
        self.created: list[dict] = []
        self.list_views_calls: list[str] = []

    def list_views(self, database_id: str) -> dict:
        self.list_views_calls.append(database_id)
        return {"results": [{"name": n} for n in self._existing]}

    def create_view(self, *, database_id, data_source_id, name, view_type, **kwargs):
        if name in self._fail_on:
            raise NotionError(f"simulated failure on {name}", status=400)
        self.created.append(
            {
                "database_id": database_id,
                "data_source_id": data_source_id,
                "name": name,
                "view_type": view_type,
                **kwargs,
            }
        )
        return {"id": f"view-{name}"}


@pytest.fixture
def env(monkeypatch):
    """Set every DB/DS env var the registry uses so _create_for_db proceeds."""
    monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
    for entry in REGISTRY:
        monkeypatch.setenv(entry.db_id_env, f"db-{entry.db_title}")
        monkeypatch.setenv(entry.ds_id_env, f"ds-{entry.db_title}")
    return monkeypatch


class TestCreateForDb:
    def test_creates_all_specs_when_none_exist(self, env):
        client = _FakeClient(existing=[])
        sessions_entry = next(e for e in REGISTRY if e.db_title == schema.DB_SESSIONS)
        tally = _create_for_db(client, sessions_entry)
        assert tally == {"created": len(SESSIONS_VIEWS), "skipped": 0, "errored": 0}
        assert [c["name"] for c in client.created] == [s.name for s in SESSIONS_VIEWS]

    def test_skips_views_with_matching_name(self, env):
        client = _FakeClient(existing=["Calendar", "This week"])
        sessions_entry = next(e for e in REGISTRY if e.db_title == schema.DB_SESSIONS)
        tally = _create_for_db(client, sessions_entry)
        assert tally["skipped"] == 2
        assert tally["created"] == len(SESSIONS_VIEWS) - 2
        # The two skipped names should not have been re-created.
        created_names = {c["name"] for c in client.created}
        assert "Calendar" not in created_names
        assert "This week" not in created_names

    def test_one_bad_spec_does_not_stop_the_rest(self, env):
        """The per-view try/except is the contract — losing one speculative
        payload must not prevent the well-known specs from landing."""
        client = _FakeClient(existing=[], fail_on={"By status"})
        sessions_entry = next(e for e in REGISTRY if e.db_title == schema.DB_SESSIONS)
        tally = _create_for_db(client, sessions_entry)
        assert tally["errored"] == 1
        assert tally["created"] == len(SESSIONS_VIEWS) - 1
        # Subsequent spec ("Recent completed") still ran.
        assert any(c["name"] == "Recent completed" for c in client.created)

    def test_skips_db_with_missing_env(self, env, monkeypatch):
        monkeypatch.delenv("NOTION_SESSIONS_DB_ID", raising=False)
        client = _FakeClient(existing=[])
        sessions_entry = next(e for e in REGISTRY if e.db_title == schema.DB_SESSIONS)
        tally = _create_for_db(client, sessions_entry)
        # All specs counted as skipped, none created.
        assert tally["created"] == 0
        assert tally["errored"] == 0
        assert tally["skipped"] == len(SESSIONS_VIEWS)
        assert client.created == []

    def test_list_views_failure_falls_back_to_create_all(self, env):
        """If list_views errors, we proceed without dedupe rather than
        bailing — Notion is the final arbiter via 4xx on duplicate names if
        that's even an error there."""

        class _RaisingClient(_FakeClient):
            def list_views(self, database_id):
                raise NotionError("boom", status=500)

        client = _RaisingClient(existing=["Calendar"])
        sessions_entry = next(e for e in REGISTRY if e.db_title == schema.DB_SESSIONS)
        tally = _create_for_db(client, sessions_entry)
        assert tally["created"] == len(SESSIONS_VIEWS)


class TestCreateViewsAggregate:
    def test_iterates_every_db_in_registry(self, env):
        client = _FakeClient(existing=[])
        totals = create_views(client=client)
        expected_created = sum(len(e.specs) for e in REGISTRY)
        assert totals["created"] == expected_created
        # list_views called once per DB.
        assert len(client.list_views_calls) == len(REGISTRY)


# ---------------- client.list_views ----------------


class TestListViews:
    """Lightweight check that the client's list_views uses GET against the
    database views endpoint. Full HTTP-layer coverage lives in test_notion.py."""

    def test_list_views_hits_get_databases_id_views(self, monkeypatch):
        from notion.client import NotionClient

        monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
        monkeypatch.setattr("notion.client.time.sleep", lambda *_: None)

        calls: list[tuple] = []

        class _Resp:
            status_code = 200
            content = b"{}"
            headers: dict = {}

            def json(self):
                return {"results": [{"name": "Calendar"}], "has_more": False}

        class _Session:
            def request(self, method, url, json=None, headers=None, timeout=None):
                calls.append((method, url))
                return _Resp()

        c = NotionClient()
        c._session = _Session()
        out = c.list_views("db-abc")
        assert out["results"][0]["name"] == "Calendar"
        assert calls[0][0] == "GET"
        assert calls[0][1].endswith("/databases/db-abc/views")


# Sanity: the module imports cleanly and exposes the expected public API.
def test_module_exports():
    assert hasattr(views_mod, "REGISTRY")
    assert hasattr(views_mod, "ViewSpec")
    assert hasattr(views_mod, "DBViews")
