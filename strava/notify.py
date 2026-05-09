"""Send a deterministic 'auto-logged' ping to the user's Telegram chat.

No LLM call on this path — the format is templated. The user's reply (RPE,
notes, anything tight?) goes through `bot.handle_message` → `companion.chat`
where the agent has the entry in `recent_sessions` for context.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("pre_coach.strava.notify")


def _format_ping(entry: dict) -> str:
    """Render a 3-4 line summary from a log entry."""
    miles = entry.get("miles")
    pace = entry.get("pace_avg")
    hr = entry.get("hr_avg")
    entry_type = entry.get("type", "run")
    details = entry.get("details") or {}
    elev = details.get("elevation_gain_ft")
    moving = details.get("moving_time")
    laps = details.get("laps") or []

    line1 = f"Logged: {miles}mi" if miles else "Logged activity"
    if pace:
        line1 += f" @ {pace}"
    line1 += f" ({entry_type})"

    bits = []
    if hr is not None:
        bits.append(f"HR avg {hr}")
    if elev is not None:
        bits.append(f"{elev}ft gain")
    if moving:
        bits.append(moving)
    line2 = " · ".join(bits) if bits else ""

    # Hint that the workout is structured — agent will see this in chat history.
    work_lap_count = sum(1 for lap in laps if lap.get("name") and "rep" in lap.get("name", "").lower())
    if work_lap_count >= 2:
        line2 += f"  ·  {work_lap_count} work laps"

    line3 = "RPE? Anything tight?"
    return "\n".join(line for line in [line1, line2, line3] if line)


def send_activity_ping(entry: dict, chat_id: Optional[str] = None) -> bool:
    """Send the templated ping via the Telegram Bot API.

    chat_id defaults to USER_TELEGRAM_CHAT_ID env var. Returns True on success.
    Logs and returns False on any failure — never raises (called from a
    background thread we don't want to crash on transient errors).
    """
    chat_id = chat_id or os.getenv("USER_TELEGRAM_CHAT_ID")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not chat_id or not token:
        logger.warning("Cannot send Strava ping: USER_TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN unset")
        return False

    text = _format_ping(entry)

    try:
        from telegram import Bot
    except ImportError:
        logger.error("python-telegram-bot not installed; cannot send ping")
        return False

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)

    try:
        asyncio.run(_send())
        return True
    except RuntimeError:
        # If we're already in a running loop (Flask thread + async telegram),
        # spin up a fresh loop in this thread.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_send())
            return True
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Failed to send Strava ping: {e}")
        return False
