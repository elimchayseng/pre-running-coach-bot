"""State manager tests against a fresh SQLite DB per test."""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from state_manager import (
    StateManager,
    slot_bucket_center,
    slot_display_label,
    slot_time_bucket,
)

ATHLETE_YAML = """\
name: TestRunner
# inline comment about target_races
target_races:
  - name: Test Half
    date: 2026-05-16
    priority: B
zones:
  threshold: "6:15"  # current
"""

PLAN_WITH_WEEK = """\
# Plan

## This Week (2026-04-27 → 2026-05-03)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Mon | 2026-04-27 | Off / strength upper | — | |
| Tue | 2026-04-28 | Easy 4mi + 4 strides | 8:45-9:15 | Strides 20s |
| Wed | 2026-04-29 | Cross-train 30-45min | — | |
| Sat | 2026-05-02 | Easy 6mi rolling | 8:45-9:15 | Some climb |
"""


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    return d


@pytest.fixture
def state(state_dir: Path) -> StateManager:
    return StateManager(state_dir)


class TestSlotHelpers:
    """slot_time_bucket / slot_bucket_center / slot_display_label — shared by GCal + Strava."""

    def test_single_session_returns_no_bucket(self):
        assert slot_time_bucket(None, 1) is None
        assert slot_bucket_center(None, 1) is None
        assert slot_display_label(None, 1) == ""

    def test_two_a_day_uses_am_pm_layout(self):
        assert slot_time_bucket("1", 2) == (6.0, 7.5)
        assert slot_time_bucket("2", 2) == (17.5, 19.0)
        assert slot_display_label("1", 2) == "AM"
        assert slot_display_label("2", 2) == "PM"

    def test_three_a_day_uses_morning_midday_evening(self):
        assert slot_time_bucket("1", 3) == (6.0, 7.5)
        assert slot_time_bucket("2", 3) == (12.0, 13.0)
        assert slot_time_bucket("3", 3) == (17.5, 19.0)
        assert slot_display_label("2", 3) == "2/3"

    def test_four_a_day_uses_four_buckets(self):
        assert slot_time_bucket("1", 4) == (6.0, 7.5)
        assert slot_time_bucket("4", 4) == (18.0, 19.0)

    def test_five_plus_spreads_linearly(self):
        """5+ sessions distribute evenly between centers 6.5 and 19.5."""
        first = slot_time_bucket("1", 5)
        last = slot_time_bucket("5", 5)
        assert first == (6.0, 7.0)
        assert last == (19.0, 20.0)
        mid = slot_time_bucket("3", 5)
        assert mid == (12.5, 13.5)  # center 13.0 is the midpoint of 6.5↔19.5

    def test_bucket_center_is_midpoint(self):
        assert slot_bucket_center("1", 2) == 6.75
        assert slot_bucket_center("2", 2) == 18.25

    def test_invalid_slot_returns_none(self):
        assert slot_time_bucket("abc", 2) is None
        assert slot_time_bucket("0", 2) is None
        assert slot_time_bucket("3", 2) is None  # idx > total
        assert slot_display_label("abc", 2) == ""


def _seed_athlete(state: StateManager, yaml_text: str = ATHLETE_YAML) -> None:
    """Insert an athlete row directly (mirrors what the migration does)."""
    with state._conn() as c:
        c.execute(
            "INSERT INTO athlete (id, yaml_text) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET yaml_text = excluded.yaml_text",
            (yaml_text,),
        )


# ---------------- athlete ----------------


class TestAthlete:
    def test_load_returns_parsed_dict(self, state):
        _seed_athlete(state)
        data = state.load_athlete()
        assert data["name"] == "TestRunner"
        assert data["target_races"][0]["name"] == "Test Half"

    def test_load_missing_returns_empty(self, state):
        assert state.load_athlete() == {}

    def test_update_patches_top_level(self, state):
        _seed_athlete(state)
        state.update_athlete({"name": "Renamed"})
        assert state.load_athlete()["name"] == "Renamed"

    def test_update_preserves_comments(self, state):
        _seed_athlete(state)
        state.update_athlete({"name": "Renamed"})
        # Pull the raw YAML back from the DB
        with state._conn() as c:
            row = c.execute("SELECT yaml_text FROM athlete WHERE id=1").fetchone()
        text = row["yaml_text"]
        assert "inline comment about target_races" in text
        assert "# current" in text

    def test_update_deep_merges_nested(self, state):
        _seed_athlete(state)
        state.update_athlete({"zones": {"easy": "8:30-9:00"}})
        data = state.load_athlete()
        assert data["zones"]["threshold"] == "6:15"  # untouched
        assert data["zones"]["easy"] == "8:30-9:00"

    def test_update_missing_row_raises(self, state):
        with pytest.raises(FileNotFoundError):
            state.update_athlete({"name": "X"})


# ---------------- plan_meta + changelog ----------------


