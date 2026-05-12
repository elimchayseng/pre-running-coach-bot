"""Tests for google_calendar.sync — row parsing, insert/patch/delete
accounting, dry-run, hash-skip, sync-state roundtrip."""

from __future__ import annotations

import json
from datetime import date

import pytest

from google_calendar import sync

PLAN_FIXTURE = """\
# Training Plan

### This Week (2026-05-08 → 2026-05-16)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Fri | 2026-05-08 | Rest + gentle yoga PM 30-40min | — | Hip/hamstring focus |
| Sat | 2026-05-09 | Easy 8mi STRICT | 8:30-9:00, HR ≤155 | foo |
| Sun | 2026-05-10 | — | — | Skipped — empty workout |
| Mon | 2026-05-11 | Easy 4mi + restorative yoga PM | 8:30-9:00, HR ≤155 | Race week begins |
| Sat | 2026-05-16 | **BROOKLYN HALF** | 1:21:00 (6:10) | 7am ET start |

### Phase 2 — Bridge

Some prose. No table rows.
"""


class _StubState:
    def __init__(self, plan_text: str):
        self._plan = plan_text

    def load_plan(self) -> str:
        return self._plan


@pytest.fixture
def patched_sync_state(monkeypatch, tmp_path):
    """Redirect SYNC_STATE_FILE to a tmp path so tests don't pollute state/."""
    p = tmp_path / ".gcal_sync_state.json"
    monkeypatch.setattr(sync, "SYNC_STATE_FILE", p)
    yield p


@pytest.fixture
def fixed_today(monkeypatch):
    """Pin today_local() to 2026-05-11 so prune windows are deterministic."""
    monkeypatch.setattr(sync, "today_local", lambda: date(2026, 5, 11))
    yield date(2026, 5, 11)


class TestParsePlanRows:
    def test_parses_locked_format_rows_only(self):
        rows = sync._parse_plan_rows(PLAN_FIXTURE)
        # 4 valid rows: Fri/Sat/Mon/Sat — Sun is skipped (empty workout),
        # header + separator + phase-2 prose are not table rows.
        assert len(rows) == 4
        dates = [r["date"] for r in rows]
        assert dates == ["2026-05-08", "2026-05-09", "2026-05-11", "2026-05-16"]

    def test_skips_empty_workout(self):
        text = """\
| Day | Date | Workout | Pace target | Notes |
| Sun | 2026-05-10 | — | — | should skip |
| Mon | 2026-05-11 | Easy 4mi | x | y |
"""
        rows = sync._parse_plan_rows(text)
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-05-11"

    def test_skips_non_iso_date(self):
        text = """\
| Day | Date | Workout | Pace target | Notes |
| Sat | 5/9 | Easy 8mi | x | y |
"""
        rows = sync._parse_plan_rows(text)
        assert rows == []


class TestEventPayload:
    def test_event_id_is_deterministic(self):
        assert sync._event_id("2026-05-11") == "pretrain20260511"

    def test_strips_bold_from_summary(self):
        row = {
            "day_name": "Sat",
            "date": "2026-05-16",
            "workout": "**BROOKLYN HALF**",
            "pace_target": "1:21:00",
            "notes": "7am",
        }
        payload, h = sync._build_event_payload(row, sync._event_id(row["date"]))
        assert payload["summary"] == "BROOKLYN HALF"
        assert payload["start"]["date"] == "2026-05-16"
        assert payload["end"]["date"] == "2026-05-17"
        assert payload["extendedProperties"]["private"]["pre_managed"] == "1"
        assert payload["extendedProperties"]["private"]["pre_plan_hash"] == h

    def test_hash_changes_with_workout(self):
        row1 = {
            "day_name": "Mon",
            "date": "2026-05-11",
            "workout": "Easy 4mi",
            "pace_target": "8:30",
            "notes": "x",
        }
        row2 = {**row1, "workout": "Easy 5mi"}
        _, h1 = sync._build_event_payload(row1, sync._event_id(row1["date"]))
        _, h2 = sync._build_event_payload(row2, sync._event_id(row2["date"]))
        assert h1 != h2


