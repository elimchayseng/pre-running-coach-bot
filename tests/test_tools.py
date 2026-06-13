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
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def state(state_dir: Path) -> StateManager:
    """SQLite-backed StateManager seeded with the ATHLETE_YAML + PLAN_MD fixtures."""
    s = StateManager(state_dir)
    with s._conn() as c:
        c.execute("INSERT INTO athlete (id, yaml_text) VALUES (1, ?)", (ATHLETE_YAML,))
    s.update_plan(PLAN_MD, "seed")
    return s


def _seed_athlete(state: StateManager, yaml_text: str) -> None:
    """Replace the athlete row entirely (mirrors the migration script)."""
    with state._conn() as c:
        c.execute(
            "INSERT INTO athlete (id, yaml_text) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET yaml_text = excluded.yaml_text",
            (yaml_text,),
        )


def _seed_log(state: StateManager, entries) -> None:
    """Append session rows directly so we can test analysis paths."""
    for e in entries:
        state.append_session(e)


# ------------- dispatcher -------------


class TestDispatcher:
    def test_all_tools_have_unique_names(self):
        names = [t["function"]["name"] for t in ALL_TOOLS]
        assert len(names) == len(set(names))

    def test_unknown_tool_returns_error(self, state):
        out = execute_tool("nonexistent_tool", {}, state)
        assert "error" in out

    def test_handler_exception_returns_error(self, state):
        # update_athlete with no row -> FileNotFoundError -> error dict
        with state._conn() as c:
            c.execute("DELETE FROM athlete")
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
        [entry] = state.sessions_on_date(date(2026, 4, 26))
        assert "rpe" not in entry

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
        assert state.get_todays_workout(target)["workout"] == "Easy 4mi"

    def test_update_plan_clears_pending_proposal(self, state, monkeypatch, fake_redis):
        """A successful plan write consumes any pending post-activity proposal,
        so the next chat turn's system prompt doesn't keep showing it."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import (
            get_pending_plan_proposal,
            set_pending_plan_proposal,
        )

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        set_pending_plan_proposal({"summary": "x", "new_plan_md": "...", "reason": "..."})
        assert get_pending_plan_proposal() is not None

        plan = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | |\n"
        )
        execute_tool(
            "update_plan",
            {"new_plan_markdown": plan, "change_reason": "apply pending"},
            state,
        )
        assert get_pending_plan_proposal() is None

    def test_update_plan_auto_resolves_matching_pending_review(self, state, monkeypatch, fake_redis):
        """update_plan + a pending proposal whose proposed_for_activity points
        at a recent Pending review (matched by strava_id) flips that review
        to ``approved`` and fires the Notion review mirror once."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import set_pending_plan_proposal

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        # Capture Notion review-mirror calls so we can assert the flip is mirrored.
        captured: list = []
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: captured.append(entry))

        # Pre-seed: a pending review for Strava activity 777
        review = state.save_review(
            session_id=None,
            strava_id=777,
            review_date=_date(2026, 4, 28),
            critique="Hard easy. HR drifted.",
            proposed_change={"summary": "demote tempo", "new_plan_md": "x", "reason": "y"},
        )
        assert review["status"] is None
        captured.clear()  # ignore the save-mirror call

        set_pending_plan_proposal(
            {
                "summary": "demote tempo",
                "new_plan_md": "x",
                "reason": "y",
                "proposed_for_activity": 777,
            }
        )

        plan = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | |\n"
        )
        execute_tool(
            "update_plan",
            {"new_plan_markdown": plan, "change_reason": "apply pending"},
            state,
        )

        rows = state.get_all_reviews()
        assert rows[0]["status"] == "approved"
        assert rows[0]["resolved_at"] is not None
        # Mirror was called for the flip (status=approved in the entry)
        assert any(c["status"] == "approved" for c in captured)

    def test_update_plan_no_matching_proposal_leaves_review_pending(self, state, monkeypatch, fake_redis):
        """No pending proposal in Redis → reviews stay Pending."""
        from datetime import date as _date

        import temporal_context

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        state.save_review(
            session_id=None,
            strava_id=888,
            review_date=_date(2026, 4, 28),
            critique="ok",
            proposed_change=None,
        )

        plan = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | |\n"
        )
        execute_tool(
            "update_plan",
            {"new_plan_markdown": plan, "change_reason": "manual edit"},
            state,
        )

        rows = state.get_all_reviews()
        assert rows[0]["status"] is None  # still Pending

    def test_update_plan_proposal_for_unrelated_activity_leaves_review_pending(self, state, monkeypatch, fake_redis):
        """A pending proposal targeting strava_id=A must not flip a review
        for strava_id=B."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import set_pending_plan_proposal

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        # Review is for activity 111
        state.save_review(None, 111, _date(2026, 4, 28), "ok", None)
        # Pending proposal is for an entirely different activity
        set_pending_plan_proposal(
            {
                "summary": "s",
                "new_plan_md": "x",
                "reason": "r",
                "proposed_for_activity": 222,
            }
        )

        plan = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | |\n"
        )
        execute_tool(
            "update_plan",
            {"new_plan_markdown": plan, "change_reason": "x"},
            state,
        )
        rows = state.get_all_reviews()
        assert rows[0]["status"] is None

    def test_update_workout_auto_resolves_matching_pending_review(self, state, monkeypatch, fake_redis):
        """The patch-style edit tool also resolves a matching pending review."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import set_pending_plan_proposal

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        review = state.save_review(None, 333, _date(2026, 4, 28), "x", None)
        set_pending_plan_proposal({"summary": "s", "new_plan_md": "x", "reason": "r", "proposed_for_activity": 333})

        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "user accepted"},
            state,
        )

        rows = state.get_all_reviews()
        assert rows[0]["id"] == review["id"]
        assert rows[0]["status"] == "approved"

    def test_auto_resolve_clears_redis_proposal(self, state, monkeypatch, fake_redis):
        """When the auto-resolve flip actually happens, the Redis proposal
        that triggered it must be cleared too — otherwise the next system
        prompt resurfaces a proposal the user already applied."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import (
            get_pending_plan_proposal,
            set_pending_plan_proposal,
        )

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        state.save_review(None, 444, _date(2026, 4, 28), "x", None)
        set_pending_plan_proposal({"summary": "s", "new_plan_md": "x", "reason": "r", "proposed_for_activity": 444})
        assert get_pending_plan_proposal() is not None

        # update_workout doesn't call _consume_pending_proposal — clearing
        # the proposal here is purely the auto-resolve path's job.
        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "user accepted"},
            state,
        )

        assert get_pending_plan_proposal() is None
        assert state.get_all_reviews()[0]["status"] == "approved"

    def test_auto_resolve_no_matching_review_keeps_proposal(self, state, monkeypatch, fake_redis):
        """If the proposal points at a Strava id with no Pending review,
        nothing is flipped and the proposal stays in Redis (a later
        update_plan or a subsequent review may yet apply it)."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import (
            get_pending_plan_proposal,
            set_pending_plan_proposal,
        )

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)
        set_pending_plan_proposal({"summary": "s", "new_plan_md": "x", "reason": "r", "proposed_for_activity": 999})

        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "x"},
            state,
        )

        assert get_pending_plan_proposal() is not None

    def test_readiness_review_only_resolves_on_matching_plan(self, state, monkeypatch, fake_redis):
        """Issue #53: a readiness proposal (review_id backlink, no strava id)
        must flip to 'approved' ONLY when update_plan writes the proposed
        plan. An unrelated full rewrite in the same turn (user declined and
        asked for something else) must leave the review Pending."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import set_pending_plan_proposal

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)

        proposed = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | recovery |\n"
        )
        review = state.save_review(
            None,
            None,
            _date(2026, 4, 28),
            "Poor sleep — back off tomorrow.",
            proposed_change={"summary": "easy", "new_plan_md": proposed, "reason": "sleep"},
            kind="readiness",
        )
        set_pending_plan_proposal(
            {"summary": "easy", "new_plan_md": proposed, "reason": "sleep", "review_id": review["id"]}
        )

        # User declined and asked for a DIFFERENT rewrite.
        unrelated = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | 6x800 @ 5k | 6:00 | quality |\n"
        )
        execute_tool("update_plan", {"new_plan_markdown": unrelated, "change_reason": "different idea"}, state)
        assert state.get_all_reviews()[0]["status"] is None  # still Pending — not the apply

    def test_readiness_review_resolves_when_applied_verbatim(self, state, monkeypatch, fake_redis):
        """The match path: writing the proposed plan (whitespace-reflowed) DOES
        flip the readiness review to approved."""
        from datetime import date as _date

        import temporal_context
        from pending_proposal_store import set_pending_plan_proposal

        monkeypatch.setattr(temporal_context, "today_local", lambda: _date(2026, 4, 28))
        monkeypatch.setattr(state, "_notify_mirror_review", lambda entry: None)

        proposed = (
            "# Plan\n\n## This Week\n\n"
            "| Day | Date | Workout | Pace target | Notes |\n"
            "|-----|------|---------|-------------|-------|\n"
            "| Tue | 2026-04-28 | Easy 4mi | 8:45-9:15 | recovery |\n"
        )
        review = state.save_review(
            None,
            None,
            _date(2026, 4, 28),
            "Poor sleep.",
            proposed_change={"summary": "easy", "new_plan_md": proposed, "reason": "sleep"},
            kind="readiness",
        )
        set_pending_plan_proposal(
            {"summary": "easy", "new_plan_md": proposed, "reason": "sleep", "review_id": review["id"]}
        )

        # Apply: same content, reflowed with extra blank lines (normalize-equal).
        applied = proposed.replace("\n\n", "\n\n\n") + "\n"
        execute_tool("update_plan", {"new_plan_markdown": applied, "change_reason": "apply"}, state)
        assert state.get_all_reviews()[0]["status"] == "approved"

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
        # Prose still written despite warning (agent decides whether to retry)
        assert "Plan v2 with no table" in state.get_plan_meta()

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


# ------------- patch tools (PR B / issue #19) -------------


class TestPatchTools:
    """update_workout and replace_week_table — the small-args plan-edit
    tools that replace the whole-plan update_plan path for common cases."""

    def _pin_today(self, monkeypatch, target):
        from datetime import date as _date

        import temporal_context

        if isinstance(target, str):
            target = _date.fromisoformat(target)
        monkeypatch.setattr(temporal_context, "today_local", lambda: target)

    def test_update_workout_dispatch(self, state, monkeypatch):
        """End-to-end: tool call -> handler -> state.update_workout -> plan
        contains the new cell, no warning emitted."""
        self._pin_today(monkeypatch, "2026-04-28")
        out = execute_tool(
            "update_workout",
            {
                "date": "2026-04-28",
                "workout": "Easy 5mi + 6 strides",
                "change_reason": "more strides",
            },
            state,
        )
        assert out["ok"] is True
        assert out["date"] == "2026-04-28"
        assert "warning" not in out
        assert state.get_todays_workout(date(2026, 4, 28))["workout"] == "Easy 5mi + 6 strides"

    def test_update_workout_with_detail_body(self, state, monkeypatch):
        """detail_body in the same call sets the row's per-day prose."""
        self._pin_today(monkeypatch, "2026-04-28")
        out = execute_tool(
            "update_workout",
            {
                "date": "2026-04-28",
                "workout": "Workout 5x800",
                "detail_body": "WU 1mi. Work 5x800. CD 1mi.",
                "change_reason": "tuesday quality",
            },
            state,
        )
        assert out["ok"] is True
        row = state.get_workout_row(date(2026, 4, 28))
        assert row["prescribed_workout"] == "Workout 5x800"
        assert row["detail_md"] == "WU 1mi. Work 5x800. CD 1mi."

    def test_update_workout_unknown_date_returns_self_routing_error(self, state, monkeypatch):
        """An unknown date surfaces an error whose message tells the LLM
        which other tool to reach for (replace_week_table / update_plan)
        instead of silently inserting an orphan day."""
        self._pin_today(monkeypatch, "2026-04-28")
        out = execute_tool(
            "update_workout",
            {
                "date": "2030-12-31",
                "workout": "Anything",
                "change_reason": "x",
            },
            state,
        )
        assert "error" in out
        err = out["error"]
        assert "2030-12-31" in err
        assert "replace_week_table" in err
        assert "update_plan" in err
        # The row must not have been silently inserted.
        assert state.get_todays_workout(date(2030, 12, 31))["found"] is False

    def test_update_workout_does_not_clear_pending_proposal(self, state, monkeypatch, fake_redis):
        """Patch-style edits are surgical, not proposal-apply. A pending
        proposal must survive an update_workout call (only update_plan
        consumes it)."""
        self._pin_today(monkeypatch, "2026-04-28")
        from pending_proposal_store import (
            get_pending_plan_proposal,
            set_pending_plan_proposal,
        )

        set_pending_plan_proposal({"summary": "x", "new_plan_md": "...", "reason": "..."})
        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "x"},
            state,
        )
        assert get_pending_plan_proposal() is not None

    def test_replace_week_table_dispatch(self, state, monkeypatch):
        """End-to-end bulk replacement via the dispatcher."""
        self._pin_today(monkeypatch, "2026-04-28")
        new_rows = [
            {"day": "Mon", "date": "2026-05-04", "workout": "Off", "pace_target": "—", "notes": ""},
            {
                "day": "Tue",
                "date": "2026-05-05",
                "workout": "Easy 5mi",
                "pace_target": "8:45-9:15",
                "notes": "",
            },
        ]
        out = execute_tool(
            "replace_week_table",
            {"rows": new_rows, "change_reason": "next week"},
            state,
        )
        assert out["ok"] is True
        assert out["rows_written"] == 2
        assert state.get_todays_workout(date(2026, 5, 5))["workout"] == "Easy 5mi"

    def test_replace_week_table_breaks_today_returns_warning(self, state, monkeypatch):
        """If the replaced window spans today but omits today's row, the
        post-write check surfaces a warning so the agent can self-correct."""
        self._pin_today(monkeypatch, "2026-04-28")
        # Window 04-27..04-29 covers today (04-28) but skips it.
        new_rows = [
            {"day": "Mon", "date": "2026-04-27", "workout": "Off", "pace_target": "—", "notes": ""},
            {"day": "Wed", "date": "2026-04-29", "workout": "Easy 5mi", "pace_target": "8:45", "notes": ""},
        ]
        out = execute_tool(
            "replace_week_table",
            {"rows": new_rows, "change_reason": "next week"},
            state,
        )
        assert out["ok"] is True
        assert "warning" in out
        assert "not parseable" in out["warning"]


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

    def test_get_today_no_races(self, state):
        _seed_athlete(state, "name: Test\n")
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

    def test_get_week_status_marks_completed_when_log_matches(self, state, monkeypatch):
        # Pin "today" so the week range is deterministic relative to PLAN_MD.
        monkeypatch.setattr("tools.plan.today_local", lambda: date(2026, 4, 28))
        # Log an easy run on Tue 4/28 — matches the "Easy 4mi" prescription.
        _seed_log(state, [{"date": "2026-04-28", "type": "easy", "miles": 4.1, "pace_avg": "8:55"}])
        out = execute_tool("get_week_status", {"week_offset": 0}, state)
        tue = next(d for d in out["days"] if d["date"] == "2026-04-28")
        assert tue["found"] is True
        assert tue["completed"] is True
        assert tue["prescription_kind"] == "run"
        assert len(tue["actuals"]) == 1
        assert tue["actuals"][0]["miles"] == 4.1
        assert tue["off_plan_actuals"] == []

    def test_get_week_status_off_plan_does_not_complete_prescription(self, state, monkeypatch):
        monkeypatch.setattr("tools.plan.today_local", lambda: date(2026, 4, 28))
        # Strength only on a "Easy 4mi" day — off-plan; prescription not met.
        _seed_log(state, [{"date": "2026-04-28", "type": "strength"}])
        out = execute_tool("get_week_status", {"week_offset": 0}, state)
        tue = next(d for d in out["days"] if d["date"] == "2026-04-28")
        assert tue["completed"] is False
        assert tue["actuals"] == []
        assert len(tue["off_plan_actuals"]) == 1
        assert tue["off_plan_actuals"][0]["type"] == "strength"