class TestPlan:
    def test_update_plan_parses_rows_and_meta(self, state):
        state.update_plan(PLAN_WITH_WEEK, "seed")
        # Table rows landed as planned sessions.
        w = state.get_todays_workout(date(2026, 4, 28))
        assert w["found"] is True
        assert w["workout"] == "Easy 4mi + 4 strides"
        assert w["status"] == "planned"
        # Prose outside the table landed in plan_meta.
        assert "# Plan" in state.get_plan_meta()
        assert "| Day |" not in state.get_plan_meta()

    def test_get_plan_meta_empty_initially(self, state):
        assert state.get_plan_meta() == ""

    def test_update_plan_appends_changelog(self, state):
        state.update_plan(PLAN_WITH_WEEK, "Brooklyn taper week")
        with state._conn() as c:
            row = c.execute("SELECT content FROM plan_changelog WHERE id=1").fetchone()
        assert "Brooklyn taper week" in row["content"]

    def test_changelog_accumulates(self, state):
        state.update_plan(PLAN_WITH_WEEK, "first edit")
        state.update_plan(PLAN_WITH_WEEK, "second edit")
        with state._conn() as c:
            log = c.execute("SELECT content FROM plan_changelog WHERE id=1").fetchone()["content"]
        assert "first edit" in log and "second edit" in log

    def test_update_plan_meta_replaces_prose(self, state):
        state.update_plan_meta("# Goals\n\nSub-3 marathon.", "set goals")
        assert "Sub-3 marathon" in state.get_plan_meta()

    def test_render_plan_includes_meta_and_week(self, state):
        state.update_plan(PLAN_WITH_WEEK, "seed")
        out = state.render_plan(today=date(2026, 4, 28))
        assert "## This week" in out
        assert "Easy 4mi + 4 strides" in out


# ---------------- surgical plan edits ----------------


PLAN_WITH_DETAIL = """\
# Plan

## This Week (2026-04-27 → 2026-05-03)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Mon | 2026-04-27 | Off / strength upper | — | |
| Tue | 2026-04-28 | Easy 4mi + 4 strides | 8:45-9:15 | Strides 20s |
| Wed | 2026-04-29 | Cross-train 30-45min | — | |
| Sat | 2026-05-02 | Easy 6mi rolling | 8:45-9:15 | Some climb |

#### 2026-04-28

Original detail prose for Tuesday strides session.
"""


class TestUpdateWorkout:
    """Single-day prescription patches via state.update_workout."""

    def test_patch_single_cell_preserves_others(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="bump strides count",
            workout="Easy 4mi + 6 strides",
        )
        w = state.get_todays_workout(date(2026, 4, 28))
        assert w["workout"] == "Easy 4mi + 6 strides"
        # Pace and notes for that row stayed.
        assert w["pace_target"] == "8:45-9:15"
        assert w["notes"] == "Strides 20s"
        # Other rows untouched.
        assert state.get_todays_workout(date(2026, 4, 27))["workout"] == "Off / strength upper"
        assert state.get_todays_workout(date(2026, 5, 2))["workout"] == "Easy 6mi rolling"

    def test_patch_multiple_fields_in_one_call(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="bump everything",
            workout="Tempo 6mi",
            pace_target="6:30-6:40",
            notes="focus on relaxed shoulders",
        )
        w = state.get_todays_workout(date(2026, 4, 28))
        assert w["workout"] == "Tempo 6mi"
        assert w["pace_target"] == "6:30-6:40"
        assert w["notes"] == "focus on relaxed shoulders"

    def test_unknown_date_raises_with_self_routing_hint(self, state):
        """Date with no row in the locked table raises ValueError whose
        message tells the LLM exactly which other tool to reach for."""
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        with pytest.raises(ValueError) as excinfo:
            state.update_workout(
                target_date=date(2030, 1, 1),
                change_note="add a day",
                workout="Easy 5mi",
            )
        msg = str(excinfo.value)
        assert "2030-01-01" in msg
        assert "replace_week_table" in msg
        assert "update_plan" in msg

    def test_no_fields_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        with pytest.raises(ValueError, match="must pass at least one"):
            state.update_workout(target_date=date(2026, 4, 28), change_note="x")

    def test_empty_plan_raises_with_self_routing_hint(self, state):
        """No plan yet → update_workout points the LLM at replace_week_table
        / update_plan rather than silently seeding an orphan row."""
        with pytest.raises(ValueError) as excinfo:
            state.update_workout(
                target_date=date(2026, 4, 28),
                change_note="x",
                workout="Easy 5mi",
            )
        msg = str(excinfo.value)
        assert "replace_week_table" in msg
        assert "update_plan" in msg

    def test_detail_body_only_no_row_edit(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="enrich detail",
            detail_body="Updated prose for Tuesday.\nFocus on cadence.",
        )
        row = state.get_workout_row(date(2026, 4, 28))
        assert row["detail_md"] == "Updated prose for Tuesday.\nFocus on cadence."
        # Locked prescription unchanged.
        assert row["prescribed_workout"] == "Easy 4mi + 4 strides"

    def test_detail_body_sets_when_missing(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 5, 2),
            change_note="add Saturday detail",
            detail_body="Long-run cues for Saturday.",
        )
        assert state.get_workout_row(date(2026, 5, 2))["detail_md"] == "Long-run cues for Saturday."
        # Existing Tuesday detail untouched.
        assert "Original detail prose" in state.get_workout_row(date(2026, 4, 28))["detail_md"]

    def test_combined_row_and_detail_in_one_transaction(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="combined",
            workout="Workout 5x800",
            pace_target="6:00",
            detail_body="WU 1mi easy. Work 5x800. CD 1mi.",
        )
        row = state.get_workout_row(date(2026, 4, 28))
        assert row["prescribed_workout"] == "Workout 5x800"
        assert row["detail_md"] == "WU 1mi easy. Work 5x800. CD 1mi."
        # Single combined edit produces a single changelog entry.
        with state._conn() as c:
            log = c.execute("SELECT content FROM plan_changelog WHERE id=1").fetchone()["content"]
        assert log.count("combined") == 1


