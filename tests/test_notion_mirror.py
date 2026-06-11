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

    def test_omits_reflection_property(self):
        """Contract: Reflection is athlete-owned and the mirror NEVER patches it.

        Notion PATCH semantics only update properties named in the payload —
        omission preserves whatever the athlete typed in Notion. The Worker
        bridge (`notion_worker/src/worker.ts`) is the single writer for this
        field. Regressing this contract would cause every Strava upload to
        silently overwrite the athlete's notes; this test is the trip-wire.
        """
        data = json.dumps({"miles": 6.0, "notes": "coach-side note from data.notes"})
        row = _row(status="completed", data=data)
        # Even when a hypothetical row carries a stray reflection field, the
        # mirror's property builder still omits it.
        row["reflection"] = "athlete typed this in Notion"
        props = mirror._session_properties(row, "sid:5")
        assert "Reflection" not in props


class TestSessionTitle:
    """Title regimes by status (issue #45): planned uses the prescribed text
    verbatim; completed/off-plan synthesize from actual miles + type so the
    title reflects what happened, not what was planned. No date prefix in
    either case — that lives on the Date property and on the Calendar cell."""

    def _title_of(self, row):
        props = mirror._session_properties(row, "sid:5")
        return props["Title"]["title"][0]["text"]["content"]

    def test_planned_uses_prescribed_text(self):
        assert self._title_of(_row()) == "Easy 8mi"

    def test_planned_without_prescribed_falls_back_to_type(self):
        assert self._title_of(_row(prescribed_workout=None)) == "easy"

    def test_planned_without_prescribed_or_type_falls_back_to_session(self):
        assert self._title_of(_row(prescribed_workout=None, type=None)) == "session"

    def test_completed_uses_actual_miles_and_type(self):
        data = json.dumps({"miles": 8.1})
        assert self._title_of(_row(status="completed", data=data)) == "8.1 mi (easy)"

    def test_completed_drops_trailing_zero_decimal(self):
        data = json.dumps({"miles": 6.0})
        assert self._title_of(_row(status="completed", data=data)) == "6 mi (easy)"

    def test_off_plan_also_uses_actual_miles(self):
        data = json.dumps({"miles": 3.27})
        assert self._title_of(_row(status="off-plan", type="easy", data=data)) == "3.3 mi (easy)"

    def test_completed_without_miles_falls_back_to_prescribed(self):
        # Strength / cross have no miles; title should still be useful.
        row = _row(status="completed", type="strength", prescribed_workout="30min strength")
        assert self._title_of(row) == "30min strength"

    def test_completed_with_zero_miles_falls_back_to_prescribed(self):
        data = json.dumps({"miles": 0})
        assert self._title_of(_row(status="completed", data=data)) == "Easy 8mi"

    def test_missed_uses_prescribed_text(self):
        # Missed sessions still show what was planned, not a synthesized "0 mi".
        assert self._title_of(_row(status="missed")) == "Easy 8mi"

    def test_no_date_prefix(self):
        # Regression: ensure the 2026-05-16 date string never leaks into the title.
        title = self._title_of(_row())
        assert "2026-" not in title, f"date prefix leaked into title: {title!r}"

    def test_two_a_day_prefixes_am_pm(self):
        am = _row(slot="1", total_slots_on_date=2)
        pm = _row(slot="2", total_slots_on_date=2, prescribed_workout="6x400 @ 5K")
        assert self._title_of(am) == "[AM] Easy 8mi"
        assert self._title_of(pm) == "[PM] 6x400 @ 5K"

    def test_three_a_day_prefixes_k_of_n(self):
        midday = _row(slot="2", total_slots_on_date=3, prescribed_workout="Mobility")
        assert self._title_of(midday) == "[2/3] Mobility"

    def test_slot_without_total_yields_no_prefix(self):
        # Defensive: caller didn't stamp total_slots_on_date → don't guess.
        row = _row(slot="1")
        assert self._title_of(row) == "Easy 8mi"

    def test_single_session_no_prefix(self):
        # total_slots_on_date=1 → label is empty, base title preserved.
        assert self._title_of(_row(total_slots_on_date=1)) == "Easy 8mi"

    def test_completed_two_a_day_keeps_slot_prefix(self):
        data = json.dumps({"miles": 5.0})
        row = _row(status="completed", slot="1", total_slots_on_date=2, data=data)
        assert self._title_of(row) == "[AM] 5 mi (easy)"