# Two-a-day plan used to exercise multi-session callers.
TWO_A_DAY_PLAN = """\
# Plan

## This Week

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Wed | 2026-05-27 | Easy 5mi (AM) | Z2 | base |
| Wed | 2026-05-27 | 6x400 @ 5K (PM) | reps | quality |
| Thu | 2026-05-28 | 8mi long | Z2 | |
"""


class TestMultiSessionTools:
    """LLM tool surfaces must expose every slot on multi-session days."""

    @pytest.fixture
    def two_a_day_state(self, state_dir):
        s = StateManager(state_dir)
        with s._conn() as c:
            c.execute("INSERT INTO athlete (id, yaml_text) VALUES (1, ?)", (ATHLETE_YAML,))
        s.update_plan(TWO_A_DAY_PLAN, "seed")
        return s

    def test_get_todays_workout_exposes_all_slots(self, two_a_day_state):
        out = execute_tool("get_todays_workout", {"date": "2026-05-27"}, two_a_day_state)
        assert out["found"] is True
        assert out["total_slots"] == 2
        assert len(out["sessions"]) == 2
        slots = sorted((s["slot"], s["slot_label"], s["workout"]) for s in out["sessions"])
        assert slots == [
            ("1", "AM", "Easy 5mi (AM)"),
            ("2", "PM", "6x400 @ 5K (PM)"),
        ]
        # Legacy fields populated from primary (slot 1).
        assert out["workout"] == "Easy 5mi (AM)"
        assert out["slot_label"] == "AM"

    def test_get_todays_workout_single_session_unchanged(self, two_a_day_state):
        out = execute_tool("get_todays_workout", {"date": "2026-05-28"}, two_a_day_state)
        assert out["found"] is True
        assert out["total_slots"] == 1
        assert out["sessions"][0]["slot"] is None
        assert out["sessions"][0]["slot_label"] == ""
        assert out["workout"] == "8mi long"

    def test_get_week_plan_includes_sessions_per_day(self, two_a_day_state, monkeypatch):
        monkeypatch.setattr("tools.plan.today_local", lambda: date(2026, 5, 27))
        out = execute_tool("get_week_plan", {"week_offset": 0}, two_a_day_state)
        wed = next(d for d in out["days"] if d["date"] == "2026-05-27")
        assert wed["total_slots"] == 2
        assert len(wed["sessions"]) == 2
        thu = next(d for d in out["days"] if d["date"] == "2026-05-28")
        assert thu["total_slots"] == 1

    def test_get_week_status_per_slot_completion(self, two_a_day_state, monkeypatch):
        """Closing the AM slot via Strava upload completes only that slot;
        the PM slot remains planned."""
        monkeypatch.setattr("tools.plan.today_local", lambda: date(2026, 5, 27))
        two_a_day_state.append_session(
            {
                "date": "2026-05-27",
                "type": "easy",
                "miles": 5.0,
                "start_local": "2026-05-27T07:00:00Z",
                "details": {"strava_id": 1},
            }
        )
        out = execute_tool("get_week_status", {"week_offset": 0}, two_a_day_state)
        wed = next(d for d in out["days"] if d["date"] == "2026-05-27")
        assert wed["total_slots"] == 2
        # Per-slot completion
        am = next(s for s in wed["sessions"] if s["slot"] == "1")
        pm = next(s for s in wed["sessions"] if s["slot"] == "2")
        assert am["completed"] is True
        assert pm["completed"] is False
        # Day-level: not yet complete since PM is still planned.
        assert wed["completed"] is False

    def test_get_week_status_multi_session_drops_day_level_actuals(self, two_a_day_state, monkeypatch):
        """On multi-session days, day-level actuals/off_plan_actuals are empty:
        per-slot kind classification can't be reliably aggregated at the day
        level. Callers iterate `sessions[]` for per-slot truth instead."""
        monkeypatch.setattr("tools.plan.today_local", lambda: date(2026, 5, 27))
        # AM easy upload + PM workout upload — both correctly close their slots.
        two_a_day_state.append_session(
            {
                "date": "2026-05-27",
                "type": "easy",
                "miles": 5.0,
                "start_local": "2026-05-27T07:00:00Z",
                "details": {"strava_id": 10},
            }
        )
        two_a_day_state.append_session(
            {
                "date": "2026-05-27",
                "type": "workout",
                "miles": 4.5,
                "start_local": "2026-05-27T18:00:00Z",
                "details": {"strava_id": 11},
            }
        )
        out = execute_tool("get_week_status", {"week_offset": 0}, two_a_day_state)
        wed = next(d for d in out["days"] if d["date"] == "2026-05-27")
        assert wed["total_slots"] == 2
        assert wed["actuals"] == []
        assert wed["off_plan_actuals"] == []
        # Truth is in sessions[]
        assert all(s["completed"] for s in wed["sessions"])
        assert wed["completed"] is True


