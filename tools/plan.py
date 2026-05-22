"""Plan / temporal context tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from temporal_context import today_local

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
                "Get the prescribed workout(s) for a date. Returns "
                "{date, day_name, found, total_slots, sessions: [...]}. "
                "On multi-session days (two-a-days, three-a-days) the "
                "`sessions` list has one entry per slot, each with its own "
                "workout/pace_target/notes/status/slot/slot_label/is_rest_day. "
                "`slot_label` is 'AM'/'PM' for 2 sessions and 'k/N' for 3+. "
                "Always iterate `sessions` rather than assuming one — render "
                "every slot the user has planned."
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
    {
        "type": "function",
        "function": {
            "name": "get_week_status",
            "description": (
                "Same as get_week_plan but each row is joined with whether "
                "the day's log shows the prescription completed. Use this "
                "when summarizing the week's progress so you can render a "
                "check (✅) for completed days, an hourglass (⏳) for today "
                "or future days, and a cross (❌) for past days that were "
                "missed. A day is 'completed' when a logged session type "
                "matches the prescription kind (run / cross_train / "
                "strength). Off-plan logs are returned in `off_plan_actuals` "
                "so you can mention them without claiming the prescription "
                "was met."
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
    today = today_local()
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
    target = _parse_date(args.get("date")) or today_local()
    return _day_summary(state, target)


def _get_week_plan(args: dict, state) -> dict:
    offset = int(args.get("week_offset", 0))
    today = today_local()
    monday = today - timedelta(days=today.weekday())
    target_monday = monday + timedelta(weeks=offset)
    return {
        "week_start": target_monday.isoformat(),
        "week_offset": offset,
        "days": [_day_summary(state, target_monday + timedelta(days=i)) for i in range(7)],
    }


def _day_summary(state, target_date) -> dict:
    """Day view used by both get_todays_workout and get_week_plan.

    Hybrid shape: day-level legacy fields populated from the primary slot
    (so existing prompts that reference ``workout`` / ``pace_target`` keep
    working) plus ``sessions[]`` listing every slot for multi-session days.
    """
    sessions = state.get_todays_workouts(target_date)
    primary = sessions[0] if sessions else {}
    return {
        "date": target_date.isoformat(),
        "day_name": target_date.strftime("%A"),
        "workout": primary.get("workout", ""),
        "pace_target": primary.get("pace_target", ""),
        "notes": primary.get("notes", ""),
        "detail_md": primary.get("detail_md", ""),
        "status": primary.get("status"),
        "is_rest_day": primary.get("is_rest_day", False),
        "found": bool(sessions),
        "slot": primary.get("slot"),
        "slot_label": primary.get("slot_label", ""),
        "total_slots": len(sessions),
        "sessions": sessions,
    }


def _get_week_status(args: dict, state) -> dict:
    """Join the week's prescriptions with completion data.

    Hybrid shape so day-level summaries (rendered by the agent for week
    overviews) keep their legacy fields AND multi-session days expose every
    slot's status individually:

    Per day:
      - Legacy day-level fields (workout, pace_target, notes,
        prescription_kind, completed, actuals, off_plan_actuals) describe
        the day's PRIMARY slot. Type-strict completion check via
        `_log_matches_prescription` so a wrong-type Strava upload that
        reconcile loose-matched to a single-planned row is still counted
        as off-plan here.
      - `sessions[]` holds one entry per slot for multi-session days,
        each with its own `slot`, `slot_label`, `prescription_kind`,
        `completed` flags. Single-session days have a 1-element list.
    """
    from google_calendar.sync import (
        _log_matches_prescription,
        _prescription_kind,
    )
    from state_manager import _workout_dict_from_row

    offset = int(args.get("week_offset", 0))
    today = today_local()
    monday = today - timedelta(days=today.weekday())
    target_monday = monday + timedelta(weeks=offset)

    days_out: list[dict] = []
    for i in range(7):
        d = target_monday + timedelta(days=i)
        prescription_rows = state.get_workout_rows(d)
        n = len(prescription_rows)
        sessions_for_day: list[dict] = []
        for r in prescription_rows:
            view = _workout_dict_from_row(r, total_slots=n, date_override=d)
            kind = _prescription_kind(r.get("prescribed_workout") or "")
            view["prescription_kind"] = kind
            # Per-slot completion: row status AND the logged type must
            # actually satisfy the prescription kind. Reconcile's loose
            # single-row fallback can mark a wrong-type strength activity
            # as completed against an easy slot — guard against that.
            data = _row_json(r)
            log_type = str(data.get("type") or "") if data else ""
            view["completed"] = (
                r.get("status") == "completed" and bool(kind) and _log_matches_prescription(kind, log_type)
            )
            view["actual"] = _summarize_session(data) if data and view["completed"] else None
            sessions_for_day.append(view)

        # All sessions (any status) on the date, including off-plan rows
        # and wrong-type matches against single-slot prescriptions.
        all_logged = state.sessions_on_date(d)
        # Day-level legacy fields: primary slot view + aggregated actuals.
        primary = sessions_for_day[0] if sessions_for_day else {}
        primary_kind = primary.get("prescription_kind")
        actuals: list[dict] = []
        off_plan_actuals: list[dict] = []
        for s in all_logged:
            t = str(s.get("type") or "")
            if primary_kind and _log_matches_prescription(primary_kind, t):
                actuals.append(_summarize_session(s))
            else:
                off_plan_actuals.append(_summarize_session(s))

        days_out.append(
            {
                "date": d.isoformat(),
                "day_name": d.strftime("%A"),
                "workout": primary.get("workout", ""),
                "pace_target": primary.get("pace_target", ""),
                "notes": primary.get("notes", ""),
                "detail_md": primary.get("detail_md", ""),
                "status": primary.get("status"),
                "is_rest_day": primary.get("is_rest_day", False),
                "found": bool(prescription_rows),
                "slot": primary.get("slot"),
                "slot_label": primary.get("slot_label", ""),
                "total_slots": n,
                "sessions": sessions_for_day,
                "prescription_kind": primary_kind,
                "completed": bool(prescription_rows) and all(s["completed"] for s in sessions_for_day),
                "is_past": d < today,
                "is_today": d == today,
                "actuals": actuals,
                "off_plan_actuals": off_plan_actuals,
            }
        )

    return {
        "week_start": target_monday.isoformat(),
        "week_offset": offset,
        "today": today.isoformat(),
        "days": days_out,
    }


def _row_json(row: dict) -> dict:
    """Best-effort parse of a sessions row's ``data`` JSON to a dict."""
    raw = row.get("data")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    import json

    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _summarize_session(entry: dict) -> dict:
    """Trimmed log-entry summary for the agent — just the fields useful when
    rendering 'easy 8.1mi @ 8:42 HR 152' next to a prescription row."""
    return {
        "type": entry.get("type"),
        "miles": entry.get("miles"),
        "pace_avg": entry.get("pace_avg"),
        "hr_avg": entry.get("hr_avg"),
        "rpe": entry.get("rpe"),
        "notes": entry.get("notes"),
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
    "get_week_status": _get_week_status,
}