class TestSessionPropertiesSlot:
    """Slot select property writes the row's slot ordinal verbatim — Notion
    auto-creates the option when an unseen value appears."""

    def test_slot_select_carries_ordinal(self):
        props = mirror._session_properties(_row(slot="1", total_slots_on_date=2), "sid:5")
        assert props["Slot"] == {"select": {"name": "1"}}

    def test_slot_none_clears_property(self):
        props = mirror._session_properties(_row(slot=None), "sid:5")
        assert props["Slot"] == {"select": None}


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

    def test_insert_without_body(self, monkeypatch):
        monkeypatch.setenv("NOTION_PLAN_CHANGES_DS_ID", "ds-c")
        client = _FakeClient(existing_page_id=None)
        mirror._upsert_plan_change(_c(), client)
        assert client.created[0]["markdown"] is None
        assert client.markdown_patched == []

    def test_insert_with_body(self, monkeypatch):
        monkeypatch.setenv("NOTION_PLAN_CHANGES_DS_ID", "ds-c")
        client = _FakeClient(existing_page_id=None)
        mirror._upsert_plan_change(_c(body="## Before\n\n```\nx\n```"), client)
        assert client.created[0]["markdown"] == "## Before\n\n```\nx\n```"

    def test_update_patches_body_when_present(self, monkeypatch):
        monkeypatch.setenv("NOTION_PLAN_CHANGES_DS_ID", "ds-c")
        client = _FakeClient(existing_page_id="page-c")
        mirror._upsert_plan_change(_c(body="new diff"), client)
        assert client.updated[0]["page_id"] == "page-c"
        assert client.markdown_patched[0] == {"page_id": "page-c", "markdown": "new diff"}

    def test_update_skips_body_patch_when_empty(self, monkeypatch):
        """A re-seed (no body) must not blow away a body a live writer set."""
        monkeypatch.setenv("NOTION_PLAN_CHANGES_DS_ID", "ds-c")
        client = _FakeClient(existing_page_id="page-c")
        mirror._upsert_plan_change(_c(), client)  # no body
        assert client.updated[0]["page_id"] == "page-c"
        assert client.markdown_patched == []


# ---------------- review mirror ----------------


def _r(**over) -> dict:
    base = {
        "id": 7,
        "session_id": 42,
        "strava_id": 99999,
        "date": "2026-05-20",
        "critique": "Held HR cap, paced even.",
        "proposed_change": None,
        "status": None,
    }
    base.update(over)
    return base


class _ReviewFakeClient:
    """Fake client that branches its query response on the data_source_id —
    review upserts query both the Sessions DS (sid lookup) and the Reviews DS."""

    def __init__(self, *, session_page_id=None, review_page_id=None):
        self.session_page_id = session_page_id
        self.review_page_id = review_page_id
        self.created: list = []
        self.updated: list = []
        self.markdown_patched: list = []

    def query_data_source(self, ds, filter_=None):
        if ds == "ds-sessions":
            return {"results": [{"id": self.session_page_id}] if self.session_page_id else []}
        if ds == "ds-reviews":
            return {"results": [{"id": self.review_page_id}] if self.review_page_id else []}
        return {"results": []}

    def create_page(self, ds, properties, markdown=None):
        self.created.append({"ds": ds, "properties": properties, "markdown": markdown})
        return {"id": "new-page"}

    def update_page(self, page_id, properties=None):
        self.updated.append({"page_id": page_id, "properties": properties})
        return {"id": page_id}

    def replace_page_markdown(self, page_id, markdown):
        self.markdown_patched.append({"page_id": page_id, "markdown": markdown})
        return {"id": page_id}


class TestReviewMirror:
    def test_source_key_uses_review_id(self):
        assert mirror.review_source_key(_r()) == "rid:7"

    def test_properties_when_session_page_exists(self):
        props = mirror._review_properties(_r(), "rid:7", session_page_id="page-s")
        assert props["Date"] == {"date": {"start": "2026-05-20"}}
        assert props["Status"] == {"select": None}  # pending
        assert props["Session"] == {"relation": [{"id": "page-s"}]}

    def test_properties_when_session_page_missing(self):
        props = mirror._review_properties(_r(), "rid:7", session_page_id=None)
        assert props["Session"] == {"relation": []}

    def test_insert_links_session_and_writes_body(self, monkeypatch):
        monkeypatch.setenv("NOTION_REVIEWS_DS_ID", "ds-reviews")
        monkeypatch.setenv("NOTION_SESSIONS_DS_ID", "ds-sessions")
        client = _ReviewFakeClient(session_page_id="page-s", review_page_id=None)
        mirror._upsert_review(
            _r(critique="Strong", proposed_change={"summary": "x", "new_plan_md": "# p", "reason": "y"}),
            client,
        )
        [created] = client.created
        assert created["ds"] == "ds-reviews"
        assert created["properties"]["Session"] == {"relation": [{"id": "page-s"}]}
        body = created["markdown"]
        assert "## Critique" in body and "Strong" in body
        assert "## Proposed change" in body and "> x" in body

    def test_update_when_review_page_exists(self, monkeypatch):
        monkeypatch.setenv("NOTION_REVIEWS_DS_ID", "ds-reviews")
        monkeypatch.setenv("NOTION_SESSIONS_DS_ID", "ds-sessions")
        client = _ReviewFakeClient(session_page_id="page-s", review_page_id="page-r")
        mirror._upsert_review(_r(critique="new note"), client)
        assert client.created == []
        assert client.updated[0]["page_id"] == "page-r"
        # critique-only review still produces a body
        assert client.markdown_patched[0]["page_id"] == "page-r"
        assert "## Critique" in client.markdown_patched[0]["markdown"]


# ---------------- render_change_body ----------------


from notion.markdown import render_change_body, render_review_body  # noqa: E402