class TestTwoADaySingleAccessors:
    """The legacy singular accessors stay usable but surface multi-session
    context via the new fields."""

    @pytest.fixture
    def two_a_day_state(self, state_dir):
        s = StateManager(state_dir)
        with s._conn() as c:
            c.execute("INSERT INTO athlete (id, yaml_text) VALUES (1, ?)", (ATHLETE_YAML,))
        s.update_plan(TWO_A_DAY_PLAN, "seed")
        return s

    def test_get_todays_workout_returns_primary_slot_with_total_slots(self, two_a_day_state):
        # State method (not the LLM tool): returns one dict for the primary slot.
        w = two_a_day_state.get_todays_workout(date(2026, 5, 27))
        assert w["found"] is True
        assert w["workout"] == "Easy 5mi (AM)"
        assert w["slot"] == "1"
        assert w["total_slots"] == 2
        assert w["slot_label"] == "AM"

    def test_get_todays_workouts_returns_all_slots(self, two_a_day_state):
        ws = two_a_day_state.get_todays_workouts(date(2026, 5, 27))
        assert len(ws) == 2
        assert ws[0]["slot"] == "1"
        assert ws[1]["slot"] == "2"
        assert ws[0]["total_slots"] == 2
        assert ws[1]["total_slots"] == 2

    def test_get_workout_rows_orders_by_slot(self, two_a_day_state):
        rows = two_a_day_state.get_workout_rows(date(2026, 5, 27))
        assert [r["slot"] for r in rows] == ["1", "2"]


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


