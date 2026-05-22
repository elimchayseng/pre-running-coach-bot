"""Translate a Strava activity (full detail JSON) into a log.jsonl entry.

Output shape lines up with state_manager.append_session:
    top-level: date, type, miles, pace_avg, hr_avg, rpe, notes
    details: type-specific richness — including the lap/split data the
             agent needs to verify a workout against its prescription.

Deterministic — no LLM. Tested against fixture JSON.
"""

from __future__ import annotations

from typing import Any, Optional

METERS_PER_MILE = 1609.344
FEET_PER_METER = 3.28084


# ---------- type mapping ----------

# Strava workout_type for runs:
#   None or 0 -> default (easy/run)
#   1 -> race
#   2 -> long run
#   3 -> workout

_RUN_WORKOUT_TYPE = {
    1: "race",
    2: "long_run",
    3: "workout",
}


def _map_type(activity: dict, hr_zones: Optional[dict] = None) -> str:
    sport = activity.get("type") or activity.get("sport_type") or ""
    if sport == "Run" or sport == "TrailRun" or sport == "VirtualRun":
        wt = activity.get("workout_type")
        if wt in _RUN_WORKOUT_TYPE:
            return _RUN_WORKOUT_TYPE[wt]
        # No explicit workout type — classify easy vs run by HR if we can
        avg_hr = activity.get("average_heartrate")
        easy_ceiling = (hr_zones or {}).get("easy_ceiling")
        if avg_hr is not None and isinstance(easy_ceiling, (int, float)):
            return "easy" if avg_hr <= easy_ceiling else "run"
        return "run"
    if sport in {"Ride", "VirtualRide", "EBikeRide", "GravelRide", "MountainBikeRide"}:
        return "cross_train"
    if sport == "Swim":
        return "cross_train"
    if sport == "WeightTraining":
        return "strength"
    if sport == "Yoga":
        return "cross_train"
    if sport == "Walk" or sport == "Hike":
        return "cross_train"
    return "run"  # safe default


# ---------- unit / formatting helpers ----------


def _meters_to_miles(meters: Optional[float], precision: int = 2) -> Optional[float]:
    if meters is None:
        return None
    return round(meters / METERS_PER_MILE, precision)


def _meters_to_feet(meters: Optional[float]) -> Optional[int]:
    if meters is None:
        return None
    return round(meters * FEET_PER_METER)


