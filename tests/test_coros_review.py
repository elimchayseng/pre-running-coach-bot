"""Tests for coros.review — the nightly readiness check-in.

Mocks the LLM at coros.review's import of _call_review_llm; everything else
(SQLite persistence with kind='readiness', the Redis proposal stash with
review_id backlink, the quiet-night ping policy, the single-proposal-key
collision guard) runs for real against temp stores.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from coros import review
from pending_proposal_store import (
    clear_pending_plan_proposal,
    get_pending_plan_proposal,
    set_pending_plan_proposal,
)
from state_manager import StateManager

TODAY = date(2026, 6, 11)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    return d


@pytest.fixture
def state(state_dir: Path) -> StateManager:
    s = StateManager(state_dir)
    with s._conn() as conn:
        conn.execute("INSERT OR REPLACE INTO athlete (id, yaml_text) VALUES (1, 'name: Test')")
        conn.commit()
    return s


@pytest.fixture
def state_with_health(state, monkeypatch):
    monkeypatch.setattr(review, "today_local", lambda: TODAY)
    state.upsert_daily_health(
        [
            {"date": "2026-06-10", "sleep_score": 55, "sleep_duration_min": 330, "hrv_avg": 65, "hrv_baseline": 82},
            {
                "date": "2026-06-11",
                "stress_avg": 60,
                "recovery_pct": 40,
                "load_ratio": 1.6,
                "load_comment": "Excessive",
            },
        ]
    )
    return state


def _llm_returns(monkeypatch, payload: dict | str):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(review, "_call_review_llm", lambda messages: raw)
    monkeypatch.setattr(review, "llm_client", object())  # non-None gate


_CHANGE = {
    "summary": "Swap tomorrow's tempo for an easy 5",
    "new_plan_md": "| Day | Date | Workout | Pace target | Notes |\n|---|---|---|---|---|",
    "reason": "poor sleep + excessive load",
}


class TestRunReadinessReview:
    def test_skips_without_health_data(self, state, monkeypatch, fake_redis):
        monkeypatch.setattr(review, "today_local", lambda: TODAY)
        monkeypatch.setattr(review, "llm_client", object())

        def _boom(messages):
            raise AssertionError("LLM must not be called without health rows")

        monkeypatch.setattr(review, "_call_review_llm", _boom)
        assert review.run_readiness_review(state) is None

    def test_quiet_night_persists_review_but_no_ping(self, state_with_health, monkeypatch, fake_redis):
        _llm_returns(monkeypatch, {"feedback": "All systems normal.", "concern": False, "plan_change": None})
        assert review.run_readiness_review(state_with_health) is None
        rows = state_with_health.get_reviews_in_range(TODAY, TODAY)
        assert len(rows) == 1
        assert rows[0]["kind"] == "readiness"
        assert rows[0]["session_id"] is None
        assert get_pending_plan_proposal() is None

    def test_concern_without_change_pings(self, state_with_health, monkeypatch, fake_redis):
        _llm_returns(
            monkeypatch,
            {"feedback": "HRV trending down 3 days straight.", "concern": True, "plan_change": None},
        )
        text = review.run_readiness_review(state_with_health)
        assert text is not None
        assert "Nightly readiness check" in text
        assert "HRV trending down" in text
        assert "Proposed plan change" not in text

    def test_always_ping_env_overrides_quiet_night(self, state_with_health, monkeypatch, fake_redis):
        monkeypatch.setenv("COROS_CHECKIN_ALWAYS_PING", "1")
        _llm_returns(monkeypatch, {"feedback": "All good.", "concern": False, "plan_change": None})
        assert review.run_readiness_review(state_with_health) is not None

    def test_plan_change_stashes_proposal_with_review_backlink(self, state_with_health, monkeypatch, fake_redis):
        _llm_returns(
            monkeypatch, {"feedback": "Readiness contradicts tomorrow.", "concern": True, "plan_change": _CHANGE}
        )
        text = review.run_readiness_review(state_with_health)
        assert "Proposed plan change: Swap tomorrow's tempo" in text
        proposal = get_pending_plan_proposal()
        assert proposal["source"] == "readiness"
        rows = state_with_health.get_reviews_in_range(TODAY, TODAY)
        assert proposal["review_id"] == rows[0]["id"]
        assert rows[0]["proposed_change"]["summary"] == _CHANGE["summary"]

    def test_existing_proposal_suppresses_new_one(self, state_with_health, monkeypatch, fake_redis):
        set_pending_plan_proposal({"summary": "earlier proposal", "new_plan_md": "x", "reason": "y"})
        _llm_returns(monkeypatch, {"feedback": "Bad night.", "concern": False, "plan_change": _CHANGE})
        text = review.run_readiness_review(state_with_health)
        # Concern is forced on so the withheld change still pings.
        assert text is not None
        assert "another proposal is already pending" in text
        # The earlier proposal is untouched.
        assert get_pending_plan_proposal()["summary"] == "earlier proposal"
        # Review row persisted WITHOUT the withheld change.
        rows = state_with_health.get_reviews_in_range(TODAY, TODAY)
        assert rows[0]["proposed_change"] is None
        clear_pending_plan_proposal()

    def test_malformed_llm_output_returns_none(self, state_with_health, monkeypatch, fake_redis):
        _llm_returns(monkeypatch, "not json at all")
        assert review.run_readiness_review(state_with_health) is None
        assert state_with_health.get_reviews_in_range(TODAY, TODAY) == []

    def test_llm_none_skips(self, state_with_health, monkeypatch):
        monkeypatch.setattr(review, "llm_client", None)
        assert review.run_readiness_review(state_with_health) is None

    def test_messages_carry_readiness_inputs(self, state_with_health, monkeypatch, fake_redis):
        captured = {}

        def _capture(messages):
            captured["messages"] = messages
            return json.dumps({"feedback": "ok", "concern": False, "plan_change": None})

        monkeypatch.setattr(review, "_call_review_llm", _capture)
        monkeypatch.setattr(review, "llm_client", object())
        review.run_readiness_review(state_with_health)
        system, user = captured["messages"]
        assert "nightly readiness" in system["content"].lower()
        payload = json.loads(user["content"])
        assert payload["tomorrow"] == "2026-06-12"
        assert len(payload["readiness_last_7_days"]) == 2
        assert "load_trend_4_weeks" in payload


class TestSaveReviewKind:
    def test_default_kind_is_activity(self, state):
        row = state.save_review(None, None, TODAY, "critique")
        assert row["kind"] == "activity"

    def test_readiness_kind_persists(self, state):
        row = state.save_review(None, None, TODAY, "critique", kind="readiness")
        assert row["kind"] == "readiness"

    def test_v8_lands_on_existing_v7_db(self, state_dir):
        """A reviews table created before v8 gains `kind` on next connect."""
        import sqlite3

        import state_manager as sm

        db = state_dir / "coach.db"
        ddl = sm.SCHEMA_PATH.read_text()
        # Strip the v8 kind column from the reviews DDL to simulate a v7 DB.
        ddl_v7 = ddl.replace(
            ",\n    -- v8: 'activity' = post-activity review; 'readiness' = nightly COROS\n"
            "    -- check-in (no session_id/strava_id). Pre-v8 DBs gain this via the\n"
            "    -- PRAGMA-guarded ALTER in state_manager._ensure_schema.\n"
            "    kind            TEXT    NOT NULL DEFAULT 'activity'\n",
            "\n",
        )
        assert "kind" not in ddl_v7.split("CREATE TABLE IF NOT EXISTS reviews")[1].split(";")[0]
        conn = sqlite3.connect(db)
        conn.executescript(ddl_v7)
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7)")
        conn.commit()
        conn.close()

        state = StateManager(state_dir)
        row = state.save_review(None, None, TODAY, "c", kind="readiness")
        assert row["kind"] == "readiness"


class TestAutoResolveReviewIdBranch:
    def test_review_id_proposal_flips_directly(self, state, fake_redis):
        from tools.state import _auto_resolve_matching_review

        row = state.save_review(None, None, TODAY, "readiness critique", kind="readiness")
        set_pending_plan_proposal(
            {"summary": "s", "new_plan_md": "x", "reason": "r", "source": "readiness", "review_id": row["id"]}
        )
        _auto_resolve_matching_review(state)
        rows = state.get_reviews_in_range(TODAY, TODAY)
        assert rows[0]["status"] == "approved"
        assert get_pending_plan_proposal() is None

    def test_strava_branch_still_works(self, state, fake_redis):
        from tools.state import _auto_resolve_matching_review

        state.save_review(None, 555, TODAY, "activity critique")
        set_pending_plan_proposal({"summary": "s", "new_plan_md": "x", "reason": "r", "proposed_for_activity": 555})
        _auto_resolve_matching_review(state)
        rows = state.get_reviews_in_range(TODAY, TODAY)
        assert rows[0]["status"] == "approved"
        assert get_pending_plan_proposal() is None