# ------------- end-of-turn calendar sync debounce (issue #26) -------------


class TestAutoSyncDebounce:
    """Plan-edit tools mark a dirty flag; a single end-of-turn flush fires
    one fire-and-forget gcal sync per turn regardless of how many edits ran.

    Verified properties:
      - N edits in one turn -> exactly one sync call.
      - No edits -> no sync.
      - The sync runs off the response path (in a separate thread).
      - A sync failure does not propagate or break tool responses.
    """

    def _pin_today(self, monkeypatch, target):
        from datetime import date as _date

        import temporal_context

        if isinstance(target, str):
            target = _date.fromisoformat(target)
        monkeypatch.setattr(temporal_context, "today_local", lambda: target)

    def _drain_dirty(self):
        """Reset the module-level dirty flag between tests so leakage from
        earlier tests in this file (which call plan-edit tools without
        flushing) doesn't contaminate the assertion baseline."""
        from tools import state as state_tools

        state_tools._consume_plan_dirty()

    def test_multiple_edits_one_sync(self, state, monkeypatch):
        """Three plan-edit tool calls + one flush -> exactly one sync_plan call."""
        self._pin_today(monkeypatch, "2026-04-28")
        self._drain_dirty()

        calls: list[dict] = []
        # Patch the sync target on the google_calendar.sync module — the daemon
        # thread imports from there inside _run, so patching there is what gets
        # called.
        from google_calendar import sync as gcal_sync
        from tools import state as state_tools

        def _fake_sync(state, dry_run=False):
            calls.append({"dry_run": dry_run})
            return {"inserted": 0, "patched": 0, "deleted": 0, "unchanged": 0, "errors": []}

        monkeypatch.setattr(gcal_sync, "sync_plan", _fake_sync)

        # Three edits in the same "turn"
        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "a"},
            state,
        )
        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 6mi", "change_reason": "b"},
            state,
        )
        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 7mi", "change_reason": "c"},
            state,
        )

        # Single end-of-turn flush -> one sync scheduled
        scheduled = state_tools.flush_pending_calendar_sync(state)
        assert scheduled is True

        # Wait for the daemon thread to run (short timeout — sync is mocked).
        import threading
        import time

        deadline = time.time() + 2.0
        while time.time() < deadline and not calls:
            time.sleep(0.01)
        # Make sure any spawned plan-sync thread finishes before next test.
        for t in threading.enumerate():
            if t.name == "pre-plan-sync":
                t.join(timeout=2.0)

        assert len(calls) == 1

        # A second flush with no further edits should NOT schedule another sync.
        scheduled_again = state_tools.flush_pending_calendar_sync(state)
        assert scheduled_again is False
        assert len(calls) == 1

    def test_flush_with_no_edits_is_noop(self, state, monkeypatch):
        """End-of-turn flush with no plan edits -> sync never called."""
        self._pin_today(monkeypatch, "2026-04-28")
        self._drain_dirty()

        calls: list[dict] = []
        from google_calendar import sync as gcal_sync
        from tools import state as state_tools

        def _fake_sync(state, dry_run=False):
            calls.append({"dry_run": dry_run})
            return {"inserted": 0, "patched": 0, "deleted": 0, "unchanged": 0, "errors": []}

        monkeypatch.setattr(gcal_sync, "sync_plan", _fake_sync)

        # Only a read tool — does not mutate the plan.
        execute_tool("get_sessions", {"start_date": "2026-04-26", "end_date": "2026-04-27"}, state)

        scheduled = state_tools.flush_pending_calendar_sync(state)
        assert scheduled is False
        assert calls == []

    def test_sync_failure_does_not_break_tool_response(self, state, monkeypatch):
        """If gcal sync raises, the tool result already returned to the agent
        is unaffected (the sync runs after the tool returned, on a daemon
        thread, and swallows exceptions)."""
        self._pin_today(monkeypatch, "2026-04-28")
        self._drain_dirty()

        from google_calendar import sync as gcal_sync
        from tools import state as state_tools

        def _broken_sync(state, dry_run=False):
            raise RuntimeError("gcal down")

        monkeypatch.setattr(gcal_sync, "sync_plan", _broken_sync)

        # The tool call still returns ok regardless of gcal's downstream state.
        out = execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "a"},
            state,
        )
        assert out["ok"] is True
        assert "error" not in out

        # Flush schedules the thread; the thread will swallow the RuntimeError.
        # If exceptions propagated, this call (or the thread join below) would
        # raise. We don't observe the swallowed error here — only confirm that
        # flushing + thread completion is clean.
        scheduled = state_tools.flush_pending_calendar_sync(state)
        assert scheduled is True

        import threading

        for t in threading.enumerate():
            if t.name == "pre-plan-sync":
                t.join(timeout=2.0)

    def test_sync_runs_off_the_response_path(self, state, monkeypatch):
        """The sync must execute in a daemon thread, not on the caller's thread,
        so a slow gcal API cannot delay the agent's response. We verify by
        capturing thread identity inside the patched sync function and
        comparing to the test's main thread."""
        self._pin_today(monkeypatch, "2026-04-28")
        self._drain_dirty()

        import threading
        import time

        captured: dict = {}
        sync_started = threading.Event()
        sync_can_finish = threading.Event()

        from google_calendar import sync as gcal_sync
        from tools import state as state_tools

        def _slow_sync(state, dry_run=False):
            captured["thread_name"] = threading.current_thread().name
            captured["is_daemon"] = threading.current_thread().daemon
            captured["thread_id"] = threading.get_ident()
            sync_started.set()
            # Block until the test releases us — proves flush() didn't wait.
            sync_can_finish.wait(timeout=2.0)
            return {"inserted": 0, "patched": 0, "deleted": 0, "unchanged": 0, "errors": []}

        monkeypatch.setattr(gcal_sync, "sync_plan", _slow_sync)

        execute_tool(
            "update_workout",
            {"date": "2026-04-28", "workout": "Easy 5mi", "change_reason": "a"},
            state,
        )

        main_thread_id = threading.get_ident()
        t0 = time.perf_counter()
        scheduled = state_tools.flush_pending_calendar_sync(state)
        flush_ms = (time.perf_counter() - t0) * 1000
        assert scheduled is True

        # flush returns immediately — well under a second even though the sync
        # itself blocks on sync_can_finish.
        assert flush_ms < 500, f"flush_pending_calendar_sync blocked for {flush_ms:.0f}ms"

        # Sync started on a different thread.
        assert sync_started.wait(timeout=2.0), "sync thread never started"
        assert captured["thread_id"] != main_thread_id
        assert captured["is_daemon"] is True
        assert captured["thread_name"] == "pre-plan-sync"

        sync_can_finish.set()
        for t in threading.enumerate():
            if t.name == "pre-plan-sync":
                t.join(timeout=2.0)


