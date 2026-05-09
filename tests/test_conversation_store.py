from conversation_store import MAX_HISTORY_TURNS, add_turn, clear_history, get_history


class TestConversationStore:
    def test_empty_history(self, fake_redis):
        assert get_history() == []

    def test_add_and_get(self, fake_redis):
        add_turn("user", "hello")
        history = get_history()
        assert len(history) == 1
        assert history[0] == {"role": "user", "content": "hello"}

    def test_multiple_turns(self, fake_redis):
        add_turn("user", "hello")
        add_turn("assistant", "hi there")
        history = get_history()
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    def test_clear_history(self, fake_redis):
        add_turn("user", "hello")
        clear_history()
        assert get_history() == []

    def test_sliding_window(self, fake_redis):
        """History should be capped at MAX_HISTORY_TURNS * 2 messages."""
        for i in range(MAX_HISTORY_TURNS * 2 + 4):
            role = "user" if i % 2 == 0 else "assistant"
            add_turn(role, f"msg {i}")

        history = get_history()
        assert len(history) == MAX_HISTORY_TURNS * 2
