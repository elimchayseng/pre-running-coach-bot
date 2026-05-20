"""Tests for strava.handler — webhook event dispatch and propagation retry.

Strava's webhook bus and read API are eventually consistent: a `create`
event fires seconds before the activity becomes fetchable. handler.py
retries on 404 with exponential backoff to ride out the gap.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tenacity import stop_after_attempt, wait_fixed

from state_manager import StateManager
from strava import handler
from strava.client import StravaAPIError

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Tenacity's retry config is captured at decoration time. Use retry_with() to
# override stop/wait at call time without doing real-time sleeps in tests.
_FAST_KW = dict(wait=wait_fixed(0), stop=stop_after_attempt(3))


def _fast_fetch(activity_id: int):
    """retry_with() returns a fresh callable with overridden config."""
    return handler._fetch_with_propagation_retry.retry_with(**_FAST_KW)(activity_id)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    """Tmp state dir, wired into handler module's globals."""
    d = tmp_path / "state"
    d.mkdir()
    (d / "athlete.yaml").write_text("hr_zones:\n  easy_ceiling: 155\n")
    monkeypatch.setattr(handler, "STATE_DIR", d)
    monkeypatch.setattr(handler, "_state", None)
    return d


def _easy_run_activity() -> dict:
    return json.loads((FIXTURES / "strava_activity_easy.json").read_text())


# ---------- 404 predicate ----------


class TestPropagationPredicate:
    def test_404_predicate_matches_strava_404(self):
        err = StravaAPIError("GET /activities/123 -> 404: Not Found")
        assert handler._is_propagation_404(err) is True

    def test_404_predicate_rejects_other_status(self):
        assert handler._is_propagation_404(StravaAPIError("GET /x -> 401: Unauth")) is False
        assert handler._is_propagation_404(StravaAPIError("GET /x -> 500: oops")) is False

    def test_404_predicate_rejects_non_strava_errors(self):
        assert handler._is_propagation_404(ValueError("nope")) is False


# ---------- retry behaviour (using retry_with for fast tests) ----------


class TestPropagationRetry:
    def test_succeeds_after_two_404s(self, monkeypatch):
        attempts = {"n": 0}
        ok = _easy_run_activity()

        def _flaky(activity_id):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise StravaAPIError(f"GET /activities/{activity_id} -> 404: Not Found")
            return ok

        monkeypatch.setattr(handler.client, "get_activity", _flaky)
        result = _fast_fetch(999)
        assert result == ok
        assert attempts["n"] == 3

    def test_gives_up_after_max_attempts(self, monkeypatch):
        attempts = {"n": 0}

        def _always_404(activity_id):
            attempts["n"] += 1
            raise StravaAPIError(f"GET /activities/{activity_id} -> 404: Not Found")

        monkeypatch.setattr(handler.client, "get_activity", _always_404)
        with pytest.raises(StravaAPIError):
            _fast_fetch(999)
        assert attempts["n"] == 3

    def test_non_404_does_not_retry(self, monkeypatch):
        attempts = {"n": 0}

        def _401(activity_id):
            attempts["n"] += 1
            raise StravaAPIError(f"GET /activities/{activity_id} -> 401: Unauthorized")

        monkeypatch.setattr(handler.client, "get_activity", _401)
        with pytest.raises(StravaAPIError):
            _fast_fetch(999)
        assert attempts["n"] == 1


# ---------- handle_event end-to-end ----------


