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

from config import HEROKU_MODEL, llm_client
from pending_proposal_store import set_pending_plan_proposal
from state_manager import StateManager
from temporal_context import today_local

logger = logging.getLogger("pre_coach.strava.review")

RUN_TYPES = {"run", "easy", "long_run", "workout", "race", "strides", "return_test"}

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
    planned = state.get_todays_workout(target_date)
    recent = state.get_recent_sessions(days=14, today=target_date)
    # Drop the just-logged entry from "recent" so the model isn't confused.
    sid = (entry.get("details") or {}).get("strava_id")
    if sid is not None:
        recent = [r for r in recent if (r.get("details") or {}).get("strava_id") != sid]
    athlete = state.load_athlete()
    plan_md = state.load_plan()

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
            "planned_for_activity_date": planned,
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
def _call_review_llm(messages: list[dict]) -> str:
    """Single LLM call, no tools, JSON-mode response. Returns raw text content."""
    response = llm_client.chat.completions.create(
        model=HEROKU_MODEL,
        messages=messages,
        max_tokens=4000,
    )
    if not response.choices:
        raise ValueError("LLM returned no response choices")
    return response.choices[0].message.content or ""


def _parse_review_output(raw: str) -> Optional[dict]:
    """Parse the model's JSON output. Returns None on malformed output.

    Tolerates the model wrapping its JSON in ```json fences despite
    instructions otherwise.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip a leading fence and any trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Review LLM returned malformed JSON: {e}; raw: {raw[:300]}")
        return None
    if not isinstance(data, dict) or "feedback" not in data:
        logger.error(f"Review LLM JSON missing required keys: {raw[:300]}")
        return None
    plan_change = data.get("plan_change")
    if plan_change is not None:
        if not isinstance(plan_change, dict):
            logger.error("plan_change present but not an object; dropping")
            data["plan_change"] = None
        else:
            required = ("summary", "new_plan_md", "reason")
            if not all(isinstance(plan_change.get(k), str) and plan_change.get(k) for k in required):
                logger.error("plan_change missing required string fields; dropping")
                data["plan_change"] = None
    return data


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
    return "\n".join(lines)


def run_post_activity_review(entry: dict, state: StateManager) -> Optional[str]:
    """Generate post-activity review text. Returns None on any failure so the
    caller can fall back to the deterministic templated ping.

    Side effects: if the model proposes a plan_change, the proposal is
    written to pending_proposal_store (Redis, 24h TTL) for the next chat
    turn to surface and apply on user confirmation.
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
        return _format_user_message(parsed, entry)
    except Exception as e:
        logger.error(f"Post-activity review failed: {e}", exc_info=True)
        return None


def is_run_type(entry: dict) -> bool:
    """Trigger gate: only run-type activities get the LLM review."""
    return entry.get("type") in RUN_TYPES
