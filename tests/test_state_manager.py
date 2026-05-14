"""State manager tests against a fresh SQLite DB per test."""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from state_manager import StateManager

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


# ---------------- plan + changelog ----------------


class TestPlan:
    def test_load_returns_text(self, state):
        state.update_plan("# My Plan\nfoo", "seed")
        assert "My Plan" in state.load_plan()

    def test_load_missing_returns_empty(self, state):
        assert state.load_plan() == ""

    def test_update_writes_and_appends_changelog(self, state):
        state.update_plan("# new", "Brooklyn taper week")
        assert state.load_plan() == "# new"
        with state._conn() as c:
            row = c.execute("SELECT content FROM plan_changelog WHERE id=1").fetchone()
        assert "Brooklyn taper week" in row["content"]

    def test_update_changelog_accumulates(self, state):
        state.update_plan("# v1", "first edit")
        state.update_plan("# v2", "second edit")
        with state._conn() as c:
            row = c.execute("SELECT content FROM plan_changelog WHERE id=1").fetchone()
        log = row["content"]
        assert "first edit" in log and "second edit" in log
        assert state.load_plan() == "# v2"


# ---------------- surgical plan edits (PR B / issue #19) ----------------


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
WU: 1mi easy
Work: 4 x 20s strides
CD: 1mi easy
"""


class TestUpdateWorkout:
    """Single-row patches via state.update_workout — surgical edits that
    avoid round-tripping the entire plan through the LLM."""

    def test_patch_single_cell_preserves_others(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="bump strides count",
            workout="Easy 4mi + 6 strides",
        )
        plan = state.load_plan()
        # The new cell landed.
        assert "Easy 4mi + 6 strides" in plan
        # Pace and notes for that row stayed.
        assert "8:45-9:15" in plan
        assert "Strides 20s" in plan
        # Other rows untouched.
        assert "Off / strength upper" in plan
        assert "Easy 6mi rolling" in plan

    def test_patch_multiple_fields_in_one_call(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="bump everything",
            workout="Tempo 6mi",
            pace_target="6:30-6:40",
            notes="focus on relaxed shoulders",
        )
        plan = state.load_plan()
        assert "Tempo 6mi" in plan
        assert "6:30-6:40" in plan
        assert "focus on relaxed shoulders" in plan
        # Original Tue cells should be gone.
        assert "Easy 4mi + 4 strides" not in plan
        assert "Strides 20s" not in plan

    def test_no_matching_row_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        with pytest.raises(ValueError, match="no row found"):
            state.update_workout(
                target_date=date(2030, 1, 1),
                change_note="nope",
                workout="Anything",
            )

    def test_no_fields_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        with pytest.raises(ValueError, match="must pass at least one"):
            state.update_workout(target_date=date(2026, 4, 28), change_note="x")

    def test_empty_plan_raises(self, state):
        with pytest.raises(ValueError, match="plan is empty"):
            state.update_workout(
                target_date=date(2026, 4, 28),
                change_note="x",
                workout="anything",
            )

    def test_detail_body_only_no_row_edit(self, state):
        """detail_body should work without any row-cell change. Useful when
        the LLM only wants to enrich the per-day prose."""
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="enrich detail",
            detail_body="Updated prose for Tuesday.\nFocus on cadence.",
        )
        plan = state.load_plan()
        # New body landed.
        assert "Updated prose for Tuesday." in plan
        assert "Focus on cadence." in plan
        # Old body gone.
        assert "Original detail prose for Tuesday strides session." not in plan
        # Locked table row unchanged.
        assert "Easy 4mi + 4 strides" in plan

    def test_detail_body_creates_section_when_missing(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 5, 2),
            change_note="add Saturday detail",
            detail_body="Long-run cues for Saturday.",
        )
        plan = state.load_plan()
        assert "#### 2026-05-02" in plan
        assert "Long-run cues for Saturday." in plan
        # Existing Tuesday detail untouched.
        assert "Original detail prose for Tuesday strides session." in plan

    def test_combined_row_and_detail_in_one_transaction(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="combined",
            workout="Workout 5x800",
            pace_target="6:00",
            detail_body="WU 1mi easy. Work 5x800 @ 6:00 / 400 jog. CD 1mi.",
        )
        plan = state.load_plan()
        assert "Workout 5x800" in plan
        assert "WU 1mi easy. Work 5x800 @ 6:00 / 400 jog. CD 1mi." in plan
        assert "Original detail prose for Tuesday strides session." not in plan
        # Single combined edit produces a single changelog entry.
        with state._conn() as c:
            row = c.execute("SELECT content FROM plan_changelog WHERE id=1").fetchone()
        assert row["content"].count("combined") == 1

    def test_detail_body_replace_does_not_disturb_other_sections(self, state):
        """The replacement should stop at the next heading. Trailing content
        below an unrelated heading must be preserved."""
        plan_with_trailing = (
            PLAN_WITH_DETAIL
            + "\n## Notes\n\nAccessory mobility 10min daily.\n"
            + "\n#### 2026-05-02\n\nLong-run plan A.\n"
        )
        state.update_plan(plan_with_trailing, "seed")
        state.update_workout(
            target_date=date(2026, 4, 28),
            change_note="replace tue detail only",
            detail_body="NEW Tuesday body.",
        )
        plan = state.load_plan()
        assert "NEW Tuesday body." in plan
        # The unrelated trailing sections must survive untouched.
        assert "Accessory mobility 10min daily." in plan
        assert "Long-run plan A." in plan


class TestReplaceWeekTable:
    """Bulk weekly-table replacement for block / phase transitions."""

    def test_replaces_all_rows(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        new_rows = [
            {"day": "Mon", "date": "2026-05-04", "workout": "Off", "pace_target": "—", "notes": ""},
            {"day": "Tue", "date": "2026-05-05", "workout": "Easy 5mi", "pace_target": "8:45-9:15", "notes": ""},
            {"day": "Wed", "date": "2026-05-06", "workout": "Tempo 6mi", "pace_target": "6:30", "notes": "track"},
        ]
        state.replace_week_table(new_rows, "next week")
        plan = state.load_plan()
        # Old data rows gone.
        assert "Easy 4mi + 4 strides" not in plan
        assert "Easy 6mi rolling" not in plan
        # New data rows present.
        assert "2026-05-05" in plan
        assert "Tempo 6mi" in plan
        # Header + separator preserved.
        assert "| Day | Date | Workout | Pace target | Notes |" in plan
        assert "|-----|------|---------|-------------|-------|" in plan

    def test_preserves_per_day_detail_sections(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        state.replace_week_table(
            [{"day": "Mon", "date": "2026-05-04", "workout": "Off", "pace_target": "—", "notes": ""}],
            "trim",
        )
        plan = state.load_plan()
        # The Tuesday detail section is outside the table — it must survive.
        assert "#### 2026-04-28" in plan
        assert "Original detail prose for Tuesday strides session." in plan

    def test_missing_key_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        bad = [{"day": "Mon", "date": "2026-05-04", "workout": "Off"}]  # missing pace_target, notes
        with pytest.raises(ValueError, match="missing required keys"):
            state.replace_week_table(bad, "x")

    def test_no_locked_table_raises(self, state):
        state.update_plan("# Plan\n\nNo table here.", "seed")
        new_rows = [{"day": "Mon", "date": "2026-05-04", "workout": "Off", "pace_target": "—", "notes": ""}]
        with pytest.raises(ValueError, match="no locked-format table"):
            state.replace_week_table(new_rows, "x")

    def test_empty_rows_raises(self, state):
        state.update_plan(PLAN_WITH_DETAIL, "seed")
        with pytest.raises(ValueError, match="rows must be non-empty"):
            state.replace_week_table([], "x")


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
        assert out["day_name"] == "Tue"

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