class TestReplaceWeekTable:
    """Bulk weekly prescription replacement for block / phase transitions."""

    def test_replaces_rows_in_window(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        new_rows = [
            {"day": "Mon", "date": "2026-04-27", "workout": "Rest", "pace_target": "—", "notes": ""},
            {"day": "Tue", "date": "2026-04-28", "workout": "Easy 5mi", "pace_target": "8:45", "notes": ""},
            {"day": "Wed", "date": "2026-04-29", "workout": "Tempo 6mi", "pace_target": "6:30", "notes": "track"},
        ]
        state.replace_week_table(new_rows, "next week")
        # New prescriptions landed.
        assert state.get_todays_workout(date(2026, 4, 28))["workout"] == "Easy 5mi"
        assert state.get_todays_workout(date(2026, 4, 29))["workout"] == "Tempo 6mi"
        # A planned row outside the window is untouched.
        assert state.get_todays_workout(date(2026, 5, 2))["workout"] == "Easy 6mi rolling"

    def test_preserves_detail_outside_window(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.replace_week_table(
            [{"day": "Mon", "date": "2026-05-11", "workout": "Off", "pace_target": "—", "notes": ""}],
            "next week",
        )
        # The 04-28 row is outside the replaced window — its detail survives.
        assert "Original detail prose" in state.get_workout_row(date(2026, 4, 28))["detail_md"]

    def test_does_not_shadow_a_completed_day(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        # Log an activity on 04-28 — it completes the planned row.
        state.append_session({"date": "2026-04-28", "type": "easy", "miles": 4})
        state.replace_week_table(
            [{"day": "Tue", "date": "2026-04-28", "workout": "Tempo 6mi", "pace_target": "6:30", "notes": ""}],
            "rewrite week",
        )
        # The completed row is preserved; no duplicate planned row was added.
        rows = state.get_rows_in_range(date(2026, 4, 28), date(2026, 4, 28))
        assert len(rows) == 1
        assert rows[0]["status"] == "completed"

    def test_missing_key_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        bad = [{"day": "Mon", "date": "2026-05-04", "workout": "Off"}]
        with pytest.raises(ValueError, match="missing required keys"):
            state.replace_week_table(bad, "x")

    def test_empty_rows_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        with pytest.raises(ValueError, match="rows must be non-empty"):
            state.replace_week_table([], "x")

    def test_duplicate_date_rows_persist_as_two_a_day(self, state):
        state.update_plan(PLAN_WITH_WEEK, "seed")
        new_rows = [
            {"day": "Wed", "date": "2026-05-27", "workout": "5mi easy AM", "pace_target": "Z2", "notes": ""},
            {"day": "Wed", "date": "2026-05-27", "workout": "6x400 PM", "pace_target": "5K", "notes": ""},
            {"day": "Thu", "date": "2026-05-28", "workout": "8mi long", "pace_target": "Z2", "notes": ""},
        ]
        state.replace_week_table(new_rows, "add two-a-day")
        with state._conn() as c:
            wed = c.execute(
                "SELECT slot, prescribed_workout FROM sessions WHERE date = ? ORDER BY slot",
                ("2026-05-27",),
            ).fetchall()
            thu = c.execute("SELECT slot FROM sessions WHERE date = ?", ("2026-05-28",)).fetchone()
        assert [r["slot"] for r in wed] == ["1", "2"]
        assert wed[0]["prescribed_workout"] == "5mi easy AM"
        assert wed[1]["prescribed_workout"] == "6x400 PM"
        assert thu["slot"] is None


PLAN_WITH_TWO_A_DAY = """\
# Plan

## This Week (2026-05-25 → 2026-05-31)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Mon | 2026-05-25 | Easy 4mi | 8:45 | base |
| Wed | 2026-05-27 | 5mi easy (AM) | Z2 | |
| Wed | 2026-05-27 | 6x400 @ 5K (PM) | reps | quality |
| Thu | 2026-05-28 | 8mi long | Z2 | |
"""


class TestSlotPersistence:
    """update_plan + replace_week_table must persist slot ordinals end-to-end."""

    def test_update_plan_assigns_slots_for_two_a_day(self, state):
        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed two-a-day")
        with state._conn() as c:
            rows = c.execute(
                "SELECT date, slot, prescribed_workout FROM sessions WHERE status = 'planned' ORDER BY date, slot, id"
            ).fetchall()
        # Mon and Thu are single-session → slot=None; Wed has two slots "1","2".
        by_date = {(r["date"], r["slot"]): r["prescribed_workout"] for r in rows}
        assert by_date[("2026-05-25", None)] == "Easy 4mi"
        assert by_date[("2026-05-27", "1")] == "5mi easy (AM)"
        assert by_date[("2026-05-27", "2")] == "6x400 @ 5K (PM)"
        assert by_date[("2026-05-28", None)] == "8mi long"

    def test_unique_date_slot_blocks_third_row(self, state):
        """UNIQUE(date, slot) rejects a third row sharing slot with an existing one."""
        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed")
        with state._conn() as c, pytest.raises(sqlite3.IntegrityError):
            c.execute(
                "INSERT INTO sessions (date, slot, status, prescribed_workout) "
                "VALUES ('2026-05-27', '1', 'planned', 'duplicate slot 1')"
            )

    def test_round_trip_render_then_parse_preserves_slots(self, state):
        from plan_markdown import parse_plan_rows, render_week_table

        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed")
        rows = state.get_prescription_rows(date(2026, 5, 25), date(2026, 5, 31))
        # Render rows back to markdown then re-parse.
        md = render_week_table([dict(r) for r in rows])
        reparsed = parse_plan_rows(md)
        wed = [r for r in reparsed if r["date"] == "2026-05-27"]
        assert [r["slot"] for r in wed] == ["1", "2"]

    def test_stamp_total_slots_on_date_annotates_rows(self, state):
        """The mirror notifier annotates each row with the date's slot count."""
        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed")
        rows = state._rows("status = 'planned'", ())
        state._stamp_total_slots_on_date(rows)
        by_date = {(r["date"], r["slot"]): r.get("total_slots_on_date") for r in rows}
        # Mon (single) and Thu (single) → 1; Wed (two-a-day) → 2 for both slots.
        assert by_date[("2026-05-25", None)] == 1
        assert by_date[("2026-05-27", "1")] == 2
        assert by_date[("2026-05-27", "2")] == 2
        assert by_date[("2026-05-28", None)] == 1

    def test_update_plan_rejects_slot_reuse_over_completed_row(self, state):
        """W5 guard: rewriting the plan must not reassign an ordinal that a
        non-planned (completed / missed / off-plan) row already owns."""
        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed")
        # Close Wed slot=2 by routing a PM-time activity to the PM slot.
        state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "workout",
                "start_local": "2026-05-27T18:00:00Z",
                "details": {"strava_id": 4242},
            }
        )
        # Re-apply the same plan: parser assigns slot "2" again, conflicting
        # with the completed PM row.
        with pytest.raises(ValueError, match="cannot reassign slot '2' on 2026-05-27"):
            state.update_plan(PLAN_WITH_TWO_A_DAY, "rewrite same plan")

    def test_update_plan_clean_reapply_when_no_history(self, state):
        """W5 guard does not fire on conflict-free re-apply."""
        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed")
        state.update_plan(PLAN_WITH_TWO_A_DAY, "idempotent re-apply")
        rows = state._rows("status = 'planned' AND date = '2026-05-27'", ())
        assert [r["slot"] for r in rows] == ["1", "2"]

    def test_replace_week_table_rejects_slot_reuse_over_completed_row(self, state):
        """W5 guard applies to replace_week_table too."""
        state.update_plan(PLAN_WITH_TWO_A_DAY, "seed")
        state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "workout",
                "start_local": "2026-05-27T18:00:00Z",
                "details": {"strava_id": 7777},
            }
        )
        new_rows = [
            {"day": "Wed", "date": "2026-05-27", "workout": "AM new", "pace_target": "Z2", "notes": ""},
            {"day": "Wed", "date": "2026-05-27", "workout": "PM new", "pace_target": "Z2", "notes": ""},
        ]
        with pytest.raises(ValueError, match="cannot reassign slot '2' on 2026-05-27"):
            state.replace_week_table(new_rows, "swap PM")


