"""Tests for the Notion Sessions mirror (Phase 1B.2): markdown + mirror.

No live API — the mirror is exercised with a fake Notion client.
"""

from __future__ import annotations

import json

import pytest

from notion import mirror
from notion.markdown import render_session_body


def _row(**over) -> dict:
    base = {
        "id": 5,
        "date": "2026-05-16",
        "slot": None,
        "status": "planned",
        "type": "easy",
        "prescribed_workout": "Easy 8mi",
        "prescribed_pace": "8:30-9:00",
        "prescribed_notes": "base",
        "detail_md": None,
        "data": None,
    }
    base.update(over)
    return base


# ---------------- render_session_body ----------------


class TestSessionBody:
    def test_planned_with_detail_returns_detail(self):
        body = render_session_body(_row(detail_md="Sharpening session.\nFeel fresh."))
        assert body == "Sharpening session.\nFeel fresh."

    def test_planned_without_detail_returns_none(self):
        assert render_session_body(_row()) is None

    def test_completed_renders_notes_and_laps(self):
        data = json.dumps(
            {
                "miles": 5.1,
                "notes": "felt smooth",
                "details": {"laps": [{"name": "Rep 1", "distance_mi": 0.62, "pace": "6:01", "hr_avg": 178}]},
            }
        )
        body = render_session_body(_row(status="completed", data=data))
        assert "## Notes" in body and "felt smooth" in body
        assert "## Laps" in body and "Rep 1" in body and "6:01" in body

    def test_completed_no_data_returns_none(self):
        assert render_session_body(_row(status="completed", data=None)) is None

    def test_completed_keeps_detail_and_actuals(self):
        body = render_session_body(_row(status="completed", detail_md="Race plan.", data=json.dumps({"notes": "done"})))
        assert body.index("Race plan.") < body.index("## Notes")


# ---------------- property builders ----------------


class TestPropertyBuilders:
    def test_title_strips_bold_and_caps(self):
        assert mirror._title("**BROOKLYN HALF**") == {"title": [{"text": {"content": "BROOKLYN HALF"}}]}

    def test_rich_empty_clears(self):
        assert mirror._rich(None) == {"rich_text": []}
        assert mirror._rich("  ") == {"rich_text": []}

    def test_rich_caps_at_2000(self):
        out = mirror._rich("x" * 5000)
        assert len(out["rich_text"][0]["text"]["content"]) == 2000

    def test_select_none_clears(self):
        assert mirror._select(None) == {"select": None}
        assert mirror._select("planned") == {"select": {"name": "planned"}}

    def test_number_rejects_non_numeric(self):
        assert mirror._number("8") == {"number": None}
        assert mirror._number(8.1) == {"number": 8.1}


class TestSessionProperties:
    def test_maps_prescription_and_actuals(self):
        data = json.dumps({"miles": 8.1, "pace_avg": "7:05", "hr_avg": 150, "details": {"strava_id": 42}})
        props = mirror._session_properties(_row(status="completed", data=data), "sid:5")
        assert props["Status"] == {"select": {"name": "completed"}}
        assert props["Miles"] == {"number": 8.1}
        assert props["Strava ID"] == {"number": 42}
        assert props["Strava URL"] == {"url": "https://www.strava.com/activities/42"}
        assert props[mirror.schema.SOURCE_KEY]["rich_text"][0]["text"]["content"] == "sid:5"

    def test_no_strava_id_clears_url(self):
        props = mirror._session_properties(_row(), "sid:5")
        assert props["Strava URL"] == {"url": None}
        assert props["Strava ID"] == {"number": None}


# ---------------- upsert ----------------


class _FakeClient:
    def __init__(self, existing_page_id=None):
        self._existing = existing_page_id
        self.created: list = []
        self.updated: list = []
        self.markdown_patched: list = []

    def query_data_source(self, ds, filter_=None):
        return {"results": [{"id": self._existing}] if self._existing else []}

    def create_page(self, ds, properties, markdown=None):
        self.created.append({"ds": ds, "properties": properties, "markdown": markdown})
        return {"id": "new-page"}

    def update_page(self, page_id, properties=None):
        self.updated.append({"page_id": page_id, "properties": properties})
        return {"id": page_id}

    def replace_page_markdown(self, page_id, markdown):
        self.markdown_patched.append({"page_id": page_id, "markdown": markdown})
        return {"id": page_id}


class TestUpsert:
    def test_insert_when_no_existing_page(self, monkeypatch):
        monkeypatch.setenv("NOTION_SESSIONS_DS_ID", "ds-1")
        client = _FakeClient(existing_page_id=None)
        mirror._upsert_session(_row(detail_md="body text"), client)
        assert len(client.created) == 1
        assert client.created[0]["markdown"] == "body text"
        assert client.updated == []

    def test_update_when_page_exists(self, monkeypatch):
        monkeypatch.setenv("NOTION_SESSIONS_DS_ID", "ds-1")
        client = _FakeClient(existing_page_id="page-7")
        mirror._upsert_session(_row(), client)
        assert client.created == []
        assert client.updated[0]["page_id"] == "page-7"
        # Body always synced (to empty here) so a removed detail is cleared.
        assert client.markdown_patched[0] == {"page_id": "page-7", "markdown": ""}

    def test_query_and_write_run_under_the_lock(self, monkeypatch):
        """The query+write is serialized so two threads mirroring the same
        source_key can't both miss the query and double-insert."""
        monkeypatch.setenv("NOTION_SESSIONS_DS_ID", "ds-1")
        lock_held: list = []

        class _LockProbeClient(_FakeClient):
            def query_data_source(self, ds, filter_=None):
                lock_held.append(mirror._upsert_lock.locked())
                return super().query_data_source(ds, filter_)

        mirror._upsert_session(_row(), _LockProbeClient())
        assert lock_held == [True]