class TestHandleEvent:
    def test_skip_delete_aspect(self, monkeypatch, state_dir):
        """Only create and update events trigger work; delete is ignored."""
        get_mock = MagicMock()
        monkeypatch.setattr(handler.client, "get_activity", get_mock)
        handler.handle_event({"aspect_type": "delete", "object_type": "activity", "object_id": 1})
        get_mock.assert_not_called()

    def test_skip_non_activity_object(self, monkeypatch, state_dir):
        get_mock = MagicMock()
        monkeypatch.setattr(handler.client, "get_activity", get_mock)
        handler.handle_event({"aspect_type": "create", "object_type": "athlete", "object_id": 1})
        get_mock.assert_not_called()

    def test_skip_create_when_already_logged(self, monkeypatch, state_dir):
        s = StateManager(state_dir)
        s.append_session({"date": "2026-05-01", "type": "easy", "miles": 4, "details": {"strava_id": 123}})

        get_mock = MagicMock()
        monkeypatch.setattr(handler.client, "get_activity", get_mock)
        handler.handle_event({"aspect_type": "create", "object_type": "activity", "object_id": 123})
        get_mock.assert_not_called()

    def test_full_create_path_writes_log_and_pings(self, monkeypatch, state_dir):
        # Patch get_activity to return success on first try (no retries needed).
        monkeypatch.setattr(handler.client, "get_activity", lambda aid: _easy_run_activity())
        ping_mock = MagicMock(return_value=True)
        monkeypatch.setattr(handler.notify, "send_activity_ping", ping_mock)
        # Force review to return None so we fall back to the templated ping
        # (covers the path this test was originally written for).
        monkeypatch.setattr(handler.review, "run_post_activity_review", lambda e, s, session_id=None: None)

        handler.handle_event({"aspect_type": "create", "object_type": "activity", "object_id": 12345678902})

        s = StateManager(state_dir)
        sessions = s.get_sessions_in_range(date(2026, 5, 11), date(2026, 5, 11))
        assert len(sessions) == 1
        assert sessions[0]["details"]["strava_id"] == 12345678902
        ping_mock.assert_called_once()

    def test_run_type_sends_review_text_not_templated_ping(self, monkeypatch, state_dir):
        """For a run, the LLM review message is delivered via send_telegram_text;
        the templated send_activity_ping is NOT called."""
        monkeypatch.setattr(handler.client, "get_activity", lambda aid: _easy_run_activity())
        review_mock = MagicMock(return_value="Solid easy run vs plan.\nProposed plan change: x")
        text_mock = MagicMock(return_value=True)
        ping_mock = MagicMock(return_value=True)
        monkeypatch.setattr(handler.review, "run_post_activity_review", review_mock)
        monkeypatch.setattr(handler.notify, "send_telegram_text", text_mock)
        monkeypatch.setattr(handler.notify, "send_activity_ping", ping_mock)

        handler.handle_event({"aspect_type": "create", "object_type": "activity", "object_id": 12345678902})

        review_mock.assert_called_once()
        text_mock.assert_called_once()
        assert "Solid easy run" in text_mock.call_args[0][0]
        ping_mock.assert_not_called()

    def test_run_type_falls_back_to_template_when_review_fails(self, monkeypatch, state_dir):
        """When review returns None, the templated ping still fires so the
        user is never left without a confirmation message."""
        monkeypatch.setattr(handler.client, "get_activity", lambda aid: _easy_run_activity())
        monkeypatch.setattr(handler.review, "run_post_activity_review", lambda e, s, session_id=None: None)
        text_mock = MagicMock(return_value=True)
        ping_mock = MagicMock(return_value=True)
        monkeypatch.setattr(handler.notify, "send_telegram_text", text_mock)
        monkeypatch.setattr(handler.notify, "send_activity_ping", ping_mock)

        handler.handle_event({"aspect_type": "create", "object_type": "activity", "object_id": 12345678902})

        text_mock.assert_not_called()
        ping_mock.assert_called_once()

    def test_non_run_type_skips_review_and_uses_templated_ping(self, monkeypatch, state_dir):
        """Cross-trains/rides/etc. should never invoke the LLM review."""
        # Munge the fixture to look like a cross-train type entry.
        activity = _easy_run_activity()
        activity["type"] = "Ride"
        activity["workout_type"] = 0
        monkeypatch.setattr(handler.client, "get_activity", lambda aid: activity)
        review_mock = MagicMock()
        ping_mock = MagicMock(return_value=True)
        monkeypatch.setattr(handler.review, "run_post_activity_review", review_mock)
        monkeypatch.setattr(handler.notify, "send_activity_ping", ping_mock)

        handler.handle_event({"aspect_type": "create", "object_type": "activity", "object_id": 12345678902})

        review_mock.assert_not_called()
        ping_mock.assert_called_once()


# ---------- aspect_type=update behaviour ----------


class TestHandleUpdateEvent:
    def test_update_replaces_existing_entry(self, monkeypatch, state_dir):
        """Pre-existing entry tagged as 'easy' is replaced when Strava
        reports the activity is now workout_type=3 (workout)."""
        # Seed the log with the same activity classified as easy
        s = StateManager(state_dir)
        original = {
            "date": "2026-05-11",
            "type": "easy",
            "miles": 4.0,
            "details": {"strava_id": 12345678902, "workout_type": None},
        }
        s.append_session(original)

        # Update event: Strava now reports workout_type=3 (workout)
        updated_activity = _easy_run_activity()
        updated_activity["workout_type"] = 3
        monkeypatch.setattr(handler.client, "get_activity", lambda aid: updated_activity)
        ping_mock = MagicMock(return_value=True)
        monkeypatch.setattr(handler.notify, "send_activity_ping", ping_mock)

        handler.handle_event({"aspect_type": "update", "object_type": "activity", "object_id": 12345678902})

        # Entry should be replaced (not duplicated), type now 'workout'
        sessions = s.get_sessions_in_range(date(2026, 5, 11), date(2026, 5, 11))
        assert len(sessions) == 1, "update should replace, not append"
        assert sessions[0]["type"] == "workout"
        assert sessions[0]["details"]["strava_id"] == 12345678902

        # Update path does NOT ping (avoids spam on metadata edits)
        ping_mock.assert_not_called()

    def test_update_for_unknown_activity_is_noop(self, monkeypatch, state_dir):
        """If we don't have the activity in our log, ignore the update.
        Don't fetch, don't append. Probably synced before we started running."""
        get_mock = MagicMock()
        monkeypatch.setattr(handler.client, "get_activity", get_mock)
        handler.handle_event({"aspect_type": "update", "object_type": "activity", "object_id": 99999})
        get_mock.assert_not_called()

        # log.jsonl should remain empty (or just whatever was there before)
        s = StateManager(state_dir)
        assert s.existing_strava_ids() == set()