class TestSlotAwareStravaMatching:
    """reconcile_strava_activity routes Strava uploads to the right slot
    via the activity's start_local hour (W1: out-of-order arrivals; W2:
    same-type two-a-days; W9: bucket-boundary edge cases)."""

    def _seed_two_a_day(self, state, plan=PLAN_WITH_TWO_A_DAY):
        state.update_plan(plan, "seed")

    def test_am_activity_closes_am_slot(self, state):
        self._seed_two_a_day(state)
        result = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "easy",
                "start_local": "2026-05-27T06:45:00Z",
                "details": {"strava_id": 1001},
            }
        )
        assert result["matched"] is True
        row = state._rows("id = ?", (result["row_id"],))[0]
        assert row["slot"] == "1"

    def test_pm_activity_closes_pm_slot(self, state):
        self._seed_two_a_day(state)
        result = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "workout",
                "start_local": "2026-05-27T18:00:00Z",
                "details": {"strava_id": 2002},
            }
        )
        assert result["matched"] is True
        row = state._rows("id = ?", (result["row_id"],))[0]
        assert row["slot"] == "2"

    def test_out_of_order_pm_then_am_routes_correctly(self, state):
        """W1: PM webhook fires before AM webhook; each routes to its time bucket."""
        self._seed_two_a_day(state)
        # PM arrives first.
        pm = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "workout",
                "start_local": "2026-05-27T18:00:00Z",
                "details": {"strava_id": 9001},
            }
        )
        pm_row = state._rows("id = ?", (pm["row_id"],))[0]
        assert pm_row["slot"] == "2"
        # AM arrives later and lands in slot 1, not appended after PM.
        am = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "easy",
                "start_local": "2026-05-27T07:00:00Z",
                "details": {"strava_id": 9002},
            }
        )
        am_row = state._rows("id = ?", (am["row_id"],))[0]
        assert am_row["slot"] == "1"
        # Both slots completed, neither is off-plan.
        all_rows = state._rows("date = ?", ("2026-05-27",))
        statuses = sorted((r["slot"], r["status"]) for r in all_rows)
        assert statuses == [("1", "completed"), ("2", "completed")]

    def test_same_type_two_a_day_split_by_time(self, state):
        """W2: two easy runs (AM + PM) — slot proximity disambiguates."""
        # Two-a-day where BOTH planned slots are type=easy.
        plan = """\
# Plan

## This Week (2026-05-25 → 2026-05-31)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Wed | 2026-05-27 | Easy 4mi (AM) | Z2 | |
| Wed | 2026-05-27 | Easy 4mi (PM) | Z2 | |
"""
        state.update_plan(plan, "seed")
        morning = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "easy",
                "start_local": "2026-05-27T07:00:00Z",
                "details": {"strava_id": 1},
            }
        )
        evening = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "easy",
                "start_local": "2026-05-27T18:30:00Z",
                "details": {"strava_id": 2},
            }
        )
        m_row = state._rows("id = ?", (morning["row_id"],))[0]
        e_row = state._rows("id = ?", (evening["row_id"],))[0]
        assert m_row["slot"] == "1"
        assert e_row["slot"] == "2"

    def test_missing_start_local_falls_back_to_type_match(self, state):
        """Legacy entries without start_local still match by type."""
        self._seed_two_a_day(state)
        result = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "workout",
                "details": {"strava_id": 3},
            }
        )
        # PM slot has workout-typed prescription; type-only match picks slot 2.
        row = state._rows("id = ?", (result["row_id"],))[0]
        assert row["slot"] == "2"

    def test_single_session_day_loose_match_still_works(self, state):
        """Single-session days (slot=NULL) bypass the bucket logic."""
        state.update_plan(PLAN_WITH_WEEK, "seed")
        # 2026-04-28 has one planned row (slot=NULL); type "run" matches via single-row fallback.
        result = state.reconcile_strava_activity(
            {
                "date": "2026-04-28",
                "type": "run",
                "start_local": "2026-04-28T19:30:00Z",
                "details": {"strava_id": 11},
            }
        )
        assert result["matched"] is True

    def test_bucket_boundary_straddle_picks_nearest(self, state):
        """W9: activity at 11:30 on a 3-session day prefers slot 2 (center 12:30)
        over slot 1 (center 6:45)."""
        plan = """\
# Plan

## This Week (2026-05-25 → 2026-05-31)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Wed | 2026-05-27 | Easy 4mi (AM) | Z2 | |
| Wed | 2026-05-27 | Mobility (midday) | - | |
| Wed | 2026-05-27 | 6x400 (PM) | reps | |
"""
        state.update_plan(plan, "seed")
        # Activity at 11:30 — closer to slot 2 (12:30) than slot 1 (6:45).
        result = state.reconcile_strava_activity(
            {
                "date": "2026-05-27",
                "type": "easy",
                "start_local": "2026-05-27T11:30:00Z",
                "details": {"strava_id": 42},
            }
        )
        row = state._rows("id = ?", (result["row_id"],))[0]
        assert row["slot"] == "2"


