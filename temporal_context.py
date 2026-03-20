from datetime import date, datetime

DEFAULT_RACE_DATE = date(2026, 4, 20)  # Boston Marathon


def get_temporal_context() -> dict:
    """Return current date/time and race countdown."""
    now = datetime.now()
    today = date.today()
    days_to_race = (DEFAULT_RACE_DATE - today).days

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
    return f"""=== TEMPORAL CONTEXT ===
Today: {ctx["date"]} ({ctx["time_of_day"]})
Race: Boston Marathon - {ctx["days_to_race"]} days away ({ctx["weeks_to_race"]} weeks)
Training phase: {ctx["training_phase"]}

Adapt your coaching to this timing context."""
