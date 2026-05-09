"""Handle Strava webhook events: fetch activity, translate, log, ping.

Runs in a background thread spawned from the Flask request handler so the
HTTP response can return 200 within Strava's 2-second window.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from state_manager import StateManager

from . import client, notify, translator

logger = logging.getLogger("pre_coach.strava.handler")

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
_state: Optional[StateManager] = None


def _get_state() -> StateManager:
    global _state
    if _state is None:
        _state = StateManager(STATE_DIR)
    return _state


def handle_event(payload: dict) -> None:
    """Process a single Strava webhook event payload.

    Strava event shape:
        {"aspect_type": "create"|"update"|"delete",
         "object_type": "activity"|"athlete",
         "object_id": <int>,
         "owner_id": <int>,
         "subscription_id": <int>,
         "event_time": <unix>}
    """
    aspect = payload.get("aspect_type")
    obj_type = payload.get("object_type")
    obj_id = payload.get("object_id")

    if obj_type != "activity" or aspect != "create":
        logger.info(f"Skipping Strava event: {aspect} {obj_type} {obj_id}")
        return

    try:
        activity_id = int(obj_id)
    except (TypeError, ValueError):
        logger.error(f"Strava event missing valid object_id: {payload}")
        return

    state = _get_state()

    # Idempotency: if we already logged this activity, skip.
    if activity_id in state.existing_strava_ids():
        logger.info(f"Strava activity {activity_id} already logged; skipping")
        return

    try:
        activity = client.get_activity(activity_id)
    except Exception as e:
        logger.error(f"Failed to fetch Strava activity {activity_id}: {e}")
        return

    athlete = state.load_athlete()
    hr_zones = (athlete.get("hr_zones") if isinstance(athlete, dict) else None) or {}

    try:
        entry = translator.activity_to_log_entry(activity, hr_zones=hr_zones)
    except Exception as e:
        logger.error(f"Failed to translate activity {activity_id}: {e}")
        return

    try:
        state.append_session(entry)
        logger.info(f"Logged Strava activity {activity_id}: {entry.get('miles')}mi {entry.get('type')}")
    except Exception as e:
        logger.error(f"Failed to append session for activity {activity_id}: {e}")
        return

    notify.send_activity_ping(entry)