# ---------------- sessions / log ----------------


class TestLog:
    def test_append_writes_session(self, state):
        state.append_session({"date": "2026-04-26", "type": "run", "miles": 5})
        with state._conn() as c:
            row = c.execute("SELECT data FROM sessions").fetchone()
        assert json.loads(row["data"])["miles"] == 5

    def test_append_requires_date(self, state):
        with pytest.raises(ValueError):
            state.append_session({"type": "run"})

    def test_recent_filters_by_window(self, state):
        today = date.today()
        state.append_session({"date": (today - timedelta(days=5)).isoformat(), "type": "a"})
        state.append_session({"date": (today - timedelta(days=30)).isoformat(), "type": "b"})
        recent = state.get_recent_sessions(days=14)
        types = {e["type"] for e in recent}
        assert "a" in types and "b" not in types

    def test_recent_with_explicit_today(self, state):
        anchor = date(2026, 4, 26)
        state.append_session({"date": "2026-04-25", "type": "a"})
        state.append_session({"date": "2026-04-01", "type": "b"})
        out = state.get_recent_sessions(days=14, today=anchor)
        types = {e["type"] for e in out}
        assert "a" in types and "b" not in types

    def test_range_inclusive(self, state):
        state.append_session({"date": "2026-04-26", "type": "x"})
        state.append_session({"date": "2026-04-27", "type": "y"})
        state.append_session({"date": "2026-04-28", "type": "z"})
        out = state.get_sessions_in_range(date(2026, 4, 27), date(2026, 4, 28))
        assert {e["type"] for e in out} == {"y", "z"}

    def test_extra_top_level_fields_round_trip(self, state):
        """Variable fields (notes, weather, pace_avg, …) must survive insert/select."""
        entry = {
            "date": "2026-05-11",
            "type": "easy",
            "miles": 5.0,
            "notes": "felt great",
            "weather": {"temp_f": 72, "wind_mph": 8},
            "pace_avg": "8:14",
        }
        state.append_session(entry)
        [out] = state.sessions_on_date(date(2026, 5, 11))
        assert out == entry

    def test_strava_id_unique_index_rejects_duplicate(self, state):
        entry = {"date": "2026-05-11", "type": "easy", "details": {"strava_id": 999}}
        state.append_session(entry)
        with pytest.raises(sqlite3.IntegrityError):
            state.append_session({**entry, "miles": 6.0})

    def test_sessions_without_strava_id_can_coexist(self, state):
        # The partial index only covers rows where strava_id is non-null.
        state.append_session({"date": "2026-05-11", "type": "strength"})
        state.append_session({"date": "2026-05-11", "type": "strength"})
        out = state.sessions_on_date(date(2026, 5, 11))
        assert len(out) == 2

    def test_update_session_by_strava_id(self, state):
        state.append_session({"date": "2026-05-11", "type": "easy", "details": {"strava_id": 12345}})
        ok = state.update_session_by_strava_id(
            12345,
            {"date": "2026-05-11", "type": "workout", "miles": 7, "details": {"strava_id": 12345}},
        )
        assert ok is True
        [out] = state.sessions_on_date(date(2026, 5, 11))
        assert out["type"] == "workout"
        assert out["miles"] == 7

    def test_update_session_returns_false_when_absent(self, state):
        assert state.update_session_by_strava_id(99999, {"date": "2026-05-11", "type": "easy"}) is False


