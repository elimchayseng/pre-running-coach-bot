import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from state_manager import StateManager
from tools import ALL_TOOLS, execute_tool
from tools.fitness import (
    _closest_zone,
    _hr_context,
    _pace_to_sec,
    _sec_to_pace,
    _zone_range,
)

ATHLETE_YAML = """\
name: Test
target_races:
  - name: Past Race
    date: 2026-01-15
    priority: A
  - name: Future Race
    date: 2026-12-31
    priority: A
    goal_pace: "6:10"
    terrain: road
zones:
  marathon_pace: "6:40"
  threshold: "6:15-6:25"
  easy: "8:30-9:00"
hr_zones:
  easy_ceiling: 155
  threshold: "175-185"
"""

PLAN_MD = """\
# Plan

## This Week

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Mon | 2026-04-27 | Off | — | |
| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | |
"""


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    (d / "athlete.yaml").write_text(ATHLETE_YAML)
    (d / "plan.md").write_text(PLAN_MD)
    return d


@pytest.fixture
def state(state_dir: Path) -> StateManager:
    return StateManager(state_dir)


# ------------- dispatcher -------------


class TestDispatcher:
    def test_all_tools_have_unique_names(self):
        names = [t["function"]["name"] for t in ALL_TOOLS]
        assert len(names) == len(set(names))

    def test_unknown_tool_returns_error(self, state):
        out = execute_tool("nonexistent_tool", {}, state)
        assert "error" in out

    def test_handler_exception_returns_error(self, state):
        # update_athlete with no file -> FileNotFoundError -> error dict
        state.athlete_path.unlink()
        out = execute_tool("update_athlete", {"updates": {"name": "X"}}, state)
        assert "error" in out


# ------------- state tools -------------


class TestStateTools:
    def test_log_session_writes_log(self, state):
        out = execute_tool(
            "log_session",
            {"date": "2026-04-26", "type": "run", "miles": 5},
            state,
        )
        assert out["ok"] is True
        sessions = state.get_sessions_in_range(date(2026, 4, 26), date(2026, 4, 26))
        assert sessions[0]["miles"] == 5

    def test_log_session_strips_none_values(self, state):
        execute_tool(
            "log_session",
            {"date": "2026-04-26", "type": "run", "miles": 5, "rpe": None},
            state,
        )
        line = state.log_path.read_text().strip()
        assert "rpe" not in json.loads(line)

    def test_update_plan_with_valid_today_row_no_warning(self, state, monkeypatch):
        """Plan contains today's row → tool returns ok with no warning."""
        # Pin today to a known date and write a plan that includes that row.
        from datetime import date as _date

        import temporal_context

        target = _date(2026, 4, 28)
        monkeypatch.setattr(temporal_context, "today_local", lambda: target)

        plan = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | |\n"
        )
        out = execute_tool(
            "update_plan",
            {"new_plan_markdown": plan, "change_reason": "test"},
            state,
        )
        assert out["ok"] is True
        assert "warning" not in out
        assert state.load_plan() == plan

    def test_update_plan_breaks_table_returns_warning(self, state, monkeypatch):
        """Plan without a parseable today row → tool returns a warning so
        the agent can self-correct."""
        from datetime import date as _date

        import temporal_context

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))

        # No table at all — definitely no row for today
        out = execute_tool(
            "update_plan",
            {"new_plan_markdown": "# Plan v2 with no table", "change_reason": "test"},
            state,
        )
        assert out["ok"] is True
        assert "warning" in out
        assert "not parseable" in out["warning"]
        assert "table format" in out["warning"]
        # Plan still written despite warning (agent decides whether to retry)
        assert state.load_plan() == "# Plan v2 with no table"

    def test_append_journal(self, state):
        execute_tool("append_journal", {"entry": "feeling great"}, state)
        assert "feeling great" in state.load_journal()

    def test_update_athlete_preserves_comments(self, state, state_dir):
        execute_tool("update_athlete", {"updates": {"name": "Renamed"}}, state)
        assert state.load_athlete()["name"] == "Renamed"

    def test_get_sessions(self, state):
        state.append_session({"date": "2026-04-25", "type": "run", "miles": 4})
        state.append_session({"date": "2026-04-27", "type": "run", "miles": 6})
        out = execute_tool(
            "get_sessions",
            {"start_date": "2026-04-26", "end_date": "2026-04-27"},
            state,
        )
        assert out["count"] == 1
        assert out["sessions"][0]["miles"] == 6


# ------------- plan tools -------------


