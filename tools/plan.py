"""Plan / temporal context tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_today",
            "description": (
                "Return today's date, the next non-passed target race "
                "(name, date, days_to_race, goal_pace if set), and the current "
                "training phase. Call early in any turn that hinges on date."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todays_workout",
            "description": (
                "Get the prescribed workout for a date from plan.md. Returns "
                "{date, day_name, workout, pace_target, notes, is_rest_day, "
                "found}. Use this before answering any 'what's my workout' "
                "question — it parses the locked plan table directly rather "
                "than re-reading the plan blob."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD; defaults to today",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_week_plan",
            "description": (
                "Get the prescribed workouts for a week as a list of "
                "structured rows. week_offset 0 = current week (Mon-Sun), "
                "1 = next week, -1 = last week."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "week_offset": {
                        "type": "integer",
                        "description": "0=this week, 1=next week, -1=last week",
                        "default": 0,
                    }
                },
            },
        },
    },
]


def _get_today(args: dict, state) -> dict:
    today = date.today()
    athlete = state.load_athlete()
    races = athlete.get("target_races", []) or []
    next_race = _next_race(races, today)
    out = {
        "today": today.isoformat(),
        "day_name": today.strftime("%A"),
    }
    if next_race:
        race_date = _parse_date(next_race.get("date"))
        days = (race_date - today).days if race_date else None
        out["next_race"] = {
            "name": next_race.get("name"),
            "date": race_date.isoformat() if race_date else None,
            "days_to_race": days,
            "priority": next_race.get("priority"),
            "goal_time": next_race.get("goal_time"),
            "goal_pace": next_race.get("goal_pace"),
            "terrain": next_race.get("terrain"),
            "training_phase": _training_phase(days) if days is not None else None,
        }
    else:
        out["next_race"] = None
    return out


def _get_todays_workout(args: dict, state) -> dict:
    target = _parse_date(args.get("date")) or date.today()
    return state.get_todays_workout(target)


def _get_week_plan(args: dict, state) -> dict:
    offset = int(args.get("week_offset", 0))
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    target_monday = monday + timedelta(weeks=offset)
    rows = []
    for i in range(7):
        d = target_monday + timedelta(days=i)
        row = state.get_todays_workout(d)
        rows.append(row)
    return {
        "week_start": target_monday.isoformat(),
        "week_offset": offset,
        "days": rows,
    }


def _next_race(races: list[dict], today: date) -> Optional[dict]:
    upcoming = []
    for r in races:
        d = _parse_date(r.get("date"))
        if d is None or d < today:
            continue
        upcoming.append((d, r))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    return upcoming[0][1]


def _parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _training_phase(days: int) -> str:
    """Mirror of temporal_context.get_training_phase, kept here so tools/ is
    self-contained until temporal_context is refactored."""
    if days <= 0:
        return "race day or post-race"
    if days <= 7:
        return "race week"
    if days <= 14:
        return "taper"
    if days <= 21:
        return "peak/early taper"
    if days <= 56:
        return "build"
    return "base"


HANDLERS = {
    "get_today": _get_today,
    "get_todays_workout": _get_todays_workout,
    "get_week_plan": _get_week_plan,
}