# ---------------- journal ----------------


class TestJournal:
    def test_append_creates_with_header(self, state):
        state.append_journal("First entry")
        text = state.load_journal()
        assert "# Journal" in text
        assert "First entry" in text

    def test_append_no_duplicate_header(self, state):
        state.append_journal("first")
        state.append_journal("second")
        text = state.load_journal()
        assert text.count("# Journal") == 1
        assert "first" in text and "second" in text

    def test_load_max_entries_keeps_tail(self, state):
        for i in range(5):
            state.append_journal(f"entry {i}")
        out = state.load_journal(max_entries=2)
        assert "entry 4" in out and "entry 3" in out
        assert "entry 0" not in out


# ---------------- gcal_sync_state ----------------


class TestGcalSyncState:
    def test_round_trip_preserves_present_fields(self, state):
        original = {
            "pretrain20260511": {
                "hash": "abc",
                "last_synced_at": "2026-05-11T00:00:00Z",
                "completed": True,
                "last_completed_at": "2026-05-11T20:00:00Z",
            },
            "pretrain20260512": {"hash": "def", "last_synced_at": "2026-05-12T00:00:00Z"},
            "precomplete20260509": {
                "completed": True,
                "off_plan": True,
                "last_completed_at": "2026-05-09T18:00:00Z",
            },
        }
        state.save_gcal_sync_state(original)
        out = state.load_gcal_sync_state()
        assert out == original

    def test_save_replaces_wholesale(self, state):
        state.save_gcal_sync_state({"a": {"hash": "1"}})
        state.save_gcal_sync_state({"b": {"hash": "2"}})
        out = state.load_gcal_sync_state()
        assert set(out.keys()) == {"b"}


# ---------------- get_todays_workout ----------------


class TestGetTodaysWorkout:
    def test_finds_iso_date_row(self, state):
        state.update_plan(PLAN_WITH_WEEK, "seed")
        out = state.get_todays_workout(date(2026, 4, 28))
        assert out["found"] is True
        assert out["workout"] == "Easy 4mi + 4 strides"
        assert out["pace_target"] == "8:45-9:15"
        assert out["notes"] == "Strides 20s"
        assert out["is_rest_day"] is False
        assert out["day_name"] == "Tuesday"

    def test_rest_day_detected(self, state):
        state.update_plan(PLAN_WITH_WEEK, "seed")
        out = state.get_todays_workout(date(2026, 4, 27))
        assert out["found"] is True
        assert out["is_rest_day"] is True

    def test_missing_date_returns_not_found(self, state):
        state.update_plan(PLAN_WITH_WEEK, "seed")
        out = state.get_todays_workout(date(2030, 1, 1))
        assert out["found"] is False
        assert out["workout"] == ""

    def test_no_plan_set(self, state):
        out = state.get_todays_workout(date(2026, 4, 28))
        assert out["found"] is False


# ---------------- load_full_context ----------------


class TestLoadFullContext:
    def test_includes_all_sections(self, state):
        _seed_athlete(state)
        state.update_plan("# Plan\nfoo", "seed")
        state.append_session({"date": date.today().isoformat(), "type": "run", "miles": 5})
        state.append_journal("hello")

        blob = state.load_full_context()

        assert "ATHLETE PROFILE" in blob
        assert "TRAINING PLAN" in blob
        assert "RECENT SESSIONS" in blob
        assert "JOURNAL" in blob
        assert "TestRunner" in blob
        assert "hello" in blob

    def test_yaml_fence_present(self, state):
        _seed_athlete(state)
        blob = state.load_full_context()
        assert "```yaml" in blob
        # The athlete YAML text is included verbatim
        assert "target_races:" in blob


# ---------------- reviews ----------------