# ---------------- enable gate ----------------


class TestEnabled:
    def test_disabled_without_config(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.delenv("NOTION_SESSIONS_DS_ID", raising=False)
        assert mirror.enabled() is False

    def test_enabled_with_full_config(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
        monkeypatch.setenv("NOTION_SESSIONS_DS_ID", "ds-1")
        assert mirror.enabled() is True

    def test_mirror_session_noops_when_disabled(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        called = []
        monkeypatch.setattr(mirror, "_mirror_batch", lambda rows: called.append(rows))
        mirror.mirror_session(_row())
        assert called == []  # disabled → no thread spawned

    def test_per_db_gates_independent(self, monkeypatch):
        """Journal and Plan Changes each gate on their own DS id so a
        partially-configured workspace mirrors just what's wired."""
        monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
        monkeypatch.delenv("NOTION_SESSIONS_DS_ID", raising=False)
        monkeypatch.setenv("NOTION_JOURNAL_DS_ID", "ds-j")
        monkeypatch.delenv("NOTION_PLAN_CHANGES_DS_ID", raising=False)
        assert mirror.enabled() is False
        assert mirror.journal_enabled() is True
        assert mirror.plan_changes_enabled() is False


# ---------------- journal mirror ----------------


def _j(**over) -> dict:
    base = {"title": "2026-05-19 12:00:00", "date": "2026-05-19", "body": "felt great"}
    base.update(over)
    return base


class TestJournalMirror:
    def test_source_key_uses_title(self):
        assert mirror.journal_source_key(_j(title="A header")) == "jid:A header"

    def test_properties_shape(self):
        props = mirror._journal_properties(_j(), "jid:k")
        assert props["Title"]["title"][0]["text"]["content"] == "2026-05-19 12:00:00"
        assert props["Date"] == {"date": {"start": "2026-05-19"}}
        assert props["Tags"] == {"multi_select": []}
        assert props[mirror.schema.SOURCE_KEY]["rich_text"][0]["text"]["content"] == "jid:k"

    def test_date_none_clears(self):
        assert mirror._journal_properties(_j(date=None), "jid:k")["Date"] == {"date": None}

    def test_insert_when_no_existing(self, monkeypatch):
        monkeypatch.setenv("NOTION_JOURNAL_DS_ID", "ds-j")
        client = _FakeClient(existing_page_id=None)
        mirror._upsert_journal_entry(_j(body="hello"), client)
        assert client.created[0]["markdown"] == "hello"
        assert client.updated == []

    def test_update_when_page_exists(self, monkeypatch):
        monkeypatch.setenv("NOTION_JOURNAL_DS_ID", "ds-j")
        client = _FakeClient(existing_page_id="page-j")
        mirror._upsert_journal_entry(_j(body="updated body"), client)
        assert client.updated[0]["page_id"] == "page-j"
        assert client.markdown_patched[0] == {"page_id": "page-j", "markdown": "updated body"}


# ---------------- plan-change mirror ----------------


def _c(**over) -> dict:
    base = {"timestamp": "2026-05-19T12:00:00", "note": "Added yoga", "action": "planned-edit"}
    base.update(over)
    return base


class TestPlanChangeMirror:
    def test_source_key_uses_timestamp(self):
        assert mirror.plan_change_source_key(_c()) == "cid:2026-05-19T12:00:00"

    def test_properties_shape(self):
        props = mirror._plan_change_properties(_c(), "cid:k")
        assert props["Date"] == {"date": {"start": "2026-05-19"}}
        assert props["Action"] == {"select": {"name": "planned-edit"}}
        assert props["Reason"]["rich_text"][0]["text"]["content"] == "Added yoga"
        assert props["Triggered by"] == {"relation": []}

    def test_insert_when_no_existing(self, monkeypatch):
        monkeypatch.setenv("NOTION_PLAN_CHANGES_DS_ID", "ds-c")
        client = _FakeClient(existing_page_id=None)
        mirror._upsert_plan_change(_c(), client)
        # No markdown body in 1B.3 — before/after diffs need session tracking
        assert client.created[0]["markdown"] is None
        assert client.markdown_patched == []

    def test_update_skips_markdown_patch(self, monkeypatch):
        """Plan Changes pages don't carry a body — update touches properties only."""
        monkeypatch.setenv("NOTION_PLAN_CHANGES_DS_ID", "ds-c")
        client = _FakeClient(existing_page_id="page-c")
        mirror._upsert_plan_change(_c(), client)
        assert client.updated[0]["page_id"] == "page-c"
        assert client.markdown_patched == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
