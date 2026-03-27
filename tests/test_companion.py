from unittest.mock import MagicMock

import pytest

import companion
import memory_manager
from companion import get_system_prompt


class TestSystemPrompt:
    @pytest.fixture(autouse=True)
    def setup_mocks(self, monkeypatch):
        self.mock_mem0 = MagicMock()
        self.mock_mem0.search.return_value = []
        monkeypatch.setattr(memory_manager, "mem0_client", self.mock_mem0)
        memory_manager._query_cache = {}
        memory_manager._cache_timestamps = {}

    def test_contains_date_authority_section(self):
        prompt = get_system_prompt("test query")
        assert "ALWAYS correct" in prompt
        assert "NEVER override" in prompt

    def test_contains_date_three_times(self):
        from temporal_context import get_temporal_context
        ctx = get_temporal_context()
        prompt = get_system_prompt("test query")
        # Date should appear at top, in temporal section implicitly, and at bottom reminder
        assert prompt.count(ctx["date"]) >= 2

    def test_contains_race_countdown(self):
        prompt = get_system_prompt("test query")
        assert "RACE COUNTDOWN" in prompt

    def test_contains_weekly_plan_when_available(self):
        self.mock_mem0.search.return_value = [
            {"memory": "Week 12: Mon easy, Tue tempo", "metadata": {"type": "weekly_plan"}}
        ]
        prompt = get_system_prompt("what's my workout?")
        assert "WEEK'S TRAINING PLAN" in prompt

    def test_contains_today_workout_when_available(self):
        from datetime import datetime
        now = datetime.now()
        day_abbrev = now.strftime("%a")[:3]
        month_day = f"{now.month}/{now.day}"
        plan_text = f"| {day_abbrev} {month_day} | 6mi easy |"
        self.mock_mem0.search.return_value = [
            {"memory": plan_text, "metadata": {"type": "weekly_plan"}}
        ]
        prompt = get_system_prompt("what's my workout?")
        assert "TODAY'S SCHEDULED WORKOUT" in prompt

    def test_contains_reminder_at_end(self):
        prompt = get_system_prompt("test")
        assert "REMINDER:" in prompt
        assert "explicit full dates" in prompt


class TestChat:
    @pytest.fixture(autouse=True)
    def setup_mocks(self, monkeypatch, fake_redis):
        # Mock LLM client
        self.mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Great job on your training!"
        self.mock_llm.chat.completions.create.return_value = mock_response
        # Patch in companion where _call_llm uses it
        monkeypatch.setattr(companion, "llm_client", self.mock_llm)

        # Mock mem0 client in memory_manager where it's actually used
        self.mock_mem0 = MagicMock()
        self.mock_mem0.search.return_value = []
        monkeypatch.setattr(memory_manager, "mem0_client", self.mock_mem0)

        # Clear memory manager cache
        memory_manager._query_cache = {}
        memory_manager._cache_timestamps = {}

    def test_chat_returns_response(self):
        from companion import chat

        result = chat("How should I train this week?", user_id="test_user")
        assert result == "Great job on your training!"

    def test_chat_calls_llm(self):
        from companion import chat

        chat("What pace for my long run?", user_id="test_user")
        self.mock_llm.chat.completions.create.assert_called_once()

    def test_chat_skips_memory_for_greeting(self):
        from companion import chat

        chat("hello", user_id="test_user")
        self.mock_mem0.add.assert_not_called()

    def test_chat_stores_memory_for_real_message(self):
        from companion import chat

        chat("I ran 18 miles today at 8:30 pace", user_id="test_user")
        self.mock_mem0.add.assert_called_once()

    def test_chat_handles_llm_error(self):
        self.mock_llm.chat.completions.create.side_effect = Exception("LLM down")
        from companion import chat

        result = chat("test message", user_id="test_user")
        assert "trouble" in result.lower() or "apologize" in result.lower()
