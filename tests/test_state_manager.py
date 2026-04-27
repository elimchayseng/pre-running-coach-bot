import json
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
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def state(state_dir: Path) -> StateManager:
    return StateManager(state_dir)


def _seed_athlete(state_dir: Path, content: str = ATHLETE_YAML) -> None:
    (state_dir / "athlete.yaml").write_text(content)


# ---------------- athlete ----------------

class TestAthlete:
    def test_load_returns_parsed_dict(self, state, state_dir):
        _seed_athlete(state_dir)
        data = state.load_athlete()
        assert data["name"] == "TestRunner"
        assert data["target_races"][0]["name"] == "Test Half"

    def test_load_missing_returns_empty(self, state):
        assert state.load_athlete() == {}

    def test_update_patches_top_level(self, state, state_dir):
        _seed_athlete(state_dir)
        state.update_athlete({"name": "Renamed"})
        assert state.load_athlete()["name"] == "Renamed"

    def test_update_preserves_comments(self, state, state_dir):
        _seed_athlete(state_dir)
        state.update_athlete({"name": "Renamed"})
        text = (state_dir / "athlete.yaml").read_text()
        assert "inline comment about target_races" in text
        assert "# current" in text

    def test_update_deep_merges_nested(self, state, state_dir):
        _seed_athlete(state_dir)
        state.update_athlete({"zones": {"easy": "8:30-9:00"}})
        data = state.load_athlete()
        assert data["zones"]["threshold"] == "6:15"  # untouched
        assert data["zones"]["easy"] == "8:30-9:00"

    def test_update_missing_file_raises(self, state):
        with pytest.raises(FileNotFoundError):
            state.update_athlete({"name": "X"})


# ---------------- plan + changelog ----------------

class TestPlan:
    def test_load_returns_text(self, state, state_dir):
        (state_dir / "plan.md").write_text("# My Plan\nfoo")
        assert "My Plan" in state.load_plan()

    def test_load_missing_returns_empty(self, state):
        assert state.load_plan() == ""

    def test_update_writes_and_appends_changelog(self, state, state_dir):
        state.update_plan("# new", "Brooklyn taper week")
        assert state.load_plan() == "# new"
        log = (state_dir / "plan_changelog.md").read_text()
        assert "Brooklyn taper week" in log

    def test_update_changelog_accumulates(self, state):
        state.update_plan("# v1", "first edit")
        state.update_plan("# v2", "second edit")
        log = state.changelog_path.read_text()
        assert "first edit" in log and "second edit" in log
        assert state.load_plan() == "# v2"


# ---------------- log.jsonl ----------------

class TestLog:
    def test_append_writes_json_line(self, state, state_dir):
        state.append_session({"date": "2026-04-26", "type": "run", "miles": 5})
        line = (state_dir / "log.jsonl").read_text().strip()
        assert json.loads(line)["miles"] == 5

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

    def test_malformed_line_skipped(self, state, state_dir):
        (state_dir / "log.jsonl").write_text(
            'not json\n{"date": "2026-04-26", "type": "ok"}\n'
        )
        out = state.get_sessions_in_range(date(2026, 4, 26), date(2026, 4, 26))
        assert len(out) == 1


# ---------------- journal ----------------

class TestJournal:
    def test_append_creates_with_header(self, state, state_dir):
        state.append_journal("First entry")
        text = (state_dir / "journal.md").read_text()
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


# ---------------- get_todays_workout ----------------

class TestGetTodaysWorkout:
    def test_finds_iso_date_row(self, state, state_dir):
        (state_dir / "plan.md").write_text(PLAN_WITH_WEEK)
        out = state.get_todays_workout(date(2026, 4, 28))
        assert out["found"] is True
        assert out["workout"] == "Easy 4mi + 4 strides"
        assert out["pace_target"] == "8:45-9:15"
        assert out["notes"] == "Strides 20s"
        assert out["is_rest_day"] is False
        assert out["day_name"] == "Tue"  # value from the table's first column

    def test_rest_day_detected(self, state, state_dir):
        (state_dir / "plan.md").write_text(PLAN_WITH_WEEK)
        out = state.get_todays_workout(date(2026, 4, 27))
        assert out["found"] is True
        assert out["is_rest_day"] is True

    def test_missing_date_returns_not_found(self, state, state_dir):
        (state_dir / "plan.md").write_text(PLAN_WITH_WEEK)
        out = state.get_todays_workout(date(2030, 1, 1))
        assert out["found"] is False
        assert out["workout"] == ""

    def test_no_plan_file(self, state):
        out = state.get_todays_workout(date(2026, 4, 28))
        assert out["found"] is False


# ---------------- load_full_context ----------------

class TestLoadFullContext:
    def test_includes_all_sections(self, state, state_dir):
        _seed_athlete(state_dir)
        (state_dir / "plan.md").write_text("# Plan\nfoo")
        state.append_session({"date": date.today().isoformat(), "type": "run", "miles": 5})
        state.append_journal("hello")

        blob = state.load_full_context()

        assert "ATHLETE PROFILE" in blob
        assert "TRAINING PLAN" in blob
        assert "RECENT SESSIONS" in blob
        assert "JOURNAL" in blob
        assert "TestRunner" in blob
        assert "hello" in blob