class TestReviews:
    def test_save_review_inserts_pending_row(self, state, monkeypatch):
        # Disconnect the mirror so save_review stays purely SQLite for this test.
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        # Seed a session so the reviews.session_id foreign key is satisfied.
        outcome = state.append_session({"date": "2026-05-20", "type": "easy", "miles": 5})
        row = state.save_review(
            session_id=outcome["row_id"],
            strava_id=123,
            review_date=date(2026, 5, 20),
            critique="Felt smooth.",
            proposed_change={"summary": "Bump tempo", "new_plan_md": "# v2", "reason": "fitness up"},
        )
        assert row["session_id"] == outcome["row_id"]
        assert row["strava_id"] == 123
        assert row["date"] == "2026-05-20"
        assert row["status"] is None  # Pending
        # proposed_change round-trips back to a dict (JSON parsed on the way out)
        assert row["proposed_change"]["summary"] == "Bump tempo"

    def test_get_reviews_in_range(self, state, monkeypatch):
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        state.save_review(None, None, date(2026, 5, 1), "a", None)
        state.save_review(None, None, date(2026, 5, 15), "b", None)
        state.save_review(None, None, date(2026, 6, 1), "c", None)
        got = state.get_reviews_in_range(date(2026, 5, 1), date(2026, 5, 31))
        assert [r["critique"] for r in got] == ["a", "b"]

    def test_save_review_fires_mirror(self, state, monkeypatch):
        captured: list = []
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: captured.append(entry))
        state.save_review(None, None, date(2026, 5, 20), "feedback", None)
        assert len(captured) == 1 and captured[0]["critique"] == "feedback"

    def test_find_pending_review_by_strava_id(self, state, monkeypatch):
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        state.save_review(None, 12345, date(2026, 5, 20), "felt fine", None)
        got = state.find_pending_review_for_activity(strava_id=12345)
        assert got is not None and got["strava_id"] == 12345 and got["status"] is None

    def test_find_pending_review_returns_none_when_resolved(self, state, monkeypatch):
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        row = state.save_review(None, 12345, date(2026, 5, 20), "felt fine", None)
        state.resolve_pending_review(row["id"], "approved")
        # Only Pending rows match
        assert state.find_pending_review_for_activity(strava_id=12345) is None

    def test_find_pending_review_by_session_id_fallback(self, state, monkeypatch):
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        outcome = state.append_session({"date": "2026-05-20", "type": "easy", "miles": 5})
        state.save_review(outcome["row_id"], None, date(2026, 5, 20), "x", None)
        got = state.find_pending_review_for_activity(session_id=outcome["row_id"])
        assert got is not None and got["session_id"] == outcome["row_id"]

    def test_find_pending_review_none_inputs_returns_none(self, state):
        assert state.find_pending_review_for_activity() is None

    def test_resolve_pending_review_updates_row(self, state, monkeypatch):
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        row = state.save_review(None, 9, date(2026, 5, 20), "x", None)
        updated = state.resolve_pending_review(row["id"], "approved")
        assert updated is not None
        assert updated["status"] == "approved"
        assert updated["resolved_at"] is not None

    def test_resolve_pending_review_fires_mirror(self, state, monkeypatch):
        captured: list = []
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: captured.append(entry))
        row = state.save_review(None, 9, date(2026, 5, 20), "x", None)
        captured.clear()  # ignore the save-mirror call
        state.resolve_pending_review(row["id"], "approved")
        assert len(captured) == 1 and captured[0]["status"] == "approved"

    def test_resolve_pending_review_idempotent(self, state, monkeypatch):
        """Second call returns None (no rows updated) — already resolved."""
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        row = state.save_review(None, 9, date(2026, 5, 20), "x", None)
        first = state.resolve_pending_review(row["id"], "approved")
        second = state.resolve_pending_review(row["id"], "rejected")
        assert first is not None and second is None
        # Row stays approved
        rows = state.get_all_reviews()
        assert rows[0]["status"] == "approved"

    def test_resolve_pending_review_missing_id(self, state):
        assert state.resolve_pending_review(999, "approved") is None

    def test_expire_old_pending_reviews_flips_stale_only(self, state, monkeypatch):
        """Only Pending rows older than the cutoff get expired."""
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        # Stale pending — 30 days old
        old_row = state.save_review(None, 1, date(2026, 4, 1), "old", None)
        # Recent pending — within 14 days
        recent_row = state.save_review(None, 2, date(2026, 5, 15), "recent", None)
        # Stale but already resolved — must not be re-flipped
        resolved = state.save_review(None, 3, date(2026, 4, 1), "approved-old", None)
        state.resolve_pending_review(resolved["id"], "approved")

        expired = state.expire_old_pending_reviews(days=14, today=date(2026, 5, 20))
        expired_ids = {r["id"] for r in expired}
        assert old_row["id"] in expired_ids
        assert recent_row["id"] not in expired_ids
        assert resolved["id"] not in expired_ids

        # Verify in SQLite directly
        all_rows = {r["id"]: r for r in state.get_all_reviews()}
        assert all_rows[old_row["id"]]["status"] == "expired"
        assert all_rows[old_row["id"]]["resolved_at"] is not None
        assert all_rows[recent_row["id"]]["status"] is None
        assert all_rows[resolved["id"]]["status"] == "approved"

    def test_expire_old_pending_reviews_returns_empty_when_none(self, state, monkeypatch):
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        state.save_review(None, 1, date(2026, 5, 15), "recent", None)
        assert state.expire_old_pending_reviews(days=14, today=date(2026, 5, 20)) == []

    def test_expire_old_pending_reviews_fires_one_batched_mirror_call(self, state, monkeypatch):
        """Sweep fires ONE batched mirror call carrying every stale row —
        not one daemon thread per row. Regression guard for the original
        per-row ``_notify_mirror_review`` loop."""
        per_row_calls: list = []
        batch_calls: list = []
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: per_row_calls.append(entry))
        monkeypatch.setattr(state, "_notify_mirror_reviews", lambda entries: batch_calls.append(list(entries)))
        state.save_review(None, 1, date(2026, 4, 1), "a", None)
        state.save_review(None, 2, date(2026, 4, 2), "b", None)
        state.save_review(None, 3, date(2026, 4, 3), "c", None)
        per_row_calls.clear()  # ignore the save-mirror calls
        batch_calls.clear()

        state.expire_old_pending_reviews(days=14, today=date(2026, 5, 20))

        # One batched call, not one per row.
        assert len(batch_calls) == 1
        assert len(batch_calls[0]) == 3
        assert all(r["status"] == "expired" for r in batch_calls[0])
        # No per-row mirror calls came from the sweep.
        assert per_row_calls == []

    def test_expire_old_pending_reviews_uses_utc_today(self, state, monkeypatch):
        """Default ``today`` comes from UTC, not local time. Fix for the
        local-vs-UTC drift the docstring promised but the code violated."""
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        import state_manager as sm

        monkeypatch.setattr(state, "_notify_mirror_reviews", lambda entries: None)

        # Row dated 30 days ago vs the FAKE UTC "now" → must expire.
        state.save_review(None, 9, date(2026, 4, 1), "old", None)

        class _FakeDateTime:
            @staticmethod
            def now(tz=None):
                assert tz is _tz.utc, "expire sweep must request UTC"
                return _dt(2026, 5, 20, 9, 0, 0, tzinfo=_tz.utc)

        monkeypatch.setattr(sm, "datetime", _FakeDateTime)

        expired = state.expire_old_pending_reviews(days=14)
        assert len(expired) == 1
        assert expired[0]["status"] == "expired"


