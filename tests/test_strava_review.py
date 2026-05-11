"""Tests for strava.review — post-activity LLM analysis and proposal stashing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from state_manager import StateManager
from strava import review

ATHLETE_YAML = """\
name: Test Runner
hr_zones:
  easy_ceiling: 155
target_races:
  - name: Future Race
    date: 2099-06-20
    priority: A
"""

PLAN_MD = """\
# Plan

## This Week

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Mon | 2026-05-11 | Easy 4mi | 8:30-9:00 | |
| Tue | 2026-05-12 | 5mi w/ 3x1000m | 6:00-6:05 reps | |
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


@pytest.fixture
def easy_entry() -> dict:
    return {
        "date": "2026-05-11",
        "type": "easy",
        "miles": 4.0,
        "pace_avg": "8:45",
        "hr_avg": 142,
        "details": {
            "strava_id": 999,
            "elevation_gain_ft": 30,
            "moving_time": "35m 0s",
            "laps": [
                {"name": "Lap 1", "distance_mi": 1.0, "pace": "8:45", "hr_avg": 140},
            ],
        },
    }


# ---------- is_run_type ----------


class TestIsRunType:
    @pytest.mark.parametrize("t", ["run", "easy", "long_run", "workout", "race", "strides"])
    def test_run_types(self, t):
        assert review.is_run_type({"type": t}) is True

    @pytest.mark.parametrize("t", ["cross_train", "strength", "ride", "swim", None])
    def test_non_run_types(self, t):
        assert review.is_run_type({"type": t}) is False


# ---------- _trim_entry ----------


class TestTrimEntry:
    def test_keeps_core_fields(self, easy_entry):
        out = review._trim_entry(easy_entry)
        for k in ("date", "type", "miles", "pace_avg", "hr_avg"):
            assert k in out

    def test_drops_unused_detail_fields(self):
        entry = {
            "date": "2026-05-11",
            "type": "easy",
            "details": {"strava_id": 1, "gear_id": "x", "kudos_count": 5, "elevation_gain_ft": 100},
        }
        out = review._trim_entry(entry)
        assert "elevation_gain_ft" in out["details"]
        assert "gear_id" not in out["details"]
        assert "kudos_count" not in out["details"]
        # strava_id is intentionally excluded from prompt details
        assert "strava_id" not in out["details"]

    def test_caps_laps_at_12(self):
        laps = [{"name": f"L{i}", "distance_mi": 1.0, "pace": "7:00", "hr_avg": 150} for i in range(20)]
        entry = {"date": "2026-05-11", "type": "workout", "details": {"laps": laps}}
        out = review._trim_entry(entry)
        assert len(out["details"]["laps"]) == 12


# ---------- _build_messages ----------


class TestBuildMessages:
    def test_two_messages_with_planned_row(self, state, easy_entry):
        msgs = review._build_messages(easy_entry, state)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        user = msgs[1]["content"]
        # Planned workout for 2026-05-11 (Mon) is "Easy 4mi"
        assert "Easy 4mi" in user
        # Activity data included
        assert "8:45" in user
        # Athlete profile included
        assert "easy_ceiling" in user

    def test_excludes_just_logged_activity_from_recent(self, state, easy_entry):
        # Pre-seed recent log with the same strava_id; review should exclude it.
        state.append_session(easy_entry)
        msgs = review._build_messages(easy_entry, state)
        user_data = json.loads(msgs[1]["content"])
        recent = user_data["recent_sessions_last_14_days"]
        for r in recent:
            assert (r.get("details") or {}).get("strava_id") != 999


# ---------- _parse_review_output ----------


class TestParseReviewOutput:
    def test_happy_path_no_plan_change(self):
        raw = json.dumps({"feedback": "Solid easy run.", "plan_change": None})
        parsed = review._parse_review_output(raw)
        assert parsed == {"feedback": "Solid easy run.", "plan_change": None}

    def test_happy_path_with_plan_change(self):
        raw = json.dumps(
            {
                "feedback": "Ran too hard.",
                "plan_change": {
                    "summary": "Drop Thursday tempo to easy.",
                    "new_plan_md": "# Plan\n...",
                    "reason": "overreach",
                },
            }
        )
        parsed = review._parse_review_output(raw)
        assert parsed["plan_change"]["summary"].startswith("Drop")

    def test_strips_code_fence(self):
        raw = "```json\n" + json.dumps({"feedback": "ok", "plan_change": None}) + "\n```"
        parsed = review._parse_review_output(raw)
        assert parsed == {"feedback": "ok", "plan_change": None}

    def test_malformed_json_returns_none(self):
        assert review._parse_review_output("not json at all") is None

    def test_missing_feedback_returns_none(self):
        raw = json.dumps({"plan_change": None})
        assert review._parse_review_output(raw) is None

    def test_partial_plan_change_dropped(self):
        raw = json.dumps(
            {
                "feedback": "ok",
                "plan_change": {"summary": "x"},  # missing new_plan_md, reason
            }
        )
        parsed = review._parse_review_output(raw)
        assert parsed["feedback"] == "ok"
        assert parsed["plan_change"] is None

    def test_plan_change_not_object_dropped(self):
        raw = json.dumps({"feedback": "ok", "plan_change": "string"})
        parsed = review._parse_review_output(raw)
        assert parsed["plan_change"] is None

    def test_empty_feedback_returns_none(self):
        """A model returning an empty feedback string is treated as malformed —
        otherwise we'd ship a Telegram message that's just the 'Logged: …' header."""
        assert review._parse_review_output(json.dumps({"feedback": "", "plan_change": None})) is None
        assert review._parse_review_output(json.dumps({"feedback": "   ", "plan_change": None})) is None

    def test_oversized_new_plan_md_dropped(self):
        """Defensive: an LLM that returns a huge new_plan_md gets its plan_change
        dropped so we don't bloat Redis / the next system prompt."""
        huge = "x" * (review._MAX_NEW_PLAN_MD_CHARS + 1)
        raw = json.dumps(
            {
                "feedback": "fine",
                "plan_change": {"summary": "s", "new_plan_md": huge, "reason": "r"},
            }
        )
        parsed = review._parse_review_output(raw)
        assert parsed["feedback"] == "fine"
        assert parsed["plan_change"] is None


