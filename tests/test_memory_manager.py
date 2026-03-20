from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

import memory_manager
from memory_manager import (
    CACHE_TTL_SECONDS,
    MAX_MEMORY_CHARS,
    _get_cached_search,
    _truncate_memory,
    retrieve_context_and_constraints,
)


class TestTruncateMemory:
    def test_short_text_unchanged(self):
        assert _truncate_memory("short") == "short"

    def test_none_returns_none(self):
        assert _truncate_memory(None) is None

    def test_empty_string(self):
        assert _truncate_memory("") == ""

    def test_exact_limit(self):
        text = "a" * MAX_MEMORY_CHARS
        assert _truncate_memory(text) == text

    def test_over_limit_truncated(self):
        text = "a" * (MAX_MEMORY_CHARS + 50)
        result = _truncate_memory(text)
        assert len(result) == MAX_MEMORY_CHARS
        assert result.endswith("...")

    def test_custom_limit(self):
        result = _truncate_memory("hello world", max_chars=8)
        assert result == "hello..."


class TestRetrieveContextAndConstraints:
    @pytest.fixture(autouse=True)
    def mock_mem0(self, monkeypatch):
        self.mock_client = MagicMock()
        # Patch the reference inside memory_manager, not config
        monkeypatch.setattr(memory_manager, "mem0_client", self.mock_client)
        # Clear the memory manager cache between tests
        memory_manager._query_cache = {}
        memory_manager._cache_timestamps = {}

    def test_empty_results(self):
        self.mock_client.search.return_value = []
        context, constraints = retrieve_context_and_constraints("test query")
        assert context == ""
        assert constraints == ""

    def test_context_extraction(self):
        self.mock_client.search.return_value = [
            {"memory": "Runs 40 miles per week", "metadata": {}},
        ]
        context, constraints = retrieve_context_and_constraints("weekly mileage")
        assert "40 miles per week" in context
        assert constraints == ""

    def test_constraint_extraction(self):
        self.mock_client.search.return_value = [
            {"memory": "Left knee injury from last month", "metadata": {"type": "injury"}},
        ]
        context, constraints = retrieve_context_and_constraints("training plan")
        assert "knee injury" in constraints

    def test_expired_constraint_filtered(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.mock_client.search.return_value = [
            {"memory": "Old injury", "metadata": {"type": "injury", "expiration_date": yesterday}},
        ]
        context, constraints = retrieve_context_and_constraints("test")
        assert constraints == ""

    def test_active_constraint_kept(self):
        future = (date.today() + timedelta(days=7)).isoformat()
        self.mock_client.search.return_value = [
            {"memory": "Current injury pain", "metadata": {"type": "injury", "expiration_date": future}},
        ]
        context, constraints = retrieve_context_and_constraints("test")
        assert "injury" in constraints

    def test_none_memories_skipped(self):
        self.mock_client.search.return_value = [None, {"memory": "valid", "metadata": {}}]
        context, constraints = retrieve_context_and_constraints("test")
        assert "valid" in context


class TestCachedSearch:
    @pytest.fixture(autouse=True)
    def mock_mem0(self, monkeypatch):
        self.mock_client = MagicMock()
        monkeypatch.setattr(memory_manager, "mem0_client", self.mock_client)
        memory_manager._query_cache = {}
        memory_manager._cache_timestamps = {}

    def test_cache_returns_same_result(self):
        self.mock_client.search.return_value = [{"memory": "cached result", "metadata": {}}]
        result1 = _get_cached_search("test query", limit=3)
        result2 = _get_cached_search("test query", limit=3)
        assert result1 == result2
        # Should only call Mem0 once — second call is cached
        assert self.mock_client.search.call_count == 1

    def test_cache_expires_after_ttl(self, monkeypatch):
        self.mock_client.search.return_value = [{"memory": "old result", "metadata": {}}]
        _get_cached_search("test query", limit=3)

        # Simulate time passing beyond TTL
        past_time = datetime.now() - timedelta(seconds=CACHE_TTL_SECONDS + 10)
        memory_manager._cache_timestamps["runner:test query:3"] = past_time

        self.mock_client.search.return_value = [{"memory": "new result", "metadata": {}}]
        result = _get_cached_search("test query", limit=3)
        assert result[0]["memory"] == "new result"
        assert self.mock_client.search.call_count == 2

    def test_cache_does_not_expire_with_total_seconds_fix(self, monkeypatch):
        """Regression test: .seconds bug would cause cache to never expire after 24h."""
        self.mock_client.search.return_value = [{"memory": "stale", "metadata": {}}]
        _get_cached_search("test query", limit=3)

        # Simulate 25 hours ago (the .seconds bug: timedelta(days=1, seconds=30).seconds == 30)
        old_time = datetime.now() - timedelta(hours=25)
        memory_manager._cache_timestamps["runner:test query:3"] = old_time

        self.mock_client.search.return_value = [{"memory": "fresh", "metadata": {}}]
        result = _get_cached_search("test query", limit=3)
        assert result[0]["memory"] == "fresh"
        assert self.mock_client.search.call_count == 2

    def test_different_queries_cached_separately(self):
        self.mock_client.search.return_value = [{"memory": "result A", "metadata": {}}]
        _get_cached_search("query A", limit=3)

        self.mock_client.search.return_value = [{"memory": "result B", "metadata": {}}]
        _get_cached_search("query B", limit=3)

        assert self.mock_client.search.call_count == 2