class TestParseWorkoutDetails:
    def test_extracts_block_until_next_anchor(self):
        text = """\
### Workout Notes

#### 2026-05-12
Sharpening, not testing. 4 days out from race.

**Structure:**
- 1.5mi WU easy
- 3x1000m @ 6:00-6:05 pace
- 2:30 jog recovery between reps
- 1mi CD easy

Cue: feel like you could've done one more.

#### 2026-05-16
Brooklyn Half. Mile 1 must be 6:15-6:20.
"""
        details = sync._parse_workout_details(text)
        assert set(details.keys()) == {"2026-05-12", "2026-05-16"}
        assert "Sharpening" in details["2026-05-12"]
        assert "Cue:" in details["2026-05-12"]
        assert "Brooklyn Half" in details["2026-05-16"]
        # Should NOT bleed across anchors.
        assert "Brooklyn" not in details["2026-05-12"]

    def test_body_terminates_at_higher_heading(self):
        text = """\
#### 2026-05-12
This is the detail body.

## Phase 2 Bridge

Phase 2 prose, not part of the detail.
"""
        details = sync._parse_workout_details(text)
        assert details["2026-05-12"] == "This is the detail body."

    def test_empty_or_whitespace_body_omitted(self):
        text = """\
#### 2026-05-12

#### 2026-05-13
Has content.
"""
        details = sync._parse_workout_details(text)
        assert "2026-05-12" not in details
        assert details["2026-05-13"] == "Has content."

    def test_no_anchors_returns_empty(self):
        assert sync._parse_workout_details("# just prose, no detail anchors") == {}

    def test_anchor_must_be_level_4_only(self):
        # H3 with date should not count as an anchor.
        text = """\
### 2026-05-12
Body under H3 — should NOT be parsed as a detail block.
"""
        assert sync._parse_workout_details(text) == {}


class TestRichDescription:
    def test_uses_detail_body_when_present(self):
        row = {
            "day_name": "Tue",
            "date": "2026-05-12",
            "workout": "5mi w/ 3x1000m",
            "pace_target": "6:00-6:05 reps",
            "notes": "Race week",
        }
        body = "Sharpening, not testing.\n\n**Structure:**\n- WU\n- Reps\n- CD"
        payload, _ = sync._build_event_payload(row, sync._event_id(row["date"]), body)
        # Rich body present → table cells NOT included as Pace:/Notes:.
        assert payload["description"].startswith("Sharpening, not testing.")
        assert payload["description"].endswith("(synced by PRE)")
        assert "**Structure:**" in payload["description"]
        assert "Pace: 6:00-6:05 reps" not in payload["description"]
        assert "Notes: Race week" not in payload["description"]

    def test_falls_back_to_table_cells_when_no_detail(self):
        row = {
            "day_name": "Mon",
            "date": "2026-05-11",
            "workout": "Easy 4mi",
            "pace_target": "8:30",
            "notes": "Race week begins",
        }
        payload, _ = sync._build_event_payload(row, sync._event_id(row["date"]))
        assert "Pace: 8:30" in payload["description"]
        assert "Notes: Race week begins" in payload["description"]

    def test_falls_back_when_detail_is_whitespace(self):
        row = {
            "day_name": "Mon",
            "date": "2026-05-11",
            "workout": "Easy 4mi",
            "pace_target": "8:30",
            "notes": "Race week begins",
        }
        payload, _ = sync._build_event_payload(row, sync._event_id(row["date"]), "   \n  \n")
        assert "Pace: 8:30" in payload["description"]

    def test_hash_changes_when_only_detail_changes(self):
        row = {
            "day_name": "Tue",
            "date": "2026-05-12",
            "workout": "5mi w/ 3x1000m",
            "pace_target": "6:00",
            "notes": "x",
        }
        eid = sync._event_id(row["date"])
        _, h_no_detail = sync._build_event_payload(row, eid, None)
        _, h_v1 = sync._build_event_payload(row, eid, "version one body")
        _, h_v2 = sync._build_event_payload(row, eid, "version two body")
        assert h_no_detail != h_v1
        assert h_v1 != h_v2

    def test_long_detail_truncated(self):
        big = "x" * (sync.DESCRIPTION_MAX_BYTES + 500)
        row = {
            "day_name": "Sat",
            "date": "2026-05-16",
            "workout": "long",
            "pace_target": "—",
            "notes": "—",
        }
        payload, _ = sync._build_event_payload(row, sync._event_id(row["date"]), big)
        desc = payload["description"]
        assert "…[truncated]" in desc
        # Final description fits within Google's 8 KB byte cap.
        assert len(desc.encode("utf-8")) < 8192

    def test_multibyte_unicode_truncated_by_bytes_not_chars(self):
        # Em-dash is 3 bytes in UTF-8. 3000 em-dashes = 9000 bytes > cap (7000).
        # If the clamp were char-based at 7000, this would NOT truncate but would
        # still send a 9 KB description and 400 from the API.
        big = "—" * 3000
        row = {
            "day_name": "Sat",
            "date": "2026-05-16",
            "workout": "long",
            "pace_target": "—",
            "notes": "—",
        }
        payload, _ = sync._build_event_payload(row, sync._event_id(row["date"]), big)
        desc = payload["description"]
        assert "…[truncated]" in desc
        assert len(desc.encode("utf-8")) < 8192


