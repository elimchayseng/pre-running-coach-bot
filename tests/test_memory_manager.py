from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

import memory_manager
from memory_manager import (
    CACHE_TTL_SECONDS,
    MAX_MEMORY_CHARS,
    _detect_weekly_plan,
    _detect_workout_report,
    _get_cached_search,
    _resolve_workout_date,
    _truncate_memory,
    resolve_temporal_references,
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


class TestResolveTemporalReferences:
    def test_no_temporal_words_unchanged(self):
        result = resolve_temporal_references("weekly mileage plan")
        assert result == "weekly mileage plan"

    def test_today_appends_date(self):
        result = resolve_temporal_references("what's my workout today?")
        assert "[Date:" in result
        assert "what's my workout today?" in result

    def test_yesterday_appends_previous_date(self):
        result = resolve_temporal_references("here's the workout from yesterday")
        yesterday = date.today() - timedelta(days=1)
        assert yesterday.strftime("%A") in result

    def test_this_morning_appends_today(self):
        result = resolve_temporal_references("ran 5 miles this morning")
        assert "[Date:" in result

    def test_this_week_appends_date_range(self):
        result = resolve_temporal_references("what's the plan for this week?")
        assert "[Week:" in result

    def test_last_week_appends_previous_range(self):
        result = resolve_temporal_references("how did last week go?")
        assert "[Week:" in result

    def test_tomorrow_appends_next_date(self):
        result = resolve_temporal_references("what's tomorrow's workout?")
        tomorrow = date.today() + timedelta(days=1)
        assert tomorrow.strftime("%A") in result

    def test_day_name_past_intent_for_reports(self):
        """'How was my Monday?' should resolve to the closest past Monday."""
        result = resolve_temporal_references("how was my Monday run?")
        assert "[Monday:" in result

    def test_day_name_future_intent_for_planning(self):
        """'What's the plan for Saturday?' should resolve to closest future Saturday."""
        result = resolve_temporal_references("what's the plan for Saturday?")
        assert "[Saturday:" in result

    def test_day_name_report_style(self):
        """'My Tuesday tempo was tough' should resolve to past Tuesday."""
        result = resolve_temporal_references("my Tuesday tempo was tough")
        assert "[Tuesday:" in result


class TestResolveWorkoutDate:
    def test_yesterday_returns_previous_date(self):
        result = _resolve_workout_date("did my run yesterday")
        expected = (date.today() - timedelta(days=1)).isoformat()
        assert result == expected

    def test_this_morning_returns_today(self):
        result = _resolve_workout_date("ran 5 miles this morning")
        assert result == date.today().isoformat()

    def test_no_temporal_defaults_to_today(self):
        result = _resolve_workout_date("ran 8 miles with 3 at MP")
        assert result == date.today().isoformat()

    def test_day_name_resolves_to_past(self):
        """'My Saturday long run was 16 miles' should resolve Saturday to past."""
        result = _resolve_workout_date("my Saturday long run was 16 miles")
        resolved = date.fromisoformat(result)
        assert resolved.strftime("%A") == "Saturday"
        assert resolved <= date.today()


class TestDetectWorkoutReport:
    def test_workout_with_miles_and_ran(self):
        assert _detect_workout_report("I ran 5 miles today") is True

    def test_workout_with_tempo_and_pace(self):
        assert _detect_workout_report("did a tempo workout, 7:00 pace") is True

    def test_not_a_workout(self):
        assert _detect_workout_report("what's the plan for this week?") is False

    def test_single_keyword_not_enough(self):
        assert _detect_workout_report("I ran to the store") is False


class TestDetectWeeklyPlan:
    SAMPLE_PLAN = """# Week 12 (Mar 23-29)
**Target: 40-44 miles**
| Day | Workout |
|-----|---------|
| Mon 3/23 | 5mi easy |
| Tue 3/24 | 8mi with 3mi @ MP |
| Wed 3/25 | Rest or cross-train |
| Thu 3/26 | 6mi easy |
| Fri 3/27 | Rest |
| Sat 3/28 | 18mi long (last 2-3mi @ MP if feeling good) |
| Sun 3/29 | Rest |"""

    def test_detects_markdown_table_plan(self):
        assert _detect_weekly_plan(self.SAMPLE_PLAN) is True

    def test_rejects_non_plan_text(self):
        assert _detect_weekly_plan("Great run today! 5 miles easy.") is False

    def test_rejects_text_without_table(self):
        assert _detect_weekly_plan("This week we'll run Mon, Tue, Wed, Thu, Fri") is False


class TestStoreWeeklyPlan:
    @pytest.fixture(autouse=True)
    def mock_mem0(self, monkeypatch):
        self.mock_client = MagicMock()
        monkeypatch.setattr(memory_manager, "mem0_client", self.mock_client)
        memory_manager._query_cache = {}
        memory_manager._cache_timestamps = {}

    def test_stores_with_correct_metadata(self):
        from memory_manager import store_weekly_plan

        week_start = date(2026, 3, 23)
        store_weekly_plan("test plan", week_start=week_start)

        call_args = self.mock_client.add.call_args
        metadata = call_args.kwargs.get("metadata") or call_args[1].get("metadata")
        assert metadata["type"] == "weekly_plan"
        assert metadata["week_start"] == "2026-03-23"
        assert metadata["week_end"] == "2026-03-29"
        assert "created_at" in metadata  # For version tracking

    def test_prepends_structured_header(self):
        from memory_manager import store_weekly_plan

        week_start = date(2026, 3, 23)
        store_weekly_plan("| Mon | easy |", week_start=week_start)

        call_args = self.mock_client.add.call_args
        messages = call_args[0][0]
        assert "WEEKLY TRAINING PLAN" in messages[0]["content"]
        assert "March 23" in messages[0]["content"]


class TestRetrieveWeeklyPlan:
    @pytest.fixture(autouse=True)
    def mock_mem0(self, monkeypatch):
        self.mock_client = MagicMock()
        monkeypatch.setattr(memory_manager, "mem0_client", self.mock_client)
        memory_manager._query_cache = {}
        memory_manager._cache_timestamps = {}

    def test_returns_plan_from_filtered_search(self):
        from memory_manager import retrieve_weekly_plan

        self.mock_client.search.return_value = [
            {"memory": "Week 12 plan: Mon easy, Tue tempo", "metadata": {"type": "weekly_plan"}}
        ]
        result = retrieve_weekly_plan()
        assert "Week 12" in result

    def test_returns_empty_when_no_plan(self):
        from memory_manager import retrieve_weekly_plan

        self.mock_client.search.return_value = []
        result = retrieve_weekly_plan()
        assert result == ""

    def test_fallback_on_filter_failure(self):
        from memory_manager import retrieve_weekly_plan

        # First call (filtered) raises, fallback search returns result
        self.mock_client.search.side_effect = [
            Exception("filters not supported"),
            [{"memory": "fallback plan", "metadata": {}}],
        ]
        result = retrieve_weekly_plan()
        assert "fallback plan" in result

    def test_returns_latest_version_on_mid_week_update(self):
        """If plan is updated mid-week, retrieve_weekly_plan should return the newer one."""
        from memory_manager import retrieve_weekly_plan

        self.mock_client.search.return_value = [
            {
                "memory": "Old plan: Mon easy, Tue tempo",
                "metadata": {"type": "weekly_plan", "created_at": "2026-03-23T08:00:00"},
            },
            {
                "memory": "Updated plan: Mon easy, Tue intervals",
                "metadata": {"type": "weekly_plan", "created_at": "2026-03-25T10:00:00"},
            },
        ]
        result = retrieve_weekly_plan()
        assert "Updated plan" in result
        assert "intervals" in result
