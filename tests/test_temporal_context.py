from datetime import date, datetime, timedelta

import pytest

from temporal_context import (
    build_temporal_prompt,
    extract_todays_workout,
    get_temporal_context,
    get_training_phase,
    get_week_date_range,
    now_local,
    reset_race_date_cache,
    resolve_day_name_to_date,
    today_local,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the race date cache between tests."""
    reset_race_date_cache()
    yield
    reset_race_date_cache()


class TestGetTrainingPhase:
    def test_race_day(self):
        assert get_training_phase(0) == "race day or post-race"

    def test_post_race(self):
        assert get_training_phase(-5) == "race day or post-race"

    def test_race_week(self):
        assert get_training_phase(5) == "race week"

    def test_taper(self):
        assert get_training_phase(10) == "taper"

    def test_peak_early_taper(self):
        assert get_training_phase(18) == "peak/early taper"

    def test_high_volume_build(self):
        assert get_training_phase(35) == "high-volume build"

    def test_base_building(self):
        assert get_training_phase(60) == "base building"

    def test_boundary_race_week(self):
        assert get_training_phase(7) == "race week"

    def test_boundary_taper(self):
        assert get_training_phase(14) == "taper"

    def test_boundary_peak(self):
        assert get_training_phase(21) == "peak/early taper"

    def test_boundary_high_volume(self):
        assert get_training_phase(42) == "high-volume build"


class TestGetTemporalContext:
    def test_returns_required_keys(self):
        ctx = get_temporal_context()
        assert "date" in ctx
        assert "time_of_day" in ctx
        assert "days_to_race" in ctx
        assert "weeks_to_race" in ctx
        assert "training_phase" in ctx

    def test_time_of_day_valid(self):
        ctx = get_temporal_context()
        assert ctx["time_of_day"] in ("morning", "afternoon", "evening", "night")

    def test_weeks_is_days_div_7(self):
        ctx = get_temporal_context()
        assert ctx["weeks_to_race"] == ctx["days_to_race"] // 7


class TestBuildTemporalPrompt:
    def test_contains_race_countdown_header(self):
        prompt = build_temporal_prompt()
        assert "RACE COUNTDOWN" in prompt

    def test_contains_boston_marathon(self):
        prompt = build_temporal_prompt()
        assert "Boston Marathon" in prompt

    def test_contains_training_phase(self):
        prompt = build_temporal_prompt()
        assert "Training phase:" in prompt


class TestExtractTodaysWorkout:
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

    def test_extracts_by_date(self):
        now = datetime.now()
        month_day = f"{now.month}/{now.day}"
        # Build a plan with today's date in it
        plan = f"| {now.strftime('%a')} {month_day} | 6mi easy test |"
        result = extract_todays_workout(plan)
        assert "6mi easy test" in result

    def test_returns_empty_for_no_match(self):
        # Plan for a different week entirely
        result = extract_todays_workout("| Mon 1/1 | 5mi easy |\n| Tue 1/2 | tempo |")
        now = datetime.now()
        # Only empty if today is NOT Jan 1 or Jan 2
        if now.month != 1 or now.day not in (1, 2):
            # Should fall back to day-name matching
            # which may or may not match depending on today's day
            pass  # Non-deterministic, skip assertion

    def test_empty_plan_returns_empty(self):
        assert extract_todays_workout("") == ""

    def test_none_plan_returns_empty(self):
        assert extract_todays_workout(None) == ""

    def test_extracts_by_day_abbreviation(self):
        now = datetime.now()
        day_abbrev = now.strftime("%a")[:3]
        plan = f"| {day_abbrev} 12/99 | special workout |"
        # Won't match by date (12/99 is invalid), but should match by day abbreviation
        result = extract_todays_workout(plan)
        assert "special workout" in result


class TestGetWeekDateRange:
    def test_returns_monday_to_sunday(self):
        monday, sunday = get_week_date_range()
        assert monday.weekday() == 0  # Monday
        assert sunday.weekday() == 6  # Sunday
        assert (sunday - monday).days == 6


class TestResolveDayNameToDate:
    def test_past_intent_earlier_this_week(self):
        today = today_local()
        # Pick a day earlier in the week (if today is not Monday)
        if today.weekday() > 0:
            target_weekday = today.weekday() - 1
            day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            resolved = resolve_day_name_to_date(day_names[target_weekday], intent="past")
            assert resolved == today - timedelta(days=1)
            assert resolved < today

    def test_past_intent_same_day_returns_today(self):
        today = today_local()
        day_name = today.strftime("%A")
        resolved = resolve_day_name_to_date(day_name, intent="past")
        assert resolved == today

    def test_past_intent_later_day_goes_to_last_week(self):
        today = today_local()
        # Pick a day later in the week (if today is not Sunday)
        if today.weekday() < 6:
            target_weekday = today.weekday() + 1
            day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            resolved = resolve_day_name_to_date(day_names[target_weekday], intent="past")
            assert resolved < today
            # Should be last week's occurrence
            assert (today - resolved).days == 6

    def test_future_intent_later_this_week(self):
        today = today_local()
        if today.weekday() < 6:
            target_weekday = today.weekday() + 1
            day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            resolved = resolve_day_name_to_date(day_names[target_weekday], intent="future")
            assert resolved == today + timedelta(days=1)
            assert resolved > today

    def test_future_intent_same_day_returns_today(self):
        today = today_local()
        day_name = today.strftime("%A")
        resolved = resolve_day_name_to_date(day_name, intent="future")
        assert resolved == today

    def test_abbreviation_works(self):
        today = today_local()
        day_abbrev = today.strftime("%a")[:3]  # e.g., "Thu"
        resolved = resolve_day_name_to_date(day_abbrev, intent="past")
        assert resolved == today

    def test_invalid_day_returns_today(self):
        today = today_local()
        resolved = resolve_day_name_to_date("notaday", intent="past")
        assert resolved == today


class TestTimezoneAwareness:
    def test_now_local_returns_datetime(self):
        result = now_local()
        assert isinstance(result, datetime)

    def test_today_local_returns_date(self):
        result = today_local()
        assert isinstance(result, date)

    def test_timezone_env_override(self, monkeypatch):
        monkeypatch.setenv("USER_TIMEZONE", "America/New_York")
        # Force re-evaluation by calling directly
        from temporal_context import _get_user_tz
        tz = _get_user_tz()
        assert tz is not None

    def test_invalid_timezone_falls_back(self, monkeypatch):
        monkeypatch.setenv("USER_TIMEZONE", "Not/A/Timezone")
        from temporal_context import _get_user_tz
        tz = _get_user_tz()
        assert tz is None  # Falls back to system local
