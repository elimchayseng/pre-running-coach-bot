"""Tests for google_calendar.sync — row parsing, insert/patch/delete
accounting, dry-run, hash-skip, sync-state roundtrip, mark_complete, and
reconcile_completion."""

from __future__ import annotations

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


class _StateStub:
    """StateManager-shaped stub for sync tests — keeps gcal sync state in
    memory so we don't need a real SQLite DB inside unit tests.

    Supports the four methods sync.py touches: ``load_plan``,
    ``sessions_on_date``, ``load_gcal_sync_state``, ``save_gcal_sync_state``.
    """

    def __init__(self, plan_text: str = "", sessions: dict[str, list[dict]] | None = None):
        self._plan = plan_text
        self._sessions = sessions or {}
        self.gcal_sync: dict[str, dict] = {}

    def load_plan(self) -> str:
        return self._plan

    def sessions_on_date(self, d) -> list[dict]:
        return list(self._sessions.get(d.isoformat(), []))

    def load_gcal_sync_state(self) -> dict[str, dict]:
        # Mimic the real load: drop entries with falsy values for parity.
        import copy

        return copy.deepcopy(self.gcal_sync)

    def save_gcal_sync_state(self, state: dict[str, dict]) -> None:
        import copy

        self.gcal_sync = copy.deepcopy(state)


@pytest.fixture
def state_stub() -> _StateStub:
    return _StateStub(PLAN_FIXTURE)


@pytest.fixture
def fixed_today(monkeypatch):
    """Pin today_local() to 2026-05-11 so prune windows are deterministic."""
    monkeypatch.setattr(sync, "today_local", lambda: date(2026, 5, 11))
    yield date(2026, 5, 11)


