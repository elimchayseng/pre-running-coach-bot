from unittest.mock import MagicMock

import pytest

import companion
import memory_manager


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
