import pytest

from temporal_context import build_temporal_prompt, get_temporal_context, get_training_phase, reset_race_date_cache


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
    def test_contains_temporal_context_header(self):
        prompt = build_temporal_prompt()
        assert "TEMPORAL CONTEXT" in prompt

    def test_contains_boston_marathon(self):
        prompt = build_temporal_prompt()
        assert "Boston Marathon" in prompt

    def test_contains_training_phase(self):
        prompt = build_temporal_prompt()
        assert "Training phase:" in prompt