class TestParsePlanRows:
    def test_parses_locked_format_rows_only(self):
        rows = sync._parse_plan_rows(PLAN_FIXTURE)
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
        assert len(desc.encode("utf-8")) < 8192

    def test_multibyte_unicode_truncated_by_bytes_not_chars(self):
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
    def test_dry_run_makes_no_api_calls(self, monkeypatch, state_stub, fixed_today):
        from google_calendar import client as gcal_client

        def _boom(*a, **kw):  # noqa: ARG001
            raise AssertionError("API should not be called in dry-run")

        monkeypatch.setattr(gcal_client, "insert_event", _boom)
        monkeypatch.setattr(gcal_client, "patch_event", _boom)
        monkeypatch.setattr(gcal_client, "delete_event", _boom)
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(state_stub, dry_run=True)
        assert result["dry_run"] is True
        assert result["inserted"] == 4
        assert result["patched"] == 0
        assert result["deleted"] == 0
        # No state written in dry-run
        assert state_stub.gcal_sync == {}
        # Reconcile is skipped in dry-run
        assert result["reconcile"] is None

    def test_inserts_all_when_state_empty(self, monkeypatch, state_stub, fixed_today):
        from google_calendar import client as gcal_client

        inserts = []
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: inserts.append(p["id"]) or {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(state_stub, dry_run=False)
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
        assert set(state_stub.gcal_sync.keys()) == set(inserts)
        for v in state_stub.gcal_sync.values():
            assert "hash" in v and "last_synced_at" in v
        # Reconcile ran (no sessions seeded → all skipped, no errors)
        assert result["reconcile"]["corrected"] == []
        assert result["reconcile"]["errors"] == []

    def test_409_on_insert_falls_through_to_patch(self, monkeypatch, state_stub, fixed_today):
        from google_calendar import client as gcal_client

        def _insert(_payload):
            raise gcal_client.GcalEventExistsError("dup")

        patches = []
        monkeypatch.setattr(gcal_client, "insert_event", _insert)
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: patches.append(eid) or {"id": eid})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(state_stub, dry_run=False)
        assert result["inserted"] == 0
        assert result["patched"] == 4
        assert sorted(patches) == [
            "pretrain20260508",
            "pretrain20260509",
            "pretrain20260511",
            "pretrain20260516",
        ]

    def test_unchanged_when_hash_matches(self, monkeypatch, state_stub, fixed_today):
        from google_calendar import client as gcal_client

        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])
        sync.sync_plan(state_stub, dry_run=False)

        def _boom_insert(_p):
            raise AssertionError("should not insert when hash matches")

        def _boom_patch(_id, _p):
            raise AssertionError("should not patch when hash matches")

        monkeypatch.setattr(gcal_client, "insert_event", _boom_insert)
        monkeypatch.setattr(gcal_client, "patch_event", _boom_patch)

        result = sync.sync_plan(state_stub, dry_run=False)
        assert result["inserted"] == 0
        assert result["patched"] == 0
        assert result["unchanged"] == 4

    def test_prune_deletes_orphans_in_window(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        plan_text = """\
| Day | Date | Workout | Pace target | Notes |
| Mon | 2026-05-11 | Easy 4mi | x | y |
"""
        state = _StateStub(plan_text)
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(
            gcal_client,
            "list_managed_events",
            lambda *a, **kw: [
                {"id": "pretrain20260511", "start": {"date": "2026-05-11"}},
                {"id": "pretrain20260512", "start": {"date": "2026-05-12"}},
            ],
        )
        deletes: list[str] = []
        monkeypatch.setattr(gcal_client, "delete_event", lambda eid: deletes.append(eid))

        result = sync.sync_plan(state, dry_run=False)
        assert result["inserted"] == 1
        assert result["deleted"] == 1
        assert deletes == ["pretrain20260512"]

    def test_detail_only_edit_triggers_patch_on_next_sync(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        plan_v1 = """\
| Day | Date | Workout | Pace target | Notes |
| Mon | 2026-05-11 | Easy 4mi | 8:30 | Race week |
| Tue | 2026-05-12 | 5mi w/ 3x1000m | 6:00-6:05 | Race week |

#### 2026-05-12
v1 detail body
"""
        plan_v2 = plan_v1.replace("v1 detail body", "v2 detail body — refined")

        state = _StateStub(plan_v1)
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result1 = sync.sync_plan(state, dry_run=False)
        assert result1["inserted"] == 2
        assert result1["patched"] == 0

        patches = []

        def _409(p):
            raise gcal_client.GcalEventExistsError(f"dup {p['id']}")

        monkeypatch.setattr(gcal_client, "insert_event", _409)
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: patches.append(eid) or {"id": eid})

        state._plan = plan_v2
        result2 = sync.sync_plan(state, dry_run=False)
        assert result2["patched"] == 1
        assert result2["unchanged"] == 1
        assert result2["errors"] == []
        assert patches == ["pretrain20260512"]

    def test_detail_block_flows_into_inserted_event(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        plan_text = """\
| Day | Date | Workout | Pace target | Notes |
| Tue | 2026-05-12 | 5mi w/ 3x1000m | 6:00-6:05 | Race week |

### Workout Notes

#### 2026-05-12
Sharpening, not testing. Feel like you could've done one more.
"""
        state = _StateStub(plan_text)
        captured = []
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: captured.append(p) or {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(state, dry_run=False)
        assert result["inserted"] == 1
        assert len(captured) == 1
        desc = captured[0]["description"]
        assert desc.startswith("Sharpening, not testing.")
        assert "Pace: 6:00-6:05" not in desc
        assert desc.endswith("(synced by PRE)")

    def test_per_row_failure_does_not_abort_batch(self, monkeypatch, state_stub, fixed_today):
        from google_calendar import client as gcal_client

        def _flaky_insert(p):
            if p["id"] == "pretrain20260509":
                raise gcal_client.GcalAPIError("synthetic")
            return {"id": p["id"]}

        monkeypatch.setattr(gcal_client, "insert_event", _flaky_insert)
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])

        result = sync.sync_plan(state_stub, dry_run=False)
        assert result["inserted"] == 3
        # Errors may include the per-row failure and (if any) reconcile noise;
        # filter for the row error we care about.
        row_errors = [e for e in result["errors"] if e["date"] == "2026-05-09"]
        assert len(row_errors) == 1


class TestSyncStateRoundtrip:
    def test_empty_state_returns_empty_dict(self):
        state = _StateStub()
        assert sync._load_sync_state(state) == {}

    def test_write_then_read(self):
        state = _StateStub()
        sync._write_sync_state(state, {"pretrain20260511": {"hash": "abc", "last_synced_at": "x"}})
        loaded = sync._load_sync_state(state)
        assert loaded == {"pretrain20260511": {"hash": "abc", "last_synced_at": "x"}}


class TestPrescriptionClassifier:
    def test_rest_day(self):
        assert sync._prescription_kind("Rest + gentle yoga PM") == "rest"
        assert sync._prescription_kind("Off day") == "rest"
        assert sync._prescription_kind("—") == "rest"

    def test_run(self):
        assert sync._prescription_kind("Easy 8mi STRICT") == "run"
        assert sync._prescription_kind("**BROOKLYN HALF**") == "run"
        assert sync._prescription_kind("5mi w/ 3x1000m") == "run"

    def test_cross_train(self):
        assert sync._prescription_kind("Cycling 60-75min, NO climbing") == "cross_train"
        assert sync._prescription_kind("Optional 20min spin OR rest") == "cross_train"

    def test_run_with_yoga_adjunct_classifies_as_run(self):
        assert sync._prescription_kind("Easy 4mi + restorative yoga PM") == "run"


class TestLogMatchesPrescription:
    def test_run_matches_run_types(self):
        for t in ("run", "easy", "long_run", "workout", "race", "strides"):
            assert sync._log_matches_prescription("run", t)
        assert not sync._log_matches_prescription("run", "strength")
        assert not sync._log_matches_prescription("run", "cross_train")

    def test_rest_never_matches(self):
        assert not sync._log_matches_prescription("rest", "run")
        assert not sync._log_matches_prescription("rest", "easy")

    def test_cross_train_matches_only_cross_train(self):
        assert sync._log_matches_prescription("cross_train", "cross_train")
        assert not sync._log_matches_prescription("cross_train", "run")


_COMPLETION_PLAN = """\
| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Sat | 2026-05-09 | Easy 8mi STRICT | 8:30-9:00, HR ≤155 | foo |
| Mon | 2026-05-11 | Rest + gentle yoga PM | — | Race week |
"""


class TestMarkComplete:
    def test_matching_run_patches_prescription_event(self, monkeypatch):
        from google_calendar import client as gcal_client

        patches: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            gcal_client,
            "insert_event",
            lambda p: (_ for _ in ()).throw(gcal_client.GcalEventExistsError("exists")),
        )
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: patches.append((eid, p)) or {})

        state = _StateStub(
            _COMPLETION_PLAN,
            {"2026-05-09": [{"date": "2026-05-09", "type": "easy", "miles": 8.1, "pace_avg": "8:42"}]},
        )
        result = sync.mark_complete(state, date(2026, 5, 9))

        assert result["ok"] is True
        assert result["prescription_kind"] == "run"
        assert result["prescribed"]["action"] == "patched"
        assert result["prescribed"]["event_id"] == "pretrain20260509"
        assert "off_plan" not in result
        eid, payload = patches[0]
        assert eid == "pretrain20260509"
        assert payload["summary"].startswith("✅ ")
        assert payload["colorId"] == "8"
        assert payload["extendedProperties"]["private"]["pre_completed"] == "1"
        assert "--- Completed ---" in payload["description"]
        assert "8.1mi" in payload["description"]

    def test_off_plan_strength_on_run_day_does_not_mark_prescription(self, monkeypatch):
        from google_calendar import client as gcal_client

        inserts: list[dict] = []
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: inserts.append(p) or {})
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: None)

        state = _StateStub(_COMPLETION_PLAN, {"2026-05-09": [{"date": "2026-05-09", "type": "strength"}]})
        result = sync.mark_complete(state, date(2026, 5, 9))

        assert "prescribed" not in result
        assert result["off_plan"]["action"] == "inserted"
        assert result["off_plan"]["event_id"] == "precomplete20260509"
        assert len(inserts) == 1
        assert inserts[0]["id"] == "precomplete20260509"

    def test_aggregates_multiple_sessions_on_one_day(self, monkeypatch):
        from google_calendar import client as gcal_client

        captured: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            gcal_client,
            "insert_event",
            lambda p: (_ for _ in ()).throw(gcal_client.GcalEventExistsError("x")),
        )
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: captured.append((eid, p)) or {})

        state = _StateStub(
            _COMPLETION_PLAN,
            {
                "2026-05-11": [
                    {"date": "2026-05-11", "type": "strength"},
                    {"date": "2026-05-11", "type": "cross_train", "miles": 12.0},
                ]
            },
        )
        result = sync.mark_complete(state, date(2026, 5, 11))
        assert "prescribed" not in result
        assert result["off_plan"]["action"] == "patched"
        eid, payload = captured[0]
        assert eid == "precomplete20260511"
        desc = payload["description"]
        assert "strength" in desc
        assert "cross_train" in desc
        assert "12.0mi" in desc

    def test_cleans_up_stale_precomplete_when_log_reclassifies_to_matching(self, monkeypatch):
        from google_calendar import client as gcal_client

        state = _StateStub(_COMPLETION_PLAN)
        sync._write_sync_state(
            state,
            {
                "precomplete20260509": {
                    "completed": True,
                    "off_plan": True,
                    "last_completed_at": "2026-05-09T17:00:00Z",
                }
            },
        )

        deletes: list[str] = []
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: {"id": eid})
        monkeypatch.setattr(gcal_client, "delete_event", lambda eid: deletes.append(eid))

        state._sessions = {"2026-05-09": [{"date": "2026-05-09", "type": "easy", "miles": 8.0}]}
        result = sync.mark_complete(state, date(2026, 5, 9))

        assert result["prescribed"]["action"] in ("inserted", "patched")
        assert result.get("off_plan_cleanup") == "deleted"
        assert deletes == ["precomplete20260509"]
        state_after = sync._load_sync_state(state)
        assert "precomplete20260509" not in state_after

    def test_sync_plan_skips_completed_events(self, monkeypatch):
        from google_calendar import client as gcal_client

        state = _StateStub(_COMPLETION_PLAN)
        sync._write_sync_state(
            state,
            {
                "pretrain20260509": {
                    "hash": "stale-hash",
                    "completed": True,
                    "last_completed_at": "2026-05-09T18:00:00Z",
                }
            },
        )

        touched: list[str] = []
        monkeypatch.setattr(
            gcal_client,
            "insert_event",
            lambda p: (touched.append(p["id"]) if p["id"] == "pretrain20260509" else None) or {"id": p["id"]},
        )
        monkeypatch.setattr(
            gcal_client,
            "patch_event",
            lambda eid, p: (touched.append(eid) if eid == "pretrain20260509" else None) or {"id": eid},
        )
        monkeypatch.setattr(gcal_client, "list_managed_events", lambda *a, **kw: [])
        monkeypatch.setattr(sync, "today_local", lambda: date(2026, 5, 11))

        result = sync.sync_plan(state, dry_run=False)
        assert touched == []
        assert result["unchanged"] == 1  # the completed row
        assert result["inserted"] == 1  # the 05-11 row


