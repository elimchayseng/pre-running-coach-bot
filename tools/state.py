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
            "name": "update_workout",
            "description": (
                "PREFERRED tool for single-day plan edits — adjusting today's "
                "or any specific day's workout, pace target, notes, and/or "
                "per-day detail prose. Patches the locked weekly table row "
                "for `date` in place; only the fields you pass change (others "
                "stay as-is). If `detail_body` is supplied, the corresponding "
                "'#### YYYY-MM-DD' section is created or its body replaced — "
                "this is the prose Google Calendar uses verbatim as the event "
                "description on the morning of the workout, so write it for "
                "the user. Quality sessions and races in the current week "
                "should always include a detail_body with rationale, "
                "structure (WU / work / CD), and execution cues; "
                "easy/recovery/rest days don't need one and fall back to the "
                "table cells. Both the row patch and the detail body land in "
                "a single atomic write with one changelog entry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD identifying the row to patch",
                    },
                    "workout": {
                        "type": "string",
                        "description": "New workout cell (e.g. 'Easy 5mi + 4 strides'). Omit to leave unchanged.",
                    },
                    "pace_target": {
                        "type": "string",
                        "description": "New pace target cell (e.g. '8:30-9:00'). Omit to leave unchanged.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "New notes cell. Omit to leave unchanged.",
                    },
                    "detail_body": {
                        "type": "string",
                        "description": (
                            "Per-day prose for the '#### YYYY-MM-DD' section "
                            "(no heading — just the body). Creates the section "
                            "if missing or replaces its body if present. Omit "
                            "for easy/recovery/rest days."
                        ),
                    },
                    "change_reason": {
                        "type": "string",
                        "description": "Short reason logged to plan_changelog.md",
                    },
                },
                "required": ["date", "change_reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_week_table",
            "description": (
                "Replace the entire locked weekly table at once. Use this for "
                "block / phase transitions when most rows in the week change "
                "together (e.g. taper week, recovery week, new training "
                "block). Header and separator lines are preserved; only the "
                "data rows change. Per-day '#### YYYY-MM-DD' detail sections "
                "elsewhere in the plan are NOT touched — call update_workout "
                "afterwards for any new quality sessions that need detail "
                "prose. Prefer update_workout for single-day edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "description": ("Ordered list of weekly rows. One per day to appear in the table."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "day": {"type": "string", "description": "Mon / Tue / ..."},
                                "date": {"type": "string", "description": "ISO YYYY-MM-DD"},
                                "workout": {"type": "string"},
                                "pace_target": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                            "required": ["day", "date", "workout", "pace_target", "notes"],
                        },
                    },
                    "change_reason": {
                        "type": "string",
                        "description": "Short reason logged to plan_changelog.md",
                    },
                },
                "required": ["rows", "change_reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": (
                "ESCAPE HATCH for full-plan rewrites. Prefer update_workout "
                "(single-day edits) or replace_week_table (block transitions) "
                "— they pass tiny tool-call args instead of the whole plan. "
                "Reserve update_plan for: applying a pending plan proposal "
                "verbatim, mid-block restructuring that touches many "
                "non-table sections, or initial plan creation. "
                "CRITICAL: preserve the locked "
                "'| Day | Date | Workout | Pace target | Notes |' "
                "table format — update_plan parses that table into the plan's "
                "workout rows, so a missing or misformatted table loses the "
                "week. For QUALITY sessions and races, include per-day "
                "'#### YYYY-MM-DD' sub-sections with rationale, structure "
                "(WU / work / CD), and execution cues. The body extends until "
                "the next heading."
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
    _mark_calendar_complete(state, session)
    return {"ok": True, "logged": session}


def _mark_calendar_complete(state, entry: dict) -> None:
    """Best-effort: reflect the day's logged sessions onto Google Calendar.
    Never raises — gcal is an optional integration and a hiccup here must not
    break the log_session tool call (the log entry was already persisted)."""
    import logging

    log_date = entry.get("date")
    if not log_date:
        return
    try:
        from google_calendar.sync import mark_complete

        mark_complete(state, log_date)
    except Exception as e:
        logging.getLogger("pre_coach.tools.state").warning(
            f"mark_complete failed for {log_date}: {type(e).__name__}: {e}"
        )


def _update_plan(args: dict, state) -> dict:
    state.update_plan(args["new_plan_markdown"], args["change_reason"])
    result = {"ok": True, "change_reason": args["change_reason"]}
    _auto_resolve_matching_review(state)
    _consume_pending_proposal()
    _attach_today_warning_if_broken(state, result)
    return result


def _update_workout(args: dict, state) -> dict:
    """Patch a single row + optional detail body. Issue #19 root-cause fix
    for plan-edit empty-choices: this tool's args stay small (no whole-plan
    re-emit) so the model produces output tokens before any upstream
    deadline fires."""
    target = date.fromisoformat(args["date"])
    state.update_workout(
        target_date=target,
        change_note=args["change_reason"],
        workout=args.get("workout"),
        pace_target=args.get("pace_target"),
        notes=args.get("notes"),
        detail_body=args.get("detail_body"),
    )
    result = {"ok": True, "date": args["date"], "change_reason": args["change_reason"]}
    _auto_resolve_matching_review(state)
    _attach_today_warning_if_broken(state, result)
    return result


def _replace_week_table(args: dict, state) -> dict:
    """Bulk replacement of the weekly table. For block / phase transitions
    when most rows change together. Detail sections are preserved."""
    state.replace_week_table(args["rows"], args["change_reason"])
    result = {
        "ok": True,
        "rows_written": len(args["rows"]),
        "change_reason": args["change_reason"],
    }
    _auto_resolve_matching_review(state)
    _attach_today_warning_if_broken(state, result)
    return result


def _consume_pending_proposal() -> None:
    """Any pending post-activity proposal is consumed when the plan is
    rewritten wholesale. Clear it so it doesn't linger in the next system
    prompt. Patch tools (update_workout / replace_week_table) intentionally
    do NOT clear — they're targeted edits, not proposal-apply."""
    try:
        from pending_proposal_store import clear_pending_plan_proposal

        clear_pending_plan_proposal()
    except Exception:
        pass


def _auto_resolve_matching_review(state) -> None:
    """Heuristic: a plan-edit tool just ran. If Redis has a pending proposal
    whose ``proposed_for_activity`` matches a recent Pending review (by
    ``strava_id``), flip that review to ``approved`` and mirror the flip
    to Notion.

    Matching rule: the proposal's ``proposed_for_activity`` is the Strava
    activity id the post-activity review was generated for. The reviews
    table also stores ``strava_id``, so a direct equality match is the
    cleanest signal that this plan write applies that proposal. No match,
    or no pending proposal in Redis → no-op (reviews stay Pending).

    On a successful flip, the matching Redis proposal is cleared too — the
    write just applied it, so leaving it in Redis would resurface the same
    proposal in the next system prompt. Clearing is best-effort: a Redis
    blip logs a warning but never re-raises (the SQLite flip is already
    committed and is the source of truth).

    The "rejected" path (user replies "don't apply") is intentionally NOT
    auto-detected here — it requires NL-intent parsing on the chat turn,
    which is the next iteration. TODO: detect explicit-rejection turns
    and flip the matching review to ``rejected`` from the chat handler.

    Failures swallow silently: this is a best-effort enrichment of the
    Notion view, never a blocker on the plan edit itself.
    """
    import logging

    try:
        from pending_proposal_store import get_pending_plan_proposal

        proposal = get_pending_plan_proposal()
    except Exception as e:
        logging.getLogger("pre_coach.tools.state").debug(f"auto-resolve: failed to read pending proposal: {e}")
        return
    if not proposal:
        return
    strava_id = proposal.get("proposed_for_activity")
    if strava_id is None:
        return
    try:
        review = state.find_pending_review_for_activity(strava_id=strava_id)
        if review is None:
            return
        resolved = state.resolve_pending_review(review["id"], "approved")
    except Exception as e:
        logging.getLogger("pre_coach.tools.state").warning(
            f"auto-resolve: failed to flip review for strava_id={strava_id}: {e}"
        )
        return
    if resolved is None:
        # Lost a race against another resolver — nothing to clear.
        return
    try:
        from pending_proposal_store import clear_pending_plan_proposal

        clear_pending_plan_proposal()
    except Exception as e:
        logging.getLogger("pre_coach.tools.state").warning(
            f"auto-resolve: review {review['id']} flipped to approved but Redis proposal clear failed: {e}"
        )


def _attach_today_warning_if_broken(state, result: dict) -> None:
    """After any plan write, verify today's row is still parseable. The
    locked '| Day | Date | Workout | Pace target | Notes |' table format
    is what get_todays_workout depends on — if a write broke it, surface
    a warning the agent can act on (vs. silent failure when /today returns
    'no workout prescribed')."""
    from temporal_context import today_local

    today_check = state.get_todays_workout(today_local())
    if not today_check["found"]:
        result["warning"] = (
            "Today's row not parseable from the updated plan. The locked "
            "'| Day | Date | Workout | Pace target | Notes |' table format "
            "must be preserved for the current week — /today depends on it. "
            "Re-check the plan and call the appropriate tool again if the "
            "table is missing or misformatted."
        )


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
    "update_workout": _update_workout,
    "replace_week_table": _replace_week_table,
    "append_journal": _append_journal,
    "update_athlete": _update_athlete,
    "get_sessions": _get_sessions,
}
