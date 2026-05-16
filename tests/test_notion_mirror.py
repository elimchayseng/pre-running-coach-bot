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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
