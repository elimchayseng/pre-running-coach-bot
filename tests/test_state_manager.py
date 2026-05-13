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