def _speed_to_pace(meters_per_second: Optional[float]) -> Optional[str]:
    """Convert m/s to a M:SS pace string per mile, or None if speed is 0/None."""
    if not meters_per_second or meters_per_second <= 0:
        return None
    seconds_per_mile = METERS_PER_MILE / meters_per_second
    minutes = int(seconds_per_mile // 60)
    seconds = int(round(seconds_per_mile - minutes * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"


def _format_duration(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


# ---------- structure extraction ----------


def _extract_laps(activity: dict) -> list[dict]:
    """Per-lap summary. The PRIMARY signal for workout verification.

    Strava's `laps` array contains user-pressed laps (or auto-laps if the
    watch generates them). For an interval workout this is typically:
        WU lap, [work, recovery] x N, CD lap.
    """
    out = []
    for lap in activity.get("laps") or []:
        out.append(
            {
                "lap_index": lap.get("lap_index"),
                "name": lap.get("name"),
                "distance_mi": _meters_to_miles(lap.get("distance"), precision=3),
                "moving_time": _format_duration(lap.get("moving_time")),
                "elapsed_time": _format_duration(lap.get("elapsed_time")),
                "pace": _speed_to_pace(lap.get("average_speed")),
                "hr_avg": _round_or_none(lap.get("average_heartrate")),
                "hr_max": _round_or_none(lap.get("max_heartrate")),
                "cadence_avg": _round_or_none(lap.get("average_cadence")),
                "elevation_gain_ft": _meters_to_feet(lap.get("total_elevation_gain")),
            }
        )
    return out


def _extract_splits(activity: dict) -> list[dict]:
    """Per-mile splits if available, else per-km. Strava auto-generates these
    independent of laps."""
    splits = activity.get("splits_standard") or []
    if not splits:
        # Fall back to metric splits; flag the unit so the agent knows.
        for split in activity.get("splits_metric") or []:
            splits.append(split)
        unit = "km"
    else:
        unit = "mi"
    out = []
    for s in splits:
        out.append(
            {
                "split": s.get("split"),
                "distance_mi": _meters_to_miles(s.get("distance"), precision=3),
                "elapsed_time": _format_duration(s.get("elapsed_time")),
                "pace": _speed_to_pace(s.get("average_speed")),
                "hr_avg": _round_or_none(s.get("average_heartrate")),
                "elevation_diff_ft": _meters_to_feet(s.get("elevation_difference")),
                "unit": unit,
            }
        )
    return out


def _extract_best_efforts(activity: dict) -> list[dict]:
    """Strava-computed best efforts at standard distances within this run."""
    out = []
    for be in activity.get("best_efforts") or []:
        out.append(
            {
                "name": be.get("name"),  # "1 mile", "2 mile", "5K", etc.
                "distance_m": be.get("distance"),
                "moving_time": _format_duration(be.get("moving_time")),
                "elapsed_time": _format_duration(be.get("elapsed_time")),
                "pr_rank": be.get("pr_rank"),
            }
        )
    return out


def _round_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


# ---------- main entrypoint ----------


def activity_to_log_entry(activity: dict, hr_zones: Optional[dict] = None) -> dict:
    """Translate a Strava activity (full detail JSON) → log.jsonl entry.

    Args:
        activity: the dict returned from GET /api/v3/activities/{id}
        hr_zones: optional `athlete.yaml` hr_zones dict — used to classify
            unmarked runs as `easy` vs `run` based on average HR.

    Returns:
        A dict with shape suitable for `state.append_session(...)`.
    """
    if not isinstance(activity, dict):
        raise ValueError("activity must be a dict (full detail from /activities/{id})")

    activity_id = activity.get("id")
    if activity_id is None:
        raise ValueError("activity is missing 'id' (Strava activity id)")

    sport = activity.get("type") or activity.get("sport_type") or ""
    entry_type = _map_type(activity, hr_zones=hr_zones)

    miles = _meters_to_miles(activity.get("distance"))
    pace = _speed_to_pace(activity.get("average_speed"))
    hr_avg = _round_or_none(activity.get("average_heartrate"))
    rpe = activity.get("perceived_exertion")

    # Notes: name + (description if set, separated)
    name = (activity.get("name") or "").strip()
    desc = (activity.get("description") or "").strip()
    notes = name if not desc else (f"{name} — {desc}" if name else desc)

    details: dict = {
        "strava_id": int(activity_id),
        "sport": sport,
        "workout_type": activity.get("workout_type"),
        "elevation_gain_ft": _meters_to_feet(activity.get("total_elevation_gain")),
        "moving_time": _format_duration(activity.get("moving_time")),
        "elapsed_time": _format_duration(activity.get("elapsed_time")),
        "suffer_score": activity.get("suffer_score"),
        "kudos_count": activity.get("kudos_count"),
        "gear_id": activity.get("gear_id"),
    }

    laps = _extract_laps(activity)
    if laps:
        details["laps"] = laps
    splits = _extract_splits(activity)
    if splits:
        details["splits"] = splits
    bests = _extract_best_efforts(activity)
    if bests:
        details["best_efforts"] = bests

    if activity.get("hr_max") is not None or activity.get("max_heartrate") is not None:
        details["hr_max"] = _round_or_none(activity.get("max_heartrate"))

    # Power for rides
    if activity.get("average_watts") is not None:
        details["watts_avg"] = _round_or_none(activity.get("average_watts"))
        details["watts_max"] = _round_or_none(activity.get("max_watts"))

    # Drop None values so the entry is concise
    details = {k: v for k, v in details.items() if v is not None}

    # start_date_local is "2026-05-12T06:32:00Z"-style. Slice for date.
    start = activity.get("start_date_local") or activity.get("start_date") or ""
    date_str = start[:10] if start else ""
    # The full local timestamp drives slot routing on multi-session days
    # (state_manager._pick_planned_match reads the hour from this field).
    start_local = start if len(start) > 10 else ""

    entry: dict = {
        "date": date_str,
        "type": entry_type,
    }
    if start_local:
        entry["start_local"] = start_local
    if miles is not None:
        entry["miles"] = miles
    if pace is not None:
        entry["pace_avg"] = pace
    if hr_avg is not None:
        entry["hr_avg"] = hr_avg
    if rpe is not None:
        entry["rpe"] = rpe
    if notes:
        entry["notes"] = notes
    entry["details"] = details
    return entry