# ---------------- change-body formatters ----------------


class TestChangeFormatters:
    def test_format_session_row_short_includes_all_present_fields(self):
        from state_manager import _format_session_row_short

        row = {
            "date": "2026-05-19",
            "status": "planned",
            "type": "easy",
            "prescribed_workout": "Easy 5mi",
            "prescribed_pace": "8:30",
            "prescribed_notes": "ankle prep",
        }
        line = _format_session_row_short(row)
        assert "2026-05-19" in line and "[planned/easy]" in line and "Easy 5mi" in line
        assert "pace=8:30" in line and "notes=ankle prep" in line

    def test_format_session_row_short_handles_none(self):
        from state_manager import _format_session_row_short

        assert _format_session_row_short(None) == "(none)"

    def test_format_actuals_parses_json_string(self):
        from state_manager import _format_actuals

        out = _format_actuals(json.dumps({"miles": 5.1, "details": {"strava_id": 42}}))
        assert "miles: 5.1" in out and "strava_id: 42" in out

    def test_format_actuals_empty(self):
        from state_manager import _format_actuals

        assert _format_actuals(None) == "(no actuals)"


# ---------------- change-body plumbing (writers attach a body) ----------------


def _capture_changes(state, monkeypatch):
    """Replace _notify_mirror_change with a list-capturer. Returns the list."""
    captured: list = []
    monkeypatch.setattr(state, "_notify_mirror_change", lambda entry: captured.append(entry))
    return captured


class TestWriterChangeBody:
    def test_update_workout_records_before_and_after(self, state, monkeypatch):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        captured = _capture_changes(state, monkeypatch)
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="bump strides",
            workout="Easy 4mi + 6 strides",
        )
        [entry] = captured
        body = entry["body"]
        assert "## Before" in body and "## After" in body
        assert "Easy 4mi + 4 strides" in body  # before
        assert "Easy 4mi + 6 strides" in body  # after

    def test_replace_week_table_records_before_and_after(self, state, monkeypatch):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        captured = _capture_changes(state, monkeypatch)
        state.replace_week_table(
            [
                {"day": "Mon", "date": "2026-04-27", "workout": "Rest", "pace_target": "—", "notes": ""},
                {"day": "Tue", "date": "2026-04-28", "workout": "Easy 5mi", "pace_target": "8:45", "notes": ""},
            ],
            "rewrite",
        )
        body = captured[-1]["body"]
        assert "Off / strength upper" in body  # was on 2026-04-27
        assert "Easy 5mi" in body  # new 2026-04-28

    def test_update_plan_meta_diffs_prose(self, state, monkeypatch):
        state.update_plan_meta("# v1\n", "seed")
        captured = _capture_changes(state, monkeypatch)
        state.update_plan_meta("# v2\n", "rewrite goals")
        body = captured[-1]["body"]
        assert "Before (plan_meta)" in body and "After (plan_meta)" in body
        assert "# v1" in body and "# v2" in body

    def test_reconcile_completed_uses_prescribed_vs_actuals(self, state, monkeypatch):
        state.update_plan(PLAN_WITH_DETAIL, "seed")  # plants a planned row for 2026-04-28
        captured = _capture_changes(state, monkeypatch)
        state.reconcile_strava_activity(
            {"date": "2026-04-28", "type": "easy", "miles": 4.1, "details": {"strava_id": 99}}
        )
        body = captured[-1]["body"]
        assert "## Prescribed" in body and "## Actuals" in body
        assert "Easy 4mi" in body  # prescribed
        assert "miles: 4.1" in body and "strava_id: 99" in body  # actuals
