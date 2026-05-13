"""Handle Strava webhook events: fetch activity, translate, log, ping.

Runs in a background thread spawned from the Flask request handler so the
HTTP response can return 200 within Strava's 2-second window.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from state_manager import StateManager

from . import client, notify, review, translator
from .client import StravaAPIError

logger = logging.getLogger("pre_coach.strava.handler")

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_state: Optional[StateManager] = None


def _get_state() -> StateManager:
    global _state
    if _state is None:
        _state = StateManager(STATE_DIR)
    return _state


def _is_propagation_404(exc: BaseException) -> bool:
    """Strava's webhook bus and read API are eventually consistent — the
    `create` event can fire seconds before the activity is fetchable. A 404
    immediately after a webhook is almost always propagation, not a missing
    activity. Retry it.
    """
    return isinstance(exc, StravaAPIError) and "-> 404" in str(exc)


def _log_retry(state: RetryCallState) -> None:
    attempt = state.attempt_number
    next_action = state.next_action
    delay = getattr(next_action, "sleep", None) if next_action else None
    logger.info(
        "Strava activity not yet propagated (attempt %d, sleeping %.1fs before retry)",
        attempt,
        delay if delay is not None else -1,
    )


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=32),
    retry=retry_if_exception(_is_propagation_404),
    before_sleep=_log_retry,
    reraise=True,
)
def _fetch_with_propagation_retry(activity_id: int) -> dict:
    """Fetch an activity, retrying on transient 404 (Strava propagation lag).

    Backs off 2 → 4 → 8 → 16 → 32s, total ~62s of waiting across 6 attempts.
    Most propagation completes within 5–15s; the long tail is rare but real.
    """
    return client.get_activity(activity_id)


def handle_event(payload: dict) -> None:
    """Process a single Strava webhook event payload.

    Strava event shape:
        {"aspect_type": "create"|"update"|"delete",
         "object_type": "activity"|"athlete",
         "object_id": <int>,
         "owner_id": <int>,
         "subscription_id": <int>,
         "event_time": <unix>}

    We handle two cases:
      - create: fetch, translate, append, ping
      - update: fetch, translate, replace existing log entry (no ping —
        update events fire for gear assignment, name edits, workout-type
        retags after upload, etc. They shouldn't spam the user.)
    """
    aspect = payload.get("aspect_type")
    obj_type = payload.get("object_type")
    obj_id = payload.get("object_id")

    if obj_type != "activity" or aspect not in ("create", "update"):
        logger.info(f"Skipping Strava event: {aspect} {obj_type} {obj_id}")
        return

    try:
        activity_id = int(obj_id)
    except (TypeError, ValueError):
        logger.error(f"Strava event missing valid object_id: {payload}")
        return

    state = _get_state()

    if aspect == "create":
        _handle_create(state, activity_id)
    else:  # "update"
        _handle_update(state, activity_id)


def _handle_create(state: StateManager, activity_id: int) -> None:
    # Optimistic pre-check: avoids a Strava API fetch when we've already logged
    # this activity. The DB's UNIQUE index on details.strava_id is the
    # authoritative dedup — even if two webhooks race past this check, only
    # one append_session will succeed.
    if activity_id in state.existing_strava_ids():
        logger.info(f"Strava activity {activity_id} already logged; skipping")
        return

    entry = _fetch_and_translate(state, activity_id)
    if entry is None:
        return

    try:
        state.append_session(entry)
        logger.info(f"Logged Strava activity {activity_id}: {entry.get('miles')}mi {entry.get('type')}")
    except sqlite3.IntegrityError:
        # Lost the race against a concurrent webhook for the same activity.
        # The other thread won; nothing to do here.
        logger.info(f"Strava activity {activity_id} raced to insert; skipping")
        return
    except Exception as e:
        logger.error(f"Failed to append session for activity {activity_id}: {e}")
        return

    _mark_calendar_complete(state, entry)

    # Runs get LLM analysis vs. today's plan; other activity types still get
    # the deterministic templated ping. If the review fails for any reason,
    # fall back to the templated ping so the user always hears something.
    if review.is_run_type(entry):
        message = review.run_post_activity_review(entry, state)
        if message:
            notify.send_telegram_text(message)
            return
        logger.info(f"Review unavailable for activity {activity_id}; falling back to templated ping")
    notify.send_activity_ping(entry)


def _handle_update(state: StateManager, activity_id: int) -> None:
    # Only update if we already have this activity logged. Ignoring updates
    # for activities we never created entries for (probably synced before
    # we were running) keeps the log focused.
    if activity_id not in state.existing_strava_ids():
        logger.info(f"Update event for unknown activity {activity_id}; ignoring")
        return

    entry = _fetch_and_translate(state, activity_id)
    if entry is None:
        return

    try:
        replaced = state.update_session_by_strava_id(activity_id, entry)
    except Exception as e:
        logger.error(f"Failed to update session for activity {activity_id}: {e}")
        return

    if replaced:
        logger.info(f"Updated Strava activity {activity_id}: now {entry.get('type')} {entry.get('miles')}mi")
        # A retag (e.g., Run → Workout) can flip the day from on-plan to
        # off-plan or vice versa; re-run completion so the calendar reflects
        # the new partitioning.
        _mark_calendar_complete(state, entry)
    else:
        # Race: we saw it in existing_strava_ids() above but missed it on
        # rewrite. Unusual but not fatal.
        logger.warning(f"Update event for activity {activity_id}: entry not found on rewrite")


def _mark_calendar_complete(state: StateManager, entry: dict) -> None:
    """Best-effort: mark the day's Google Calendar event(s) complete after a
    Strava log write. Never raises — a gcal hiccup must not break the webhook
    or the log entry that was already persisted.
    """
    log_date = entry.get("date")
    if not log_date:
        return
    try:
        from google_calendar.sync import mark_complete  # late import: optional integration

        mark_complete(state, log_date)
    except Exception as e:
        logger.warning(f"mark_complete failed for {log_date}: {type(e).__name__}: {e}")


def _fetch_and_translate(state: StateManager, activity_id: int) -> Optional[dict]:
    """Shared fetch + translate flow. Returns the translated entry or None
    on any error (errors logged inline)."""
    try:
        activity = _fetch_with_propagation_retry(activity_id)
    except StravaAPIError as e:
        if "-> 404" in str(e):
            logger.error(
                "Activity %s never became fetchable after retries — likely "
                "deleted between event and our fetch. Giving up.",
                activity_id,
            )
        else:
            logger.error(f"Failed to fetch Strava activity {activity_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch Strava activity {activity_id}: {e}")
        return None

    athlete = state.load_athlete()
    hr_zones = (athlete.get("hr_zones") if isinstance(athlete, dict) else None) or {}

    try:
        return translator.activity_to_log_entry(activity, hr_zones=hr_zones)
    except Exception as e:
        logger.error(f"Failed to translate activity {activity_id}: {e}")
        return None