# ---------------- reconcile_completion ----------------


class TestReconcileCompletion:
    PLAN = """\
| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Sat | 2026-05-09 | Easy 8mi | 8:30 | a |
| Mon | 2026-05-11 | Rest + yoga | — | b |
| Tue | 2026-05-12 | Easy 5mi | 8:30 | c |
"""

    def test_marks_dates_with_matching_logs(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        monkeypatch.setattr(
            gcal_client,
            "insert_event",
            lambda p: (_ for _ in ()).throw(gcal_client.GcalEventExistsError("x")),
        )
        patches: list[str] = []
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: patches.append(eid) or {})

        state = _StateStub(
            self.PLAN,
            {"2026-05-09": [{"date": "2026-05-09", "type": "easy", "miles": 8.0}]},
        )
        out = sync.reconcile_completion(state, days_back=14)

        assert len(out["corrected"]) == 1
        assert out["corrected"][0]["date"] == "2026-05-09"
        assert out["corrected"][0]["prescribed"] is True
        assert "pretrain20260509" in patches

    def test_skips_dates_without_logs(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: {"id": eid})

        state = _StateStub(self.PLAN, {})  # no log entries
        out = sync.reconcile_completion(state, days_back=14)

        assert out["corrected"] == []
        # All three plan dates in window have no sessions → all skipped.
        assert set(out["skipped"]) == {"2026-05-09", "2026-05-11"}  # 05-12 > today

    def test_idempotent_when_already_completed(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        # Seed sync state with completed=True for the matching date.
        state = _StateStub(
            self.PLAN,
            {"2026-05-09": [{"date": "2026-05-09", "type": "easy", "miles": 8.0}]},
        )
        sync._write_sync_state(
            state,
            {
                "pretrain20260509": {
                    "hash": "h",
                    "completed": True,
                    "last_completed_at": "2026-05-09T18:00:00Z",
                }
            },
        )

        # mark_complete still patches when reconcile re-fires, but the sync
        # state stays marked completed (no flip-flop).
        monkeypatch.setattr(
            gcal_client,
            "insert_event",
            lambda p: (_ for _ in ()).throw(gcal_client.GcalEventExistsError("x")),
        )
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: {"id": eid})

        out = sync.reconcile_completion(state, days_back=14)
        assert len(out["corrected"]) == 1
        # Completed flag preserved
        assert state.gcal_sync["pretrain20260509"]["completed"] is True

    def test_orphan_surfaced_not_uncompleted(self, monkeypatch, fixed_today):
        from google_calendar import client as gcal_client

        # gcal sync state claims completed for a date with NO log entries.
        state = _StateStub(self.PLAN, {})  # no logs at all
        sync._write_sync_state(
            state,
            {
                "pretrain20260509": {
                    "hash": "h",
                    "completed": True,
                    "last_completed_at": "2026-05-09T18:00:00Z",
                }
            },
        )

        monkeypatch.setattr(gcal_client, "insert_event", lambda p: {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: {"id": eid})

        out = sync.reconcile_completion(state, days_back=14)
        assert any(o["event_id"] == "pretrain20260509" for o in out["orphans"])
        # Did NOT modify the gcal_sync_state to uncomplete
        assert state.gcal_sync["pretrain20260509"]["completed"] is True

    def test_rest_day_with_run_log_classified_off_plan(self, monkeypatch, fixed_today):
        """Logging a run on a Rest day → mark_complete partitions to off_plan,
        not prescribed. Reconcile inherits that."""
        from google_calendar import client as gcal_client

        inserts: list[dict] = []
        monkeypatch.setattr(gcal_client, "insert_event", lambda p: inserts.append(p) or {"id": p["id"]})
        monkeypatch.setattr(gcal_client, "patch_event", lambda eid, p: {"id": eid})

        state = _StateStub(
            self.PLAN,
            {"2026-05-11": [{"date": "2026-05-11", "type": "easy", "miles": 4.0}]},  # logged on a rest day
        )
        out = sync.reconcile_completion(state, days_back=14)
        # Date is corrected, but it's an off_plan completion, not prescribed.
        [entry] = [c for c in out["corrected"] if c["date"] == "2026-05-11"]
        assert entry["prescribed"] is False
        assert entry["off_plan"] is True
