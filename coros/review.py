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
import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

from config import llm_client
from pending_proposal_store import get_pending_plan_proposal, set_pending_plan_proposal
from review_common import TELEGRAM_MAX_CHARS as _TELEGRAM_MAX_CHARS
from review_common import call_review_llm as _call_review_llm
from review_common import parse_review_output as _parse_review_output
from state_manager import StateManager
from temporal_context import today_local

logger = logging.getLogger("pre_coach.coros.review")

# The readiness prompt demands the FULL revised plan.md in new_plan_md, which
# can exceed the post-activity review's 4000-token default and truncate
# mid-JSON (silently dropping the whole check-in).
_MAX_TOKENS = 8000


def _always_ping() -> bool:
    return (os.getenv("COROS_CHECKIN_ALWAYS_PING") or "").lower() in ("1", "true")


def _clean_text(val: str) -> str:
    """COROS-derived free text (regex captures like load_comment and
    hrv_evaluation accept arbitrary line content) feeds the LLM that drafts
    plan changes and Telegram text — the same trust boundary
    state_manager.render_readiness_block already enforces with this exact
    charset/length clamp for the chat system prompt. Without it, a
    hijacked/garbled COROS payload gets ~one line of instructions per field
    per day into the plan-proposing prompt."""
    return re.sub(r"[^A-Za-z0-9 .%/:-]", "", val)[:40]


def _build_messages(state: StateManager, today: Optional[date] = None) -> list[dict]:
    """Build the [system, user] message list for the readiness check-in."""
    today = today or today_local()
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

    # Strip raw (the unparsed COROS tool-text bundle — kilobytes of
    # third-party-controlled free text) and fetched_at from the prompt rows:
    # the parsed columns carry the signal; raw would be a prompt-injection
    # surface feeding an LLM that drafts plan changes and Telegram text.
    # The parsed TEXT columns are regex captures of the same third-party
    # text, so they get the charset/length clamp too.
    readiness_rows = [
        {k: (_clean_text(v) if isinstance(v, str) else v) for k, v in row.items() if k not in ("raw", "fetched_at")}
        for row in state.get_daily_health(days=7, today=today)
    ]

    user = json.dumps(
        {
            "today": today.isoformat(),
            "tomorrow": tomorrow.isoformat(),
            "readiness_last_7_days": readiness_rows,
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


def run_readiness_review(state: StateManager, today: Optional[date] = None) -> Optional[str]:
    """Generate the nightly readiness check-in. Returns the Telegram text to
    send, or None when there's nothing worth saying (quiet night) or on any
    failure — the nightly pull itself already succeeded either way.

    ``today`` is the pass date the scheduler captured before the pull, so the
    review row, its once-per-night dedup, and the prompt window all agree on
    the same day even when the pass crosses local midnight. Defaults to
    today_local() for direct callers.

    Side effects mirror run_post_activity_review:
      - review persisted with kind='readiness' (status NULL = Pending);
      - a proposed plan_change stashed in pending_proposal_store with a
        review_id backlink so applying it auto-resolves this review.
    """
    if llm_client is None:
        logger.warning("llm_client not initialized; skipping readiness review")
        return None
    today = today or today_local()
    if not state.get_daily_health(days=2, today=today):
        logger.info("No recent daily_health rows; skipping readiness review")
        return None
    # Once per night, DB-enforced: a manual `python -m coros.scheduler` run,
    # a marker-store outage (the Redis due-check fails open by design), or a
    # crashed-and-restarted worker must not produce duplicate LLM reviews,
    # rows, and Telegram pings for the same date.
    if any(r.get("kind") == "readiness" for r in state.get_reviews_in_range(today, today)):
        logger.info("Readiness review for %s already exists; skipping", today.isoformat())
        return None
    try:
        messages = _build_messages(state, today=today)
        raw = _call_review_llm(messages, max_tokens=_MAX_TOKENS)
        parsed = _parse_review_output(raw)
        if parsed is None:
            return None

        # LLMs in JSON mode occasionally emit booleans as strings. The
        # string "false" is truthy: it would ping Telegram every night AND
        # skip the quiet-night no-op resolution, accumulating the perpetual
        # Pending rows that machinery exists to prevent. Normalize first.
        concern = parsed.get("concern")
        if isinstance(concern, str):
            concern = concern.strip().lower() in ("true", "yes", "1")
        parsed["concern"] = bool(concern)

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
        except sqlite3.IntegrityError:
            # The partial unique index (one readiness row per date) says
            # another process wrote tonight's review between our dedup
            # SELECT and this INSERT. That process owns the ping and the
            # proposal — stand down entirely.
            logger.info("Readiness review for %s inserted concurrently; standing down", today.isoformat())
            return None
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
                # Deliver the analysis anyway; the proposal just isn't
                # applyable. Force concern so the delivery gate below still
                # fires — otherwise a concern=false night with a stash
                # failure would silently swallow the proposed change.
                logger.error(f"Failed to stash readiness proposal: {e}")
                parsed["plan_change"] = None
                parsed["concern"] = True

        if not parsed.get("plan_change") and not parsed.get("concern") and review_row:
            # Quiet night: resolve immediately to 'no-op' so Pending in the
            # Reviews view keeps meaning "needs user attention" instead of
            # accumulating ~365 all-clear rows a year.
            try:
                state.resolve_pending_review(review_row["id"], "no-op")
            except Exception as e:
                logger.warning(f"Could not no-op quiet-night review: {e}")

        if parsed.get("plan_change") or parsed.get("concern") or _always_ping():
            return _format_user_message(parsed)
        logger.info("Quiet night — readiness check-in stored, no ping")
        return None
    except Exception as e:
        logger.error(f"Readiness review failed: {e}", exc_info=True)
        return None
