import os
import re
from datetime import date, datetime
from typing import Optional

DEFAULT_RACE_DATE = date(2026, 4, 20)  # Boston Marathon (fallback)

# Cache the resolved race date to avoid repeated Mem0 lookups
_resolved_race_date: Optional[date] = None


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
    """Return current date/time and race countdown."""
    now = datetime.now()
    today = date.today()
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

    return f"""=== TEMPORAL CONTEXT ===
Today: {ctx["date"]} ({ctx["time_of_day"]})
{race_line}
Training phase: {ctx["training_phase"]}

Adapt your coaching to this timing context."""
