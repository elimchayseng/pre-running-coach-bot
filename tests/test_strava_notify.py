"""Tests for strava.notify — auto-log ping formatting and Telegram send path.

The async/sync wrapper in send_activity_ping is the most fragile part of
the auto-log flow. These tests pin the templated output and verify the
graceful-degrade behaviour when env config is missing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from strava import notify

# ---------- _format_ping ----------


class TestFormatPing:
    def test_full_entry_renders_all_pieces(self):
        entry = {
            "date": "2026-05-12",
            "type": "workout",
            "miles": 8.0,
            "pace_avg": "7:30",
            "hr_avg": 145,
            "details": {
                "elevation_gain_ft": 250,
                "moving_time": "1h 0m 0s",
                "laps": [
                    {"name": "WU"},
                    {"name": "Rep 1"},
                    {"name": "Recovery"},
                    {"name": "Rep 2"},
                    {"name": "Recovery"},
                    {"name": "Rep 3"},
                    {"name": "CD"},
                ],
            },
        }
        text = notify._format_ping(entry)
        assert "8.0mi" in text
        assert "@ 7:30" in text
        assert "(workout)" in text
        assert "HR avg 145" in text
        assert "250ft gain" in text
        assert "1h 0m 0s" in text
        assert "3 work laps" in text
        assert "RPE?" in text

    def test_minimal_entry_still_readable(self):
        entry = {"date": "2026-05-12", "type": "cross_train"}
        text = notify._format_ping(entry)
        # Should not raise KeyError on missing fields
        assert "Logged" in text
        assert "(cross_train)" in text
        assert "RPE?" in text

    def test_no_laps_no_work_lap_count(self):
        entry = {
            "date": "2026-05-12",
            "type": "easy",
            "miles": 4.0,
            "hr_avg": 138,
            "details": {"elevation_gain_ft": 12, "moving_time": "36m 0s"},
        }
        text = notify._format_ping(entry)
        assert "work laps" not in text

    def test_single_rep_lap_doesnt_trigger_count(self):
        """Only 2+ work laps surface the hint — one rep is just an interval."""
        entry = {
            "date": "2026-05-12",
            "type": "workout",
            "details": {"laps": [{"name": "WU"}, {"name": "Rep 1"}, {"name": "CD"}]},
        }
        text = notify._format_ping(entry)
        assert "work laps" not in text


# ---------- send_activity_ping ----------


class TestSendActivityPing:
    @pytest.fixture
    def entry(self) -> dict:
        return {
            "date": "2026-05-12",
            "type": "easy",
            "miles": 4.0,
            "details": {"strava_id": 12345},
        }

    def test_missing_chat_id_returns_false(self, monkeypatch, entry):
        monkeypatch.delenv("USER_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
        # Default chat_id (env) is missing AND not passed → False, no raise
        assert notify.send_activity_ping(entry) is False

    def test_missing_token_returns_false(self, monkeypatch, entry):
        monkeypatch.setenv("USER_TELEGRAM_CHAT_ID", "12345")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert notify.send_activity_ping(entry) is False

    def _patch_bot(self, monkeypatch, send_mock):
        """Build a fake telegram.Bot whose send_message records calls into send_mock."""

        class _FakeBot:
            def __init__(self, token):
                self.token = token

            async def send_message(self, chat_id, text):
                send_mock(chat_id, text)

        monkeypatch.setattr("telegram.Bot", _FakeBot)

    def test_explicit_chat_id_overrides_env(self, monkeypatch, entry):
        """An explicit chat_id arg works even when the env var is missing."""
        monkeypatch.delenv("USER_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
        send_mock = MagicMock()
        self._patch_bot(monkeypatch, send_mock)

        assert notify.send_activity_ping(entry, chat_id="99999") is True
        send_mock.assert_called_once()
        chat_arg, text_arg = send_mock.call_args[0]
        assert chat_arg == "99999"
        assert "4.0mi" in text_arg

    def test_happy_path_with_env(self, monkeypatch, entry):
        monkeypatch.setenv("USER_TELEGRAM_CHAT_ID", "55555")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
        send_mock = MagicMock()
        self._patch_bot(monkeypatch, send_mock)

        assert notify.send_activity_ping(entry) is True
        send_mock.assert_called_once()
        chat_arg, text_arg = send_mock.call_args[0]
        assert chat_arg == "55555"
        assert "Logged" in text_arg

    def test_send_value_error_returns_false(self, monkeypatch, entry):
        """Non-RuntimeError exceptions from the bot should return False
        rather than propagating (called from background thread)."""
        monkeypatch.setenv("USER_TELEGRAM_CHAT_ID", "55555")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")

        class _BrokenBot:
            def __init__(self, token):
                pass

            async def send_message(self, chat_id, text):
                raise ValueError("telegram lib error")

        monkeypatch.setattr("telegram.Bot", _BrokenBot)
        assert notify.send_activity_ping(entry) is False

    def test_send_runtime_error_in_fallback_returns_false(self, monkeypatch, entry):
        """If asyncio.run raises RuntimeError, we try the fallback loop. If
        the SEND itself raises (network etc.), the fallback's exception
        handler returns False rather than propagating."""
        monkeypatch.setenv("USER_TELEGRAM_CHAT_ID", "55555")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")

        class _BrokenBot:
            def __init__(self, token):
                pass

            async def send_message(self, chat_id, text):
                raise RuntimeError("network unreachable")

        monkeypatch.setattr("telegram.Bot", _BrokenBot)
        assert notify.send_activity_ping(entry) is False

    def test_successful_send_mirrors_to_conversation_history(self, monkeypatch, entry):
        """A sent message must be mirrored into Redis history so the next
        chat turn knows what the bot already said."""
        monkeypatch.setenv("USER_TELEGRAM_CHAT_ID", "55555")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")
        self._patch_bot(monkeypatch, MagicMock())

        mirrored = []
        import conversation_store

        monkeypatch.setattr(
            conversation_store, "add_turn", lambda role, content: mirrored.append((role, content))
        )

        assert notify.send_activity_ping(entry) is True
        assert len(mirrored) == 1
        role, content = mirrored[0]
        assert role == "assistant"
        assert "Logged" in content

    def test_failed_send_does_not_mirror(self, monkeypatch, entry):
        monkeypatch.setenv("USER_TELEGRAM_CHAT_ID", "55555")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake_token")

        class _BrokenBot:
            def __init__(self, token):
                pass

            async def send_message(self, chat_id, text):
                raise ValueError("nope")

        monkeypatch.setattr("telegram.Bot", _BrokenBot)
        mirrored = []
        import conversation_store

        monkeypatch.setattr(
            conversation_store, "add_turn", lambda role, content: mirrored.append((role, content))
        )

        assert notify.send_activity_ping(entry) is False
        assert mirrored == []  # nothing actually got sent; nothing to mirror