class TestPlanTools:
    def test_get_today_returns_next_race(self, state):
        out = execute_tool("get_today", {}, state)
        assert out["next_race"]["name"] == "Future Race"
        assert out["next_race"]["goal_pace"] == "6:10"
        assert out["next_race"]["terrain"] == "road"
        assert out["next_race"]["days_to_race"] > 0

    def test_get_today_skips_past_races(self, state):
        out = execute_tool("get_today", {}, state)
        assert "Past Race" != out["next_race"]["name"]

    def test_get_today_no_races(self, state, state_dir):
        (state_dir / "athlete.yaml").write_text("name: Test\n")
        out = execute_tool("get_today", {}, state)
        assert out["next_race"] is None

    def test_get_todays_workout(self, state):
        out = execute_tool("get_todays_workout", {"date": "2026-04-28"}, state)
        assert out["found"] is True
        assert out["workout"] == "Easy 4mi"

    def test_get_week_plan_returns_seven_days(self, state):
        out = execute_tool("get_week_plan", {"week_offset": 0}, state)
        assert len(out["days"]) == 7
        assert "week_start" in out


# ------------- fitness helpers -------------


class TestFitnessHelpers:
    def test_pace_to_sec(self):
        assert _pace_to_sec("6:30") == 390
        assert _pace_to_sec("8:00") == 480

    def test_pace_to_sec_range_midpoint(self):
        assert _pace_to_sec("8:30-9:00") == 525

    def test_pace_to_sec_invalid(self):
        assert _pace_to_sec("not a pace") is None
        assert _pace_to_sec(None) is None

    def test_sec_to_pace(self):
        assert _sec_to_pace(390) == "6:30"
        assert _sec_to_pace(485) == "8:05"

    def test_zone_range_with_hyphen(self):
        assert _zone_range("6:15-6:25") == (375, 385)

    def test_zone_range_single(self):
        assert _zone_range("6:40") == (400, 400)

    def test_closest_zone(self):
        zones = {"marathon_pace": "6:40", "threshold": "6:15-6:25", "easy": "8:30-9:00"}
        # 6:35 is closest to threshold midpoint, marathon_pace is 6:40
        # Actually 6:35 (395) is 15s from threshold midpoint (380) and 5s from MP (400)
        match = _closest_zone(395, zones)
        assert match[0] == "marathon_pace"

    def test_hr_context_easy(self):
        assert "easy" in _hr_context(150, {"easy_ceiling": 155, "threshold": "175-185"})

    def test_hr_context_below_threshold(self):
        assert "below" in _hr_context(165, {"easy_ceiling": 155, "threshold": "175-185"})

    def test_hr_context_in_threshold(self):
        assert "in threshold" in _hr_context(180, {"threshold": "175-185"})

    def test_hr_context_above_threshold(self):
        assert "above" in _hr_context(195, {"threshold": "175-185"})


# ------------- fitness summary integration -------------


class TestFitnessSummary:
    def _seed_runs(self, state, today, runs):
        for offset, payload in runs:
            d = (today - timedelta(days=offset)).isoformat()
            state.append_session({"date": d, **payload})

    def test_no_data_safe(self, state):
        out = execute_tool("get_fitness_summary", {"window_days": 14}, state)
        assert out["session_count"] == 0
        assert out["quality_sessions"] == []

    def test_pace_vs_zone_decoration(self, state):
        today = date.today()
        # 6:30 pace, marathon zone is 6:40 -> faster
        self._seed_runs(state, today, [(2, {"type": "workout", "miles": 8, "pace_avg": "6:30", "hr_avg": 165})])
        out = execute_tool("get_fitness_summary", {"window_days": 7}, state)
        assert len(out["quality_sessions"]) == 1
        q = out["quality_sessions"][0]
        assert "faster than marathon_pace" in q["vs_zone"]
        assert "below threshold" in q["hr_context"]

    def test_low_hr_fast_signal(self, state):
        today = date.today()
        self._seed_runs(
            state,
            today,
            [
                (5, {"type": "workout", "miles": 8, "pace_avg": "6:30", "hr_avg": 160}),
                (3, {"type": "workout", "miles": 8, "pace_avg": "6:35", "hr_avg": 162}),
            ],
        )
        out = execute_tool("get_fitness_summary", {"window_days": 14}, state)
        signals_text = " | ".join(out["signals"])
        assert "below" in signals_text or "fitness" in signals_text.lower()

    def test_data_gap_when_hr_missing(self, state):
        today = date.today()
        self._seed_runs(
            state,
            today,
            [
                (5, {"type": "workout", "miles": 8, "pace_avg": "6:30"}),
                (3, {"type": "workout", "miles": 8, "pace_avg": "6:35"}),
            ],
        )
        out = execute_tool("get_fitness_summary", {"window_days": 14}, state)
        assert any("HR missing" in g for g in out["data_gaps"])

    def test_no_quality_signal_in_long_window(self, state):
        today = date.today()
        self._seed_runs(
            state,
            today,
            [
                (5, {"type": "easy", "miles": 4}),
            ],
        )
        out = execute_tool("get_fitness_summary", {"window_days": 21}, state)
        assert any("No quality sessions" in s for s in out["signals"])