class TestSyncPlan:
    def test_dry_run_makes_no_api_calls(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        api_calls = {"insert": 0, "patch": 0, "delete": 0, "list": 0}

        def _boom(*a, **kw):  # noqa: ARG001
            raise AssertionError("API should not be called in dry-run")

        monkeypatch.setattr(gcal_client, "insert_event", _boom)
        monkeypatch.setattr(gcal_client, "patch_event", _boom)
        monkeypatch.setattr(gcal_client, "delete_event", _boom)

        def _list(*a, **kw):  # noqa: ARG001
            api_calls["list"] += 1
            return []

        monkeypatch.setattr(gcal_client, "list_managed_events", _list)

        result = sync.sync_plan(_StubState(PLAN_FIXTURE), dry_run=True)
        assert result["dry_run"] is True
        assert result["inserted"] == 4  # all rows look new
        assert result["patched"] == 0
        assert result["deleted"] == 0
        # Sync state file should NOT be written in dry-run
        assert not patched_sync_state.exists()

    def test_inserts_all_when_state_empty(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        inserts = []

        def _insert(payload):
            inserts.append(payload["id"])
            return {"id": payload["id"]}

        monkeypatch.setattr(gcal_client, "insert_event", _insert)
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(_StubState(PLAN_FIXTURE), dry_run=False)
        assert result["inserted"] == 4
        assert result["patched"] == 0
        assert result["deleted"] == 0
        assert result["unchanged"] == 0
        assert result["errors"] == []
        assert sorted(inserts) == [
            "pretrain20260508",
            "pretrain20260509",
            "pretrain20260511",
            "pretrain20260516",
        ]
        # Sync state file written
        assert patched_sync_state.exists()
        state = json.loads(patched_sync_state.read_text())
        assert set(state.keys()) == set(inserts)
        for v in state.values():
            assert "hash" in v and "last_synced_at" in v

    def test_409_on_insert_falls_through_to_patch(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        def _insert(_payload):
            raise gcal_client.GcalEventExistsError("dup")

        patches = []

        def _patch(event_id, patch):
            patches.append(event_id)
            return {"id": event_id}

        monkeypatch.setattr(gcal_client, "insert_event", _insert)
        monkeypatch.setattr(gcal_client, "patch_event", _patch)
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(_StubState(PLAN_FIXTURE), dry_run=False)
        assert result["inserted"] == 0
        assert result["patched"] == 4
        assert sorted(patches) == [
            "pretrain20260508",
            "pretrain20260509",
            "pretrain20260511",
            "pretrain20260516",
        ]

    def test_unchanged_when_hash_matches(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        # First sync: insert all, recording hashes.
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])
        sync.sync_plan(_StubState(PLAN_FIXTURE), dry_run=False)

        # Second sync: nothing should be inserted/patched.
        def _boom_insert(_p):
            raise AssertionError("should not insert when hash matches")

        def _boom_patch(_id, _p):
            raise AssertionError("should not patch when hash matches")

        monkeypatch.setattr(gcal_client, "insert_event", _boom_insert)
        monkeypatch.setattr(gcal_client, "patch_event", _boom_patch)

        result = sync.sync_plan(_StubState(PLAN_FIXTURE), dry_run=False)
        assert result["inserted"] == 0
        assert result["patched"] == 0
        assert result["unchanged"] == 4

    def test_prune_deletes_orphans_in_window(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        # Plan only has Mon-2026-05-11.
        plan_text = """\
| Day | Date | Workout | Pace target | Notes |
| Mon | 2026-05-11 | Easy 4mi | x | y |
"""
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})

        # Existing managed event for 2026-05-12 (orphan) should be deleted.
        def _list(*a, **kw):  # noqa: ARG001
            return [
                {"id": "pretrain20260511", "start": {"date": "2026-05-11"}},
                {"id": "pretrain20260512", "start": {"date": "2026-05-12"}},
            ]

        monkeypatch.setattr(gcal_client, "list_managed_events", _list)

        deletes = []

        def _delete(event_id):
            deletes.append(event_id)

        monkeypatch.setattr(gcal_client, "delete_event", _delete)

        result = sync.sync_plan(_StubState(plan_text), dry_run=False)
        assert result["inserted"] == 1
        assert result["deleted"] == 1
        assert deletes == ["pretrain20260512"]

    def test_detail_only_edit_triggers_patch_on_next_sync(self, monkeypatch, patched_sync_state, fixed_today):
        """Regression: changing only the #### YYYY-MM-DD body (no table edit)
        must produce patched=1, unchanged=N-1 on the next sync. The user
        verified this manually (Tue 5/12 + Sat 5/16) — pin it."""
        from google_calendar import client as gcal_client

        plan_v1 = """\
| Day | Date | Workout | Pace target | Notes |
| Mon | 2026-05-11 | Easy 4mi | 8:30 | Race week |
| Tue | 2026-05-12 | 5mi w/ 3x1000m | 6:00-6:05 | Race week |

#### 2026-05-12
v1 detail body
"""
        plan_v2 = plan_v1.replace("v1 detail body", "v2 detail body — refined")

        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        # First sync: both rows inserted.
        result1 = sync.sync_plan(_StubState(plan_v1), dry_run=False)
        assert result1["inserted"] == 2
        assert result1["patched"] == 0

        # Second sync: only the detail body changed. The hash for Tue differs
        # from the stored hash → re-attempts insert (matches real flow); the
        # event already exists in gcal → 409 → falls through to patch. Mon's
        # hash unchanged → no API call.
        patches = []

        def _409_on_existing(p):
            raise gcal_client.GcalEventExistsError(f"dup {p['id']}")

        monkeypatch.setattr(gcal_client, "insert_event", _409_on_existing)
        monkeypatch.setattr(
            gcal_client,
            "patch_event",
            lambda eid, p: patches.append(eid) or {"id": eid},
        )

        result2 = sync.sync_plan(_StubState(plan_v2), dry_run=False)
        assert result2["patched"] == 1
        assert result2["unchanged"] == 1
        assert result2["errors"] == []
        assert patches == ["pretrain20260512"]

    def test_detail_block_flows_into_inserted_event(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        plan_text = """\
| Day | Date | Workout | Pace target | Notes |
| Tue | 2026-05-12 | 5mi w/ 3x1000m | 6:00-6:05 | Race week |

### Workout Notes

#### 2026-05-12
Sharpening, not testing. Feel like you could've done one more.
"""
        captured = []
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: captured.append(p) or {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(_StubState(plan_text), dry_run=False)
        assert result["inserted"] == 1
        assert len(captured) == 1
        desc = captured[0]["description"]
        assert desc.startswith("Sharpening, not testing.")
        assert "Pace: 6:00-6:05" not in desc  # rich body replaces table fallback
        assert desc.endswith("(synced by PRE)")

    def test_per_row_failure_does_not_abort_batch(self, monkeypatch, patched_sync_state, fixed_today):
        from google_calendar import client as gcal_client

        def _flaky_insert(p):
            if p["id"] == "pretrain20260509":
                raise gcal_client.GcalAPIError("synthetic")
            return {"id": p["id"]}

        monkeypatch.setattr(gcal_client, "insert_event", _flaky_insert)
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(_StubState(PLAN_FIXTURE), dry_run=False)
        # 4 rows total; 1 fails, 3 succeed.
        assert result["inserted"] == 3
        assert len(result["errors"]) == 1
        assert result["errors"][0]["date"] == "2026-05-09"


class TestSyncStateRoundtrip:
    def test_corrupt_state_file_treated_as_empty(self, patched_sync_state):
        patched_sync_state.write_text("{not json")
        assert sync._load_sync_state() == {}

    def test_write_then_read(self, patched_sync_state):
        sync._write_sync_state({"pretrain20260511": {"hash": "abc", "last_synced_at": "x"}})
        loaded = sync._load_sync_state()
        assert loaded == {"pretrain20260511": {"hash": "abc", "last_synced_at": "x"}}
