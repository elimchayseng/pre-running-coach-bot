from datetime import date, datetime, timedelta

import pytest

from temporal_context import (
    build_temporal_prompt,
    get_next_race,
    get_race_date,
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
    """Reset the cached race info between tests."""
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


class TestRaceDateResolution:
    def test_env_var_overrides_state(self, monkeypatch):
        monkeypatch.setenv("RACE_DATE", "2099-12-31")
        assert get_race_date() == date(2099, 12, 31)

    def test_env_var_invalid_falls_through(self, monkeypatch):
        monkeypatch.setenv("RACE_DATE", "not-a-date")
        # Should fall through to state lookup; either returns a date or None
        result = get_race_date()
        assert result is None or isinstance(result, date)

    def test_no_state_no_env_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RACE_DATE", raising=False)
        # Repoint state dir to an empty tmp_path so no athlete.yaml exists
        import temporal_context

        monkeypatch.setattr(temporal_context, "_state_dir", lambda: tmp_path)
        assert get_race_date() is None

    def test_state_provides_next_race(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RACE_DATE", raising=False)
        (tmp_path / "athlete.yaml").write_text(
            "target_races:\n  - name: Past Race\n    date: 2020-01-01\n  - name: Future Race\n    date: 2099-06-15\n"
        )
        import temporal_context

        monkeypatch.setattr(temporal_context, "_state_dir", lambda: tmp_path)
        info = get_next_race()
        assert info["name"] == "Future Race"
        assert info["date"] == date(2099, 6, 15)


class TestGetTemporalContext:
    def test_returns_required_keys(self):
        ctx = get_temporal_context()
        for key in ("date", "time_of_day", "days_to_race", "weeks_to_race", "training_phase"):
            assert key in ctx

    def test_time_of_day_valid(self):
        ctx = get_temporal_context()
        assert ctx["time_of_day"] in ("morning", "afternoon", "evening", "night")

    def test_no_race_yields_none_fields(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RACE_DATE", raising=False)
        import temporal_context

        monkeypatch.setattr(temporal_context, "_state_dir", lambda: tmp_path)
        ctx = get_temporal_context()
        assert ctx["days_to_race"] is None
        assert ctx["weeks_to_race"] is None
        assert ctx["training_phase"] is None


class TestBuildTemporalPrompt:
    def test_no_race_message(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RACE_DATE", raising=False)
        import temporal_context

        monkeypatch.setattr(temporal_context, "_state_dir", lambda: tmp_path)
        prompt = build_temporal_prompt()
        assert "RACE COUNTDOWN" in prompt
        assert "No target race" in prompt

    def test_includes_race_name_from_state(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RACE_DATE", raising=False)
        (tmp_path / "athlete.yaml").write_text("target_races:\n  - name: Tahoe 50K\n    date: 2099-06-15\n")
        import temporal_context

        monkeypatch.setattr(temporal_context, "_state_dir", lambda: tmp_path)
        prompt = build_temporal_prompt()
        assert "Tahoe 50K" in prompt
        assert "Training phase:" in prompt


class TestGetWeekDateRange:
    def test_returns_monday_to_sunday(self):
        monday, sunday = get_week_date_range()
        assert monday.weekday() == 0
        assert sunday.weekday() == 6
        assert (sunday - monday).days == 6


class TestResolveDayNameToDate:
    def test_past_intent_earlier_this_week(self):
        today = today_local()
        if today.weekday() > 0:
            target_weekday = today.weekday() - 1
            names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            resolved = resolve_day_name_to_date(names[target_weekday], intent="past")
            assert resolved == today - timedelta(days=1)

    def test_past_intent_same_day_returns_today(self):
        today = today_local()
        resolved = resolve_day_name_to_date(today.strftime("%A"), intent="past")
        assert resolved == today

    def test_past_intent_later_day_goes_to_last_week(self):
        today = today_local()
        if today.weekday() < 6:
            target_weekday = today.weekday() + 1
            names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            resolved = resolve_day_name_to_date(names[target_weekday], intent="past")
            assert (today - resolved).days == 6

    def test_future_intent_later_this_week(self):
        today = today_local()
        if today.weekday() < 6:
            target_weekday = today.weekday() + 1
            names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            resolved = resolve_day_name_to_date(names[target_weekday], intent="future")
            assert resolved == today + timedelta(days=1)

    def test_future_intent_same_day_returns_today(self):
        today = today_local()
        resolved = resolve_day_name_to_date(today.strftime("%A"), intent="future")
        assert resolved == today

    def test_abbreviation_works(self):
        today = today_local()
        resolved = resolve_day_name_to_date(today.strftime("%a")[:3], intent="past")
        assert resolved == today

    def test_invalid_day_returns_today(self):
        assert resolve_day_name_to_date("notaday", intent="past") == today_local()


class TestTimezoneAwareness:
    def test_now_local_returns_datetime(self):
        assert isinstance(now_local(), datetime)

    def test_today_local_returns_date(self):
        assert isinstance(today_local(), date)

    def test_timezone_env_override(self, monkeypatch):
        monkeypatch.setenv("USER_TIMEZONE", "America/New_York")
        from temporal_context import _get_user_tz

        assert _get_user_tz() is not None

    def test_invalid_timezone_falls_back(self, monkeypatch):
        monkeypatch.setenv("USER_TIMEZONE", "Not/A/Timezone")
        from temporal_context import _get_user_tz

        assert _get_user_tz() is None
