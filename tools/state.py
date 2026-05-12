"""State-mutation and state-read tools wrapping StateManager."""

from __future__ import annotations

from datetime import date

SESSION_TYPES = [
    "run",
    "easy",
    "long_run",
    "workout",
    "race",
    "strides",
    "return_test",
    "weekly_summary",
    "injury_event",
    "pt_diagnosis",
    "milestone",
    "cross_train",
    "strength",
]


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "log_session",
            "description": (
                "Append a training session to the log. Call this immediately "
                "when the user reports a run, workout, race, or notable event "
                "— don't wait to be asked. The 'details' object is for "
                "type-specific extras (splits, planned vs actual, weather, "
                "elevation, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD",
                    },
                    "type": {
                        "type": "string",
                        "enum": SESSION_TYPES,
                        "description": "Session category",
                    },
                    "miles": {"type": "number", "description": "Distance in miles"},
                    "pace_avg": {
                        "type": "string",
                        "description": "Average pace, e.g. '6:38'",
                    },
                    "hr_avg": {"type": "integer", "description": "Average HR (bpm)"},
                    "rpe": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Rate of perceived exertion 1-10",
                    },
                    "notes": {"type": "string"},
                    "details": {
                        "type": "object",
                        "description": "Type-specific extras (splits, planned, weather, elevation_gain_ft, etc.)",
                        "additionalProperties": True,
                    },
                },
                "required": ["date", "type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": (
                "Replace plan.md with new content. Use when adjusting today's "
                "workout, the week, or the broader block. CRITICAL: preserve "
                "the locked '| Day | Date | Workout | Pace target | Notes |' "
                "table format for the current week — get_todays_workout depends "
                "on it. Append a 'Recent Plan Adjustments' line in the new "
                "content noting the change and reason. "
                "For QUALITY sessions and races (workouts, long runs, races) in "
                "the current week, also include a per-day sub-section anchored "
                "by '#### YYYY-MM-DD' (the date on its own line as an H4 "
                "heading) with rationale, structure breakdown (WU / work / CD), "
                "and execution cues. The Google Calendar sync uses this prose "
                "verbatim as the event description, so write it the way you "
                "want the user to read it on their phone the morning of. The "
                "body extends until the next heading. Easy/recovery/rest days "
                "don't need a sub-section — they fall back to the table cells."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_plan_markdown": {
                        "type": "string",
                        "description": "Full new content of plan.md",
                    },
                    "change_reason": {
                        "type": "string",
                        "description": "Short reason logged to plan_changelog.md",
                    },
                },
                "required": ["new_plan_markdown", "change_reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_journal",
            "description": (
                "Add a freeform note to journal.md. Use for life context "
                "(sleep, stress, travel, illness, mood) and decision "
                "rationale that should persist beyond this conversation. "
                "Pass body text only — a timestamp header is added "
                "automatically. Do NOT include a date or section header in "
                "the entry text."
            ),
            "parameters": {
                "type": "object",
                "properties": {"entry": {"type": "string"}},
                "required": ["entry"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_athlete",
            "description": (
                "Patch fields in athlete.yaml. Use for new PR, resolved injury, "
                "updated zones, new race target. The 'updates' object is "
                "deep-merged into athlete.yaml — nested dicts merge, but lists "
                "REPLACE entirely (so to remove a resolved injury, pass the "
                "full new injury_history list). Comments and key order are "
                "preserved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "object",
                        "description": "Partial structure to merge into athlete.yaml",
                        "additionalProperties": True,
                    }
                },
                "required": ["updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sessions",
            "description": (
                "Get all logged sessions in a date range. Use for trend "
                "questions ('how does my mileage compare to last month') or to "
                "verify what was actually run before adjusting the plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
]


# ---------- handlers ----------


def _log_session(args: dict, state) -> dict:
    session = {k: v for k, v in args.items() if v is not None}
    state.append_session(session)
    return {"ok": True, "logged": session}


def _update_plan(args: dict, state) -> dict:
    state.update_plan(args["new_plan_markdown"], args["change_reason"])
    result = {"ok": True, "change_reason": args["change_reason"]}
    # Any pending post-activity proposal is now consumed (either applied
    # verbatim or superseded by a manual edit). Clear it so it doesn't
    # linger in the next system prompt.
    try:
        from pending_proposal_store import clear_pending_plan_proposal

        clear_pending_plan_proposal()
    except Exception:
        pass
    # After write, verify today's row is parseable. The locked
    # "| Day | Date | Workout | Pace target | Notes |" table format is what
    # /today depends on — if the agent's write broke it, surface a warning
    # the agent can act on (vs. silent failure when /today returns "no
    # workout prescribed").
    from temporal_context import today_local

    today_check = state.get_todays_workout(today_local())
    if not today_check["found"]:
        result["warning"] = (
            "Today's row not parseable from the new plan. The locked "
            "'| Day | Date | Workout | Pace target | Notes |' table format "
            "must be preserved for the current week — /today depends on it. "
            "Re-check the plan and call update_plan again if the table is "
            "missing or misformatted."
        )
    return result


def _append_journal(args: dict, state) -> dict:
    state.append_journal(args["entry"])
    return {"ok": True}


def _update_athlete(args: dict, state) -> dict:
    state.update_athlete(args["updates"])
    return {"ok": True, "updated_fields": list(args["updates"].keys())}


def _get_sessions(args: dict, state) -> dict:
    start = date.fromisoformat(args["start_date"])
    end = date.fromisoformat(args["end_date"])
    sessions = state.get_sessions_in_range(start, end)
    return {"sessions": sessions, "count": len(sessions)}


HANDLERS = {
    "log_session": _log_session,
    "update_plan": _update_plan,
    "append_journal": _append_journal,
    "update_athlete": _update_athlete,
    "get_sessions": _get_sessions,
}
