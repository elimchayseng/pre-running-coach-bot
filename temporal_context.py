"""Timezone-aware date helpers and race-countdown context.

Race date resolution order:
    1. RACE_DATE env var (testing override)
    2. State manager's athlete.yaml -> earliest non-passed target_races entry
    3. None (callers degrade gracefully — no Mem0 fallback, no hardcoded race)
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8 fallback

logger = logging.getLogger("pre_coach")

_resolved_race_info: Optional[dict] = None  # cached {name, date}


def _get_user_tz() -> Optional[timezone]:
    """Get user's timezone from USER_TIMEZONE env var; None = system local."""
    tz_name = os.getenv("USER_TIMEZONE")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (KeyError, Exception):
            pass
    return None


def now_local() -> datetime:
    tz = _get_user_tz()
    return datetime.now(tz) if tz else datetime.now()


def today_local() -> date:
    return now_local().date()


def _state_dir() -> Path:
    return Path(__file__).resolve().parent / "state"


def _next_race_from_state() -> Optional[dict]:
    """Read athlete.yaml and return the earliest non-passed target race."""
    try:
        from state_manager import StateManager

        athlete = StateManager(_state_dir()).load_athlete()
    except Exception as e:
        logger.warning(f"Failed to load athlete.yaml for race date: {e}")
        return None

    races = athlete.get("target_races", []) or []
    today = today_local()
    upcoming: list[tuple[date, dict]] = []
    for r in races:
        d = r.get("date") if isinstance(r, dict) else None
        race_date = _coerce_date(d)
        if race_date is None or race_date < today:
            continue
        upcoming.append((race_date, r))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    rd, r = upcoming[0]
    return {"name": r.get("name", "next race"), "date": rd}


def _coerce_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def get_next_race() -> Optional[dict]:
    """Return {name, date} for the next non-passed target race, or None."""
    global _resolved_race_info
    if _resolved_race_info is not None:
        return _resolved_race_info

    env_date = os.getenv("RACE_DATE")
    if env_date:
        try:
            d = date.fromisoformat(env_date)
            _resolved_race_info = {"name": "race (RACE_DATE)", "date": d}
            return _resolved_race_info
        except ValueError:
            pass

    info = _next_race_from_state()
    if info is not None:
        _resolved_race_info = info
    return info


def get_race_date() -> Optional[date]:
    """Return the next non-passed target race date, or None if none configured."""
    info = get_next_race()
    return info["date"] if info else None


def reset_race_date_cache() -> None:
    """Reset cached race info — call after athlete.yaml is updated mid-process."""
    global _resolved_race_info
    _resolved_race_info = None


def get_temporal_context() -> dict:
    """Return current date/time context. days_to_race / training_phase are
    None when no race is configured."""
    now = now_local()
    today = now.date()
    race_date = get_race_date()
    days_to_race = (race_date - today).days if race_date else None

    hour = now.hour
    if 5 <= hour < 12:
        time_of_day = "morning"
    elif 12 <= hour < 17:
        time_of_day = "afternoon"
    elif 17 <= hour < 21:
        time_of_day = "evening"
    else:
        time_of_day = "night"

    return {
        "date": now.strftime("%A, %B %d, %Y"),
        "time_of_day": time_of_day,
        "days_to_race": days_to_race,
        "weeks_to_race": days_to_race // 7 if days_to_race is not None else None,
        "training_phase": get_training_phase(days_to_race) if days_to_race is not None else None,
    }


def get_training_phase(days: int) -> str:
    if days <= 0:
        return "race day or post-race"
    if days <= 7:
        return "race week"
    if days <= 14:
        return "taper"
    if days <= 21:
        return "peak/early taper"
    if days <= 42:
        return "high-volume build"
    return "base building"


def build_temporal_prompt() -> str:
    """Build a short race-countdown block for the system prompt."""
    info = get_next_race()
    if info is None:
        return "=== RACE COUNTDOWN ===\nNo target race configured."

    today = today_local()
    days = (info["date"] - today).days
    name = info["name"]

    if days <= 0:
        line = f"Last race: {name} on {info['date'].strftime('%B %d, %Y')} (completed)"
    else:
        weeks = days // 7
        line = f"Race: {name} - {days} days away ({weeks} weeks)"
    phase = get_training_phase(days)
    return f"=== RACE COUNTDOWN ===\n{line}\nTraining phase: {phase}"


def get_week_date_range() -> tuple[date, date]:
    """Return (Monday, Sunday) of the current week (timezone-aware)."""
    today = today_local()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def resolve_day_name_to_date(day_name: str, intent: str = "past") -> date:
    """Resolve a day name to a date.

    intent='past' -> closest past or today; 'future' -> closest future or today.
    """
    day_map = {
        "monday": 0,
        "mon": 0,
        "tuesday": 1,
        "tue": 1,
        "wednesday": 2,
        "wed": 2,
        "thursday": 3,
        "thu": 3,
        "friday": 4,
        "fri": 4,
        "saturday": 5,
        "sat": 5,
        "sunday": 6,
        "sun": 6,
    }
    target = day_map.get(day_name.lower().strip())
    if target is None:
        return today_local()

    today = today_local()
    diff = today.weekday() - target
    if intent == "past":
        if diff > 0:
            return today - timedelta(days=diff)
        if diff == 0:
            return today
        return today - timedelta(days=(7 + diff))
    else:
        if diff < 0:
            return today + timedelta(days=abs(diff))
        if diff == 0:
            return today
        return today + timedelta(days=(7 - diff))
