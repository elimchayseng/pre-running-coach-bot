"""Nightly readiness check-in: does tomorrow's plan still make sense?

Runs in the scheduler thread right after a successful COROS pull. One LLM
call, no tool loop — same shape as strava/review.py (whose JSON parser it
reuses). Inputs: tonight's vitals + 7-day trend, yesterday's completed
session(s) and reflections, tomorrow's prescription, the 4-week load arc.
Output: { "feedback": str, "plan_change": null | {summary, new_plan_md, reason} }.

Plan changes are NEVER applied here. They ride the existing
pending_proposal_store → next-chat-turn → user-confirms → update_plan flow,
and the review row persists with kind='readiness' so the resolve/expire/
Notion machinery works unchanged.

Quiet-night policy: no Telegram ping unless the model proposes a change or
flags a concern (COROS_CHECKIN_ALWAYS_PING=1 overrides — then every night
gets the one-line assessment).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Optional

from config import llm_client
from pending_proposal_store import get_pending_plan_proposal, set_pending_plan_proposal
from state_manager import StateManager
from strava.review import _call_review_llm, _parse_review_output
from temporal_context import today_local

logger = logging.getLogger("pre_coach.coros.review")

_TELEGRAM_MAX_CHARS = 3900


def _always_ping() -> bool:
    return (os.getenv("COROS_CHECKIN_ALWAYS_PING") or "").lower() in ("1", "true")


def _build_messages(state: StateManager) -> list[dict]:
    """Build the [system, user] message list for the readiness check-in."""
    today = today_local()
    tomorrow = today + timedelta(days=1)

    system = (
        "You are PRE, an elite endurance coach running a nightly readiness "
        "check: given tonight's wearable data, does TOMORROW's prescribed "
        "session still make sense?\n"
        "\n"
        "Voice: clinical, declarative, 2-4 sentences. Lead with the verdict. "
        "Reference specific numbers (sleep score, HRV vs baseline, load "
        "ratio). Drop filler.\n"
        "\n"
        "Output STRICT JSON only — no prose, no markdown fences. Schema:\n"
        "{\n"
        '  "feedback": string,\n'
        '  "concern": boolean (true when readiness data warrants attention '
        "even without a plan change),\n"
        '  "plan_change": null | {\n'
        '    "summary": string (1 sentence describing the shift),\n'
        '    "new_plan_md": string (FULL revised plan.md content, preserving '
        "the locked '| Day | Date | Workout | Pace target | Notes |' weekly "
        "table format),\n"
        '    "reason": string (short reason for plan_changelog.md)\n'
        "  }\n"
        "}\n"
        "\n"
        "Propose plan_change ONLY when readiness clearly contradicts "
        "tomorrow's prescription:\n"
        "- Poor sleep (score <60 or <6h) after a hard session today/yesterday "
        "with quality prescribed tomorrow → propose swapping to easy/rest.\n"
        "- All vitals at/above baseline, recovery high, load ratio in range, "
        "and only an easy day tomorrow → MAY propose upgrading to quality, "
        "but only if the weekly structure supports it.\n"
        "- Load ratio sustained above 1.5 ('Excessive') → bias toward "
        "reducing, not adding.\n"
        "Apply the 7-day-trend rule: a single-day HRV dip with decent sleep "
        "is NOT grounds for a change. Most nights the answer is "
        "plan_change: null and concern: false — silence is the default.\n"
        "Never modify rows for dates BEFORE tomorrow. Never remove the "
        "locked weekly table. Append a 'Recent Plan Adjustments' note "
        "describing the change if you propose one."
    )

    user = json.dumps(
        {
            "today": today.isoformat(),
            "tomorrow": tomorrow.isoformat(),
            "readiness_last_7_days": state.get_daily_health(days=7, today=today),
            "load_trend_4_weeks": state.get_load_trend(weeks=4, today=today),
            "todays_sessions": state.sessions_on_date(today),
            "yesterdays_sessions": state.sessions_on_date(today - timedelta(days=1)),
            "tomorrows_prescription": state.get_todays_workouts(tomorrow),
            "athlete": state.load_athlete(),
            "current_plan_md": state.render_plan(today=today),
        },
        ensure_ascii=False,
        default=str,
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _format_user_message(parsed: dict) -> str:
    """Render the parsed check-in into a Telegram message."""
    lines = ["Nightly readiness check:", "", parsed["feedback"].strip()]
    plan_change = parsed.get("plan_change")
    if plan_change:
        lines.append("")
        lines.append(f"Proposed plan change: {plan_change['summary'].strip()}")
        lines.append("Reply 'yes' to apply, or tell me what to adjust.")
    text = "\n".join(lines)
    if len(text) > _TELEGRAM_MAX_CHARS:
        text = text[: _TELEGRAM_MAX_CHARS - 1] + "…"
    return text


def run_readiness_review(state: StateManager) -> Optional[str]:
    """Generate the nightly readiness check-in. Returns the Telegram text to
    send, or None when there's nothing worth saying (quiet night) or on any
    failure — the nightly pull itself already succeeded either way.

    Side effects mirror run_post_activity_review:
      - review persisted with kind='readiness' (status NULL = Pending);
      - a proposed plan_change stashed in pending_proposal_store with a
        review_id backlink so applying it auto-resolves this review.
    """
    if llm_client is None:
        logger.warning("llm_client not initialized; skipping readiness review")
        return None
    today = today_local()
    if not state.get_daily_health(days=2, today=today):
        logger.info("No recent daily_health rows; skipping readiness review")
        return None
    try:
        messages = _build_messages(state)
        raw = _call_review_llm(messages)
        parsed = _parse_review_output(raw)
        if parsed is None:
            return None

        plan_change = parsed.get("plan_change")
        # Single-proposal-key collision guard: a pending post-activity (or
        # earlier readiness) proposal would be clobbered by stashing a new
        # one. Hold this change back — tomorrow night re-evaluates with
        # fresher data anyway.
        if plan_change and get_pending_plan_proposal():
            logger.info("Pending proposal already exists; withholding readiness plan_change")
            parsed["plan_change"] = None
            plan_change = None
            parsed["concern"] = True
            parsed["feedback"] = (
                parsed["feedback"].rstrip(".")
                + ". (A plan change is warranted but another proposal is already pending — resolve that first.)"
            )

        review_row = None
        try:
            review_row = state.save_review(
                session_id=None,
                strava_id=None,
                review_date=today,
                critique=parsed["feedback"],
                proposed_change=parsed.get("plan_change"),
                kind="readiness",
            )
        except Exception as e:
            logger.error(f"Failed to persist readiness review: {e}")

        if plan_change:
            try:
                set_pending_plan_proposal(
                    {
                        "summary": plan_change["summary"],
                        "new_plan_md": plan_change["new_plan_md"],
                        "reason": plan_change["reason"],
                        "source": "readiness",
                        "review_id": review_row["id"] if review_row else None,
                        "proposed_at": today.isoformat(),
                    }
                )
            except Exception as e:
                # Deliver the analysis anyway; the proposal just isn't applyable.
                logger.error(f"Failed to stash readiness proposal: {e}")
                parsed["plan_change"] = None

        if parsed.get("plan_change") or parsed.get("concern") or _always_ping():
            return _format_user_message(parsed)
        logger.info("Quiet night — readiness check-in stored, no ping")
        return None
    except Exception as e:
        logger.error(f"Readiness review failed: {e}", exc_info=True)
        return None
