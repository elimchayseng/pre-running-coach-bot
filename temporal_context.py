import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python 3.8 fallback

DEFAULT_RACE_DATE = date(2026, 4, 20)  # Boston Marathon (fallback)

# Cache the resolved race date to avoid repeated Mem0 lookups
_resolved_race_date: Optional[date] = None


def _get_user_tz() -> timezone:
    """Get user's timezone from USER_TIMEZONE env var, defaulting to system local time."""
    tz_name = os.getenv("USER_TIMEZONE")
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except (KeyError, Exception):
            pass
    return None  # Will use datetime.now() which uses system local time


def now_local() -> datetime:
    """Get current datetime in the user's configured timezone."""
    tz = _get_user_tz()
    if tz:
        return datetime.now(tz)
    return datetime.now()


def today_local() -> date:
    """Get today's date in the user's configured timezone."""
    return now_local().date()


def _parse_date_from_memory(text: str) -> Optional[date]:
    """Try to extract a date from a Mem0 memory string."""
    # Match ISO format (2026-04-20) or common formats
    iso_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1))
        except ValueError:
            pass
    return None


def get_race_date() -> date:
    """Get race date from env var, then Mem0, then fallback to default."""
    global _resolved_race_date
    if _resolved_race_date is not None:
        return _resolved_race_date

    # 1. Check env var
    env_date = os.getenv("RACE_DATE")
    if env_date:
        try:
            _resolved_race_date = date.fromisoformat(env_date)
            return _resolved_race_date
        except ValueError:
            pass

    # 2. Check Mem0 (lazy import to avoid circular dependency)
    try:
        from memory_manager import get_race_date as mem0_get_race_date

        mem_text = mem0_get_race_date()
        if mem_text:
            parsed = _parse_date_from_memory(mem_text)
            if parsed:
                _resolved_race_date = parsed
                return _resolved_race_date
    except Exception:
        pass  # Mem0 unavailable, use fallback

    # 3. Fallback
    _resolved_race_date = DEFAULT_RACE_DATE
    return _resolved_race_date


def reset_race_date_cache() -> None:
    """Reset cached race date (for testing or after /race set)."""
    global _resolved_race_date
    _resolved_race_date = None


def get_temporal_context() -> dict:
    """Return current date/time and race countdown (timezone-aware)."""
    now = now_local()
    today = now.date()
    race_date = get_race_date()
    days_to_race = (race_date - today).days

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
        "weeks_to_race": days_to_race // 7,
        "training_phase": get_training_phase(days_to_race),
    }


def get_training_phase(days: int) -> str:
    """Determine training phase based on days until race."""
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
    """Build temporal context string for system prompt injection."""
    ctx = get_temporal_context()
    race_date = get_race_date()

    if ctx["days_to_race"] <= 0:
        race_line = f"Last race: {race_date.strftime('%B %d, %Y')} (completed)"
    else:
        race_line = f"Race: Boston Marathon - {ctx['days_to_race']} days away ({ctx['weeks_to_race']} weeks)"

    return f"""=== RACE COUNTDOWN ===
{race_line}
Training phase: {ctx["training_phase"]}"""


def extract_todays_workout(plan_text: str) -> str:
    """Extract today's workout from a weekly plan text.

    Handles markdown table format like:
        | Mon 3/23 | 5mi easy |
        | Tue 3/24 | 8mi with 3mi @ MP |
    Also handles plain text lines like:
        Monday: 5mi easy
        Tuesday, Mar 24: 8mi with 3mi @ MP
    """
    if not plan_text:
        return ""

    now = now_local()
    day_name = now.strftime("%A")       # e.g., "Tuesday"
    day_abbrev = now.strftime("%a")[:3]  # e.g., "Tue"
    today_month_day = f"{now.month}/{now.day}"  # e.g., "3/24"

    # Try matching by exact date first (e.g., "3/24" or "Mar 24")
    for line in plan_text.split("\n"):
        stripped = line.strip().strip("|").strip()
        if not stripped or stripped.startswith("-"):
            continue
        if today_month_day in stripped:
            # Extract workout from table row: "| Mon 3/24 | 5mi easy |"
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                return parts[-1]  # Last column is the workout
            return stripped

    # Fallback: match by day abbreviation (Mon, Tue, etc.)
    for line in plan_text.split("\n"):
        stripped = line.strip().strip("|").strip()
        if not stripped or stripped.startswith("-"):
            continue
        lower = stripped.lower()
        if lower.startswith(day_abbrev.lower()) or lower.startswith(day_name.lower()):
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                return parts[-1]
            # Try colon-separated: "Tuesday: 5mi easy"
            if ":" in stripped:
                return stripped.split(":", 1)[1].strip()
            return stripped

    return ""


def get_week_date_range() -> tuple[date, date]:
    """Return (Monday, Sunday) of the current week (timezone-aware)."""
    today = today_local()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def resolve_day_name_to_date(day_name: str, intent: str = "past") -> date:
    """Resolve a day name (e.g., 'Monday') to the nearest matching date.

    Args:
        day_name: Full day name like "Monday" or abbreviation like "Mon"
        intent: "past" = closest past occurrence (for reports/reviews),
                "future" = closest future occurrence (for planning)

    Returns:
        The resolved date.
    """
    day_map = {
        "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    }
    target_weekday = day_map.get(day_name.lower().strip())
    if target_weekday is None:
        return today_local()

    today = today_local()
    current_weekday = today.weekday()
    diff = current_weekday - target_weekday

    if intent == "past":
        # Closest past: if diff > 0 it was earlier this week, if diff == 0 it's today,
        # if diff < 0 it was last week
        if diff > 0:
            return today - timedelta(days=diff)
        elif diff == 0:
            return today  # Same day = today
        else:
            return today - timedelta(days=(7 + diff))
    else:
        # Closest future: if diff < 0 it's later this week, if diff == 0 it's today,
        # if diff > 0 it's next week
        if diff < 0:
            return today + timedelta(days=abs(diff))
        elif diff == 0:
            return today
        else:
            return today + timedelta(days=(7 - diff))