class TestRenderReviewBody:
    def test_critique_only(self):
        body = render_review_body("Steady run.", None)
        assert body.startswith("## Critique")
        assert "## Proposed change" not in body

    def test_critique_plus_proposal(self):
        body = render_review_body(
            "Strong execution.",
            {"summary": "Bump tempo 5s", "new_plan_md": "# v2", "reason": "fitness up"},
        )
        assert "## Critique" in body and "Strong execution" in body
        assert "## Proposed change" in body
        assert "> Bump tempo 5s" in body
        assert "```markdown\n# v2\n```" in body
        assert "*Reason:* fitness up" in body

    def test_empty_returns_none(self):
        assert render_review_body(None, None) is None
        assert render_review_body("", {}) is None


class TestRenderChangeBody:
    def test_both_sides(self):
        body = render_change_body("old", "new")
        assert "## Before" in body and "old" in body
        assert "## After" in body and "new" in body
        assert "```" in body

    def test_after_only(self):
        body = render_change_body(None, "new")
        assert "## Before" not in body
        assert "## After" in body

    def test_before_only(self):
        body = render_change_body("old", "")
        assert "## Before" in body
        assert "## After" not in body

    def test_both_empty_returns_none(self):
        assert render_change_body("", None) is None
        assert render_change_body(None, "   ") is None

    def test_custom_headings(self):
        body = render_change_body("p", "a", before_heading="Prescribed", after_heading="Actuals")
        assert "## Prescribed" in body and "## Actuals" in body


# ---------------- daily health (PRE Health) ----------------


def _health_row(**overrides) -> dict:
    base = {
        "date": "2026-06-10",
        "sleep_score": 96,
        "sleep_duration_min": 384,
        "sleep_nap_min": 122,
        "hrv_avg": 102,
        "hrv_baseline": 82,
        "hrv_evaluation": "Above normal",
        "resting_hr": 49,
        "stress_avg": 22,
        "steps": 4356,
        "recovery_pct": None,
        "recovery_level": None,
        "load_short_term": 131.0,
        "load_long_term": 104.0,
        "load_ratio": 1.25,
        "load_comment": "Optimized",
    }
    base.update(overrides)
    return base


class TestHealthProperties:
    def test_maps_metrics(self):
        props = mirror._health_properties(_health_row(), "hid:2026-06-10")
        assert props["Title"]["title"][0]["text"]["content"] == "2026-06-10"
        assert props["Date"] == {"date": {"start": "2026-06-10"}}
        assert props["Sleep score"] == {"number": 96}
        assert props["Sleep hours"] == {"number": 6.4}  # 384 min, naps separate
        assert props["Nap min"] == {"number": 122}
        assert props["HRV eval"] == {"select": {"name": "Above normal"}}
        assert props["Load status"] == {"select": {"name": "Optimized"}}
        assert props["source_key"]["rich_text"][0]["text"]["content"] == "hid:2026-06-10"

    def test_missing_fields_clear_properties(self):
        props = mirror._health_properties({"date": "2026-06-11"}, "hid:2026-06-11")
        assert props["Sleep hours"] == {"number": None}
        assert props["HRV eval"] == {"select": None}
        assert props["Recovery %"] == {"number": None}

    def test_health_key(self):
        from notion import schema

        assert schema.health_key("2026-06-11") == "hid:2026-06-11"


class TestHealthUpsert:
    def test_insert_when_no_existing_page(self, monkeypatch):
        monkeypatch.setenv("NOTION_HEALTH_DS_ID", "ds-h")
        client = _FakeClient(existing_page_id=None)
        mirror._upsert_health(_health_row(), client)
        assert len(client.created) == 1
        assert client.created[0]["ds"] == "ds-h"
        assert client.created[0]["markdown"] is None  # property-only pages
        assert client.updated == []

    def test_update_when_page_exists(self, monkeypatch):
        monkeypatch.setenv("NOTION_HEALTH_DS_ID", "ds-h")
        client = _FakeClient(existing_page_id="page-h")
        mirror._upsert_health(_health_row(), client)
        assert client.created == []
        assert client.updated[0]["page_id"] == "page-h"
        assert client.markdown_patched == []  # body never touched


class TestHealthEnabledGate:
    def test_disabled_without_ds_id(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
        monkeypatch.delenv("NOTION_HEALTH_DS_ID", raising=False)
        assert mirror.health_enabled() is False

    def test_enabled_with_full_config(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
        monkeypatch.setenv("NOTION_HEALTH_DS_ID", "ds-h")
        assert mirror.health_enabled() is True

    def test_mirror_health_rows_noops_when_disabled(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        called = []
        monkeypatch.setattr(mirror, "_mirror_health_batch", lambda rows: called.append(rows))
        mirror.mirror_health_rows([_health_row()])
        assert called == []

    def test_rows_without_dates_filtered(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
        monkeypatch.setenv("NOTION_HEALTH_DS_ID", "ds-h")
        spawned = []
        monkeypatch.setattr(mirror, "_spawn", lambda fn, rows: spawned.append(rows))
        mirror.mirror_health_rows([{"no_date": True}, _health_row()])
        assert len(spawned) == 1
        assert len(spawned[0]) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
