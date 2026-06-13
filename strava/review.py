"""Post-activity review: analyze a run vs. plan, optionally propose plan changes.

Runs in the Strava webhook background thread after a run is logged. One LLM
call, no tool loop — the function has all data it needs inline. The model
returns strict JSON: { "feedback": str, "plan_change": null | {summary, new_plan_md, reason} }.

Plan changes are NEVER applied here. They get stashed in
pending_proposal_store; the next chat turn surfaces them in the system prompt
and the main agent applies via update_plan when the user confirms.

On any failure (LLM error, malformed output) the caller falls back to the
deterministic templated ping in strava/notify.py.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Optional

from openai import APIStatusError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import review_common
from config import llm_client
from pending_proposal_store import get_pending_plan_proposal, set_pending_plan_proposal
from state_manager import StateManager
from temporal_context import today_local

logger = logging.getLogger("pre_coach.strava.review")

RUN_TYPES = {"run", "easy", "long_run", "workout", "race", "strides", "return_test"}

# Shared review internals now live in review_common.py (issue #56). Keep the
# historical private aliases so this module's call sites and tests are
# unchanged.
_TELEGRAM_MAX_CHARS = review_common.TELEGRAM_MAX_CHARS
_MAX_NEW_PLAN_MD_CHARS = review_common.MAX_NEW_PLAN_MD_CHARS
_call_review_llm = review_common.call_review_llm
_parse_review_output = review_common.parse_review_output

# Activity-entry fields kept in the prompt. The full Strava blob has 30+
# fields and full lap/split arrays — we trim aggressively to keep the prompt
# focused and avoid blowing past cache. The agent doesn't need gear_id or
# kudos_count to grade a run.
_KEEP_ENTRY_KEYS = ("date", "type", "miles", "pace_avg", "hr_avg", "rpe", "notes")
_KEEP_DETAILS_KEYS = (
    "sport",
    "workout_type",
    "elevation_gain_ft",
    "moving_time",
    "elapsed_time",
    "hr_max",
    "suffer_score",
    "splits",
    "best_efforts",
)


def _trim_entry(entry: dict) -> dict:
    """Trim a log entry to the fields the review prompt actually uses.

    Laps are summarized down to name/distance/pace/hr_avg only, and capped
    at the first 12 laps (covers WU + reps + CD for typical workouts).
    Splits are kept whole — they're per-mile and bounded by distance.
    """
    out = {k: entry[k] for k in _KEEP_ENTRY_KEYS if k in entry}
    details = entry.get("details") or {}
    trimmed_details: dict = {}
    for k in _KEEP_DETAILS_KEYS:
        if k in details:
            trimmed_details[k] = details[k]
    laps = details.get("laps") or []
    if laps:
        trimmed_details["laps"] = [
            {
                "name": lap.get("name"),
                "distance_mi": lap.get("distance_mi"),
                "pace": lap.get("pace"),
                "hr_avg": lap.get("hr_avg"),
            }
            for lap in laps[:12]
        ]
    if trimmed_details:
        out["details"] = trimmed_details
    return out


def _build_messages(entry: dict, state: StateManager) -> list[dict]:
    """Build the [system, user] message list for the review call."""
    activity_date_str = entry.get("date")
    target_date = _safe_parse_date(activity_date_str) or today_local()
    # On multi-session days, surface every planned slot so the LLM sees the
    # full day's design — and flag which slot this activity closed via its
    # strava_id, so the review compares against the right prescription.
    planned_sessions = state.get_todays_workouts(target_date)
    sid = (entry.get("details") or {}).get("strava_id")
    matched_slot: Optional[str] = None
    if sid is not None:
        all_rows = state.get_session_rows_on_date(target_date) if hasattr(state, "get_session_rows_on_date") else []
        for r in all_rows:
            row_sid = None
            raw = r.get("data")
            if raw:
                try:
                    row_sid = (json.loads(raw) if isinstance(raw, str) else raw).get("details", {}).get("strava_id")
                except (TypeError, json.JSONDecodeError):
                    row_sid = None
            if row_sid == sid:
                matched_slot = r.get("slot")
                break
    recent = state.get_recent_sessions(days=14, today=target_date)
    # Drop the just-logged entry from "recent" so the model isn't confused.
    if sid is not None:
        recent = [r for r in recent if (r.get("details") or {}).get("strava_id") != sid]
    athlete = state.load_athlete()
    plan_md = state.render_plan(today=target_date)

    system = (
        "You are PRE, an elite endurance coach reviewing a run the athlete "
        "just completed. Compare what they ran to what was planned, then "
        "decide whether the rest of the week needs adjustment.\n"
        "\n"
        "Voice: clinical, declarative, 2-4 sentences. Lead with the answer. "
        "Reference specific numbers (pace vs target, HR, distance). Drop "
        "filler. Don't ask for RPE — that's collected separately.\n"
        "\n"
        "Output STRICT JSON only — no prose, no markdown fences. Schema:\n"
        "{\n"
        '  "feedback": string,\n'
        '  "plan_change": null | {\n'
        '    "summary": string (1 sentence describing the shift),\n'
        '    "new_plan_md": string (FULL revised plan.md content, preserving '
        "the locked '| Day | Date | Workout | Pace target | Notes |' weekly "
        "table format),\n"
        '    "reason": string (short reason for plan_changelog.md)\n'
        "  }\n"
        "}\n"
        "\n"
        "Propose plan_change ONLY when the activity meaningfully deviates "
        "from plan: distance off by >20%, average pace off by >30 sec/mi vs "
        "target, HR signals overreach for the prescription, or a clear "
        "missed/added session. Otherwise plan_change must be null.\n"
        "Never modify rows for dates BEFORE today. Never remove the locked "
        "weekly table. Append a 'Recent Plan Adjustments' note describing "
        "the change if you propose one."
    )

    user = json.dumps(
        {
            "today": today_local().isoformat(),
            "activity": _trim_entry(entry),
            "planned_for_activity_date": {
                "total_slots": len(planned_sessions),
                "sessions": planned_sessions,
                # Which slot the activity actually closed (None for single-session days).
                "matched_slot": matched_slot,
            },
            "recent_sessions_last_14_days": recent,
            "athlete": athlete,
            "current_plan_md": plan_md,
        },
        ensure_ascii=False,
        default=str,
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _safe_parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, RateLimitError, APIStatusError)),
)
def _format_user_message(parsed: dict, entry: dict) -> str:
    """Render the parsed review into a Telegram message.

    Always leads with a one-line "Logged: …" header (same data the templated
    ping shows) so the user still sees what was recorded, then the analysis,
    then the proposed shift if any.
    """
    miles = entry.get("miles")
    pace = entry.get("pace_avg")
    entry_type = entry.get("type", "run")
    header = f"Logged: {miles}mi" if miles else "Logged activity"
    if pace:
        header += f" @ {pace}"
    header += f" ({entry_type})"

    lines = [header, "", parsed["feedback"].strip()]
    plan_change = parsed.get("plan_change")
    if plan_change:
        lines.append("")
        lines.append(f"Proposed plan change: {plan_change['summary'].strip()}")
        lines.append("Reply 'yes' to apply, or tell me what to adjust.")
    text = "\n".join(lines)
    if len(text) > _TELEGRAM_MAX_CHARS:
        text = text[: _TELEGRAM_MAX_CHARS - 1] + "…"
    return text


def run_post_activity_review(entry: dict, state: StateManager, session_id: Optional[int] = None) -> Optional[str]:
    """Generate post-activity review text. Returns None on any failure so the
    caller can fall back to the deterministic templated ping.

    Side effects:
      - The review (critique + optional proposed plan_change) is persisted
        to the ``reviews`` SQLite table and mirrored to Notion via
        ``state.save_review``. The Notion page starts at Status=Pending.
      - If the model proposed a plan_change, the proposal is also written
        to pending_proposal_store (Redis, 24h TTL) so the next chat turn
        can surface and apply it on user confirmation.
    """
    if llm_client is None:
        logger.warning("llm_client not initialized; skipping review")
        return None
    try:
        messages = _build_messages(entry, state)
        raw = _call_review_llm(messages)
        parsed = _parse_review_output(raw)
        if parsed is None:
            return None
        plan_change = parsed.get("plan_change")
        # Single-proposal-key collision guard (mirrors coros/review.py): a
        # webhook-driven post-activity review can land at any hour and would
        # otherwise silently clobber a pending readiness proposal the user
        # was already pinged about — their "yes" would then apply the wrong
        # change. First proposal wins; this one is delivered as analysis only.
        if plan_change and get_pending_plan_proposal():
            logger.info("Pending proposal already exists; withholding post-activity plan_change")
            parsed["plan_change"] = None
            plan_change = None
            parsed["feedback"] = (
                parsed["feedback"].rstrip(".")
                + ". (A plan change is warranted but another proposal is already pending — resolve that first.)"
            )
        if plan_change:
            try:
                set_pending_plan_proposal(
                    {
                        "summary": plan_change["summary"],
                        "new_plan_md": plan_change["new_plan_md"],
                        "reason": plan_change["reason"],
                        "proposed_for_activity": (entry.get("details") or {}).get("strava_id"),
                        "proposed_at": today_local().isoformat(),
                    }
                )
            except Exception as e:
                # Don't fail the whole review just because Redis is down —
                # we can still deliver the analysis text. The proposal won't
                # be applyable, so strip it from the user message.
                logger.error(f"Failed to stash pending proposal: {e}")
                parsed["plan_change"] = None
        try:
            review_date = _safe_parse_date(entry.get("date")) or today_local()
            strava_id = (entry.get("details") or {}).get("strava_id")
            state.save_review(
                session_id=session_id,
                strava_id=strava_id,
                review_date=review_date,
                critique=parsed["feedback"],
                proposed_change=parsed.get("plan_change"),
            )
        except Exception as e:
            # Persisting the review is best-effort — never break the user
            # ping over a SQLite or Notion hiccup.
            logger.error(f"Failed to persist post-activity review: {e}")
        return _format_user_message(parsed, entry)
    except Exception as e:
        logger.error(f"Post-activity review failed: {e}", exc_info=True)
        return None


def is_run_type(entry: dict) -> bool:
    """Trigger gate: only run-type activities get the LLM review."""
    return entry.get("type") in RUN_TYPES
