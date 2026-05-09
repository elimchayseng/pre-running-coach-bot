"""Tests for strava.translator.activity_to_log_entry.

Validates that the lap/split data the agent needs for workout verification
makes it into the entry shape state_manager.append_session expects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strava.translator import (
    _format_duration,
    _meters_to_feet,
    _meters_to_miles,
    _speed_to_pace,
    activity_to_log_entry,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------- helpers ----------


class TestHelpers:
    def test_meters_to_miles(self):
        assert _meters_to_miles(1609.344) == 1.0
        assert _meters_to_miles(8047.0) == 5.0
        assert _meters_to_miles(None) is None

    def test_meters_to_feet(self):
        assert _meters_to_feet(100.0) == 328
        assert _meters_to_feet(None) is None

    def test_speed_to_pace(self):
        # 3.0 m/s ≈ 8:56/mi
        assert _speed_to_pace(3.0) == "8:56"
        # 4.5 m/s ≈ 5:58/mi
        assert _speed_to_pace(4.5) == "5:58"

    def test_speed_to_pace_zero_or_none(self):
        assert _speed_to_pace(0.0) is None
        assert _speed_to_pace(None) is None

    def test_format_duration(self):
        assert _format_duration(45) == "0m 45s"
        assert _format_duration(125) == "2m 5s"
        assert _format_duration(3725) == "1h 2m 5s"
        assert _format_duration(None) is None


# ---------- type mapping ----------


class TestTypeMapping:
    def test_workout_type_3_is_workout(self):
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        assert out["type"] == "workout"

    def test_easy_run_with_low_hr_classified_easy(self):
        out = activity_to_log_entry(_load("strava_activity_easy.json"), hr_zones={"easy_ceiling": 155})
        assert out["type"] == "easy"

    def test_easy_run_no_hr_zones_classifies_run(self):
        out = activity_to_log_entry(_load("strava_activity_easy.json"))
        assert out["type"] == "run"

    def test_run_above_easy_ceiling_is_run(self):
        activity = _load("strava_activity_easy.json")
        activity["average_heartrate"] = 170.0
        out = activity_to_log_entry(activity, hr_zones={"easy_ceiling": 155})
        assert out["type"] == "run"

    def test_ride_is_cross_train(self):
        activity = {
            "id": 1,
            "type": "Ride",
            "distance": 32000,
            "average_speed": 8.0,
            "start_date_local": "2026-05-08T17:00:00Z",
            "name": "Evening ride",
        }
        out = activity_to_log_entry(activity)
        assert out["type"] == "cross_train"

    def test_workout_type_2_is_long_run(self):
        activity = _load("strava_activity_easy.json")
        activity["workout_type"] = 2
        out = activity_to_log_entry(activity)
        assert out["type"] == "long_run"

    def test_workout_type_1_is_race(self):
        activity = _load("strava_activity_workout.json")
        activity["workout_type"] = 1
        out = activity_to_log_entry(activity)
        assert out["type"] == "race"


# ---------- top-level fields ----------


class TestTopLevelFields:
    def test_workout_entry_shape(self):
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        assert out["date"] == "2026-05-12"
        assert out["miles"] == 5.0
        assert out["pace_avg"]  # M:SS string
        assert out["hr_avg"] == 162
        assert "Brooklyn" in out["notes"]
        assert "details" in out

    def test_strava_id_in_details(self):
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        assert out["details"]["strava_id"] == 12345678901

    def test_missing_id_raises(self):
        with pytest.raises(ValueError):
            activity_to_log_entry({"type": "Run", "distance": 5000})

    def test_notes_combines_name_and_description(self):
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        assert "3x1km @ HM goal" in out["notes"]
        assert "Felt strong" in out["notes"]

    def test_no_description_just_name(self):
        out = activity_to_log_entry(_load("strava_activity_easy.json"))
        assert out["notes"] == "Easy 4mi"


# ---------- the killer feature: lap data preservation ----------


class TestWorkoutVerificationData:
    def test_laps_preserved_with_per_lap_pace_and_hr(self):
        """For workout verification, the agent needs to see each rep's pace + HR."""
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        laps = out["details"]["laps"]
        assert len(laps) == 7  # WU + 3 reps + 2 recoveries + CD

        # Rep 1 should be ~3:42/mi (4.505 m/s)
        rep1 = next(lap for lap in laps if lap["name"] == "Rep 1")
        assert rep1["pace"] == "5:57"
        assert rep1["hr_avg"] == 175
        assert rep1["distance_mi"] == 0.621

        # Recovery should be slower with lower HR
        rec1 = next(lap for lap in laps if lap["lap_index"] == 3)
        assert rec1["hr_avg"] == 142

    def test_splits_preserved(self):
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        splits = out["details"]["splits"]
        assert len(splits) == 5  # 5mi total
        assert splits[0]["unit"] == "mi"
        assert splits[0]["pace"]
        assert splits[2]["hr_avg"] == 175  # mile 3 was the work mile

    def test_best_efforts_preserved(self):
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        bests = out["details"]["best_efforts"]
        assert any(b["name"] == "1 mile" for b in bests)
        assert any(b["name"] == "5K" for b in bests)

    def test_easy_run_has_minimal_lap_data(self):
        """An easy run with no manual laps should still produce one lap entry."""
        out = activity_to_log_entry(_load("strava_activity_easy.json"))
        laps = out["details"].get("laps", [])
        assert len(laps) == 1


# ---------- shape compatibility with state_manager.append_session ----------


class TestStateCompatibility:
    def test_entry_passes_append_session_validation(self, tmp_path):
        """Entry must have 'date' (state_manager.append_session validates this)."""
        from state_manager import StateManager

        s = StateManager(tmp_path)
        out = activity_to_log_entry(_load("strava_activity_workout.json"))
        # Must not raise ValueError (which the validator does on missing date)
        s.append_session(out)

        recent = s.get_sessions_in_range(
            __import__("datetime").date(2026, 5, 12),
            __import__("datetime").date(2026, 5, 12),
        )
        assert len(recent) == 1
        assert recent[0]["details"]["strava_id"] == 12345678901