# ---------- _format_user_message ----------


class TestFormatUserMessage:
    def test_no_plan_change_includes_header_and_feedback(self, easy_entry):
        parsed = {"feedback": "Easy held the zone.", "plan_change": None}
        msg = review._format_user_message(parsed, easy_entry)
        assert "Logged" in msg
        assert "4.0mi" in msg
        assert "@ 8:45" in msg
        assert "Easy held the zone." in msg
        assert "Proposed plan change" not in msg

    def test_with_plan_change_includes_summary_and_confirm_line(self, easy_entry):
        parsed = {
            "feedback": "Overreach signals.",
            "plan_change": {"summary": "Shift threshold", "new_plan_md": "...", "reason": "..."},
        }
        msg = review._format_user_message(parsed, easy_entry)
        assert "Proposed plan change: Shift threshold" in msg
        assert "Reply 'yes' to apply" in msg

    def test_truncates_to_telegram_limit(self, easy_entry):
        """A pathologically long feedback string gets truncated so Telegram
        won't reject the message (4096-char hard cap)."""
        parsed = {"feedback": "x" * 10000, "plan_change": None}
        msg = review._format_user_message(parsed, easy_entry)
        assert len(msg) <= review._TELEGRAM_MAX_CHARS
        assert msg.endswith("…")


# ---------- run_post_activity_review (integration of helpers) ----------


def _mock_llm_response(monkeypatch, content: str) -> MagicMock:
    fake_client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=content))]
    fake_client.chat.completions.create.return_value = completion
    monkeypatch.setattr(review, "llm_client", fake_client)
    return fake_client


class TestRunPostActivityReview:
    def test_happy_no_proposal(self, state, easy_entry, monkeypatch, fake_redis):
        _mock_llm_response(
            monkeypatch,
            json.dumps({"feedback": "Solid easy run held the easy ceiling.", "plan_change": None}),
        )
        out = review.run_post_activity_review(easy_entry, state)
        assert out is not None
        assert "Solid easy run" in out
        # No proposal should be stashed
        from pending_proposal_store import get_pending_plan_proposal

        assert get_pending_plan_proposal() is None

    def test_happy_with_proposal_stashes_to_redis(self, state, easy_entry, monkeypatch, fake_redis):
        _mock_llm_response(
            monkeypatch,
            json.dumps(
                {
                    "feedback": "Overcooked — HR was high.",
                    "plan_change": {
                        "summary": "Demote Thursday tempo",
                        "new_plan_md": "# revised\n",
                        "reason": "overreach signals",
                    },
                }
            ),
        )
        out = review.run_post_activity_review(easy_entry, state)
        assert "Proposed plan change" in out

        from pending_proposal_store import get_pending_plan_proposal

        stashed = get_pending_plan_proposal()
        assert stashed["summary"] == "Demote Thursday tempo"
        assert stashed["new_plan_md"] == "# revised\n"
        assert stashed["reason"] == "overreach signals"
        assert stashed["proposed_for_activity"] == 999

    def test_llm_raises_returns_none(self, state, easy_entry, monkeypatch):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = RuntimeError("boom")
        monkeypatch.setattr(review, "llm_client", fake_client)
        out = review.run_post_activity_review(easy_entry, state)
        assert out is None

    def test_malformed_json_returns_none(self, state, easy_entry, monkeypatch):
        _mock_llm_response(monkeypatch, "not actually json")
        out = review.run_post_activity_review(easy_entry, state)
        assert out is None

    def test_llm_client_none_returns_none(self, state, easy_entry, monkeypatch):
        monkeypatch.setattr(review, "llm_client", None)
        out = review.run_post_activity_review(easy_entry, state)
        assert out is None

    def test_proposal_stash_failure_still_delivers_feedback(self, state, easy_entry, monkeypatch, fake_redis):
        """Redis down: stashing the proposal raises after retries, but the
        review still delivers the analysis message to the user — with the
        proposal stripped since it isn't applyable without Redis."""
        _mock_llm_response(
            monkeypatch,
            json.dumps(
                {
                    "feedback": "Overcooked the easy.",
                    "plan_change": {
                        "summary": "Demote Thursday tempo",
                        "new_plan_md": "# revised\n",
                        "reason": "overreach",
                    },
                }
            ),
        )

        def _raise(_payload):
            raise RuntimeError("redis unreachable")

        monkeypatch.setattr(review, "set_pending_plan_proposal", _raise)

        out = review.run_post_activity_review(easy_entry, state)
        assert out is not None
        assert "Overcooked the easy." in out
        # Proposal lines must NOT appear, since it couldn't be stashed.
        assert "Proposed plan change" not in out