# ------------- health summary -------------


HEALTH_TODAY = date(2026, 6, 11)


def _health_row(d: str, **overrides) -> dict:
    base = {
        "date": d,
        "sleep_score": 80,
        "sleep_duration_min": 450,
        "hrv_avg": 85,
        "hrv_baseline": 82,
        "resting_hr": 50,
        "stress_avg": 30,
        "load_short_term": 130.0,
        "load_long_term": 105.0,
        "load_ratio": 1.24,
        "load_comment": "Optimized",
    }
    base.update(overrides)
    return base


@pytest.fixture
def pin_health_today(monkeypatch):
    """Pin the window reference date so reads are deterministic."""
    import tools.health

    monkeypatch.setattr(tools.health, "today_local", lambda: HEALTH_TODAY)
    return HEALTH_TODAY


class TestHealthSummary:
    def test_registered_and_dispatchable(self, state, pin_health_today):
        # Catches a registration regression (e.g. a missing health.HANDLERS merge)
        # that would otherwise sail through CI silently.
        names = [t["function"]["name"] for t in ALL_TOOLS]
        assert "get_health_summary" in names
        out = execute_tool("get_health_summary", {"window_days": 7}, state)
        assert "error" not in out

    def test_happy_path_payload(self, state, pin_health_today):
        state.upsert_daily_health([_health_row("2026-06-10"), _health_row("2026-06-11")])
        out = execute_tool("get_health_summary", {"window_days": 7}, state)
        assert out["has_data"] is True
        assert out["latest_sync_date"] == "2026-06-11"  # most recent in window
        assert "2026-06-11" in out["readiness_table"]
        assert isinstance(out["load_trend"], list)
        assert isinstance(out["signals"], list)

    def test_empty_state_never_denies_integration(self, state, pin_health_today):
        # The reason the tool exists: report the gap honestly, don't claim
        # there is no COROS integration.
        out = execute_tool("get_health_summary", {"window_days": 7}, state)
        assert out["has_data"] is False
        assert out["latest_sync_date"] is None
        assert "never" in out["note"]

    def test_empty_state_reports_last_sync_when_stale(self, state, pin_health_today):
        # Data exists, but all of it predates the requested window.
        state.upsert_daily_health([_health_row("2026-05-01")])
        out = execute_tool("get_health_summary", {"window_days": 7}, state)
        assert out["has_data"] is False
        assert out["latest_sync_date"] == "2026-05-01"
        assert "2026-05-01" in out["note"]

    @pytest.mark.parametrize(
        "arg,expected",
        [
            ({"window_days": 200}, 90),   # clamp to MAX_WINDOW
            ({"window_days": -5}, 1),     # clamp to MIN_WINDOW
            ({"window_days": 0}, 7),      # falsy -> default
            ({"window_days": None}, 7),   # explicit None -> default
            ({"window_days": "abc"}, 7),  # non-coercible -> default, not an error
            ({}, 7),                      # omitted -> default
        ],
    )
    def test_window_days_coercion_and_clamping(self, state, pin_health_today, arg, expected):
        out = execute_tool("get_health_summary", arg, state)
        assert "error" not in out
        assert out["window_days"] == expected

    def test_signals_sleep_average(self):
        from tools.health import _signals

        rows = [_health_row("2026-06-10", sleep_duration_min=450), _health_row("2026-06-11", sleep_duration_min=420)]
        sig = _signals(rows)
        assert any("Average sleep 7h15 over 2 night(s)" in s for s in sig)

    def test_signals_hrv_below_baseline(self):
        from tools.health import _signals

        sig = _signals([_health_row("2026-06-11", hrv_avg=70, hrv_baseline=82)])
        assert any("below baseline" in s and "watch recovery" in s for s in sig)

    def test_signals_hrv_at_or_above_baseline(self):
        from tools.health import _signals

        sig = _signals([_health_row("2026-06-11", hrv_avg=90, hrv_baseline=82)])
        assert any("at or above baseline" in s for s in sig)

    def test_signals_flags_non_optimized_load_only(self):
        from tools.health import _signals

        rows = [
            _health_row("2026-06-10", load_comment="Optimized"),
            _health_row("2026-06-11", load_comment="Excessive"),
        ]
        sig = _signals(rows)
        assert any("1 day(s) with non-optimized training load" in s for s in sig)
