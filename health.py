"""Shared health check logic used by CLI, Telegram, and Flask."""

from config import HEROKU_MODEL, logger


def run_health_checks() -> dict[str, bool]:
    """Health-check Redis and the LLM. Returns dict of component -> ok."""
    results = {}

    from conversation_store import check_redis_health

    results["redis"] = check_redis_health()

    try:
        from config import llm_client

        llm_client.chat.completions.create(
            model=HEROKU_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        results["llm"] = True
    except Exception as e:
        logger.error(f"LLM health check failed: {e}")
        results["llm"] = False

    # Strava is optional — only check if creds are present.
    import os

    if os.getenv("STRAVA_CLIENT_ID") and os.getenv("STRAVA_CLIENT_SECRET"):
        try:
            from strava.auth import health_check as strava_health

            results["strava"] = strava_health()
        except Exception as e:
            logger.error(f"Strava health check failed: {e}")
            results["strava"] = False

    # Google Calendar is optional — only check when a calendar is configured.
    # This catches the silent failure mode where the OAuth refresh token has
    # expired (e.g. the 7-day testing-mode cap) and the plan has quietly
    # stopped syncing to the calendar.
    if os.getenv("CALENDAR_ID"):
        try:
            from google_calendar.auth import health_check as gcal_health

            results["gcal"] = gcal_health()
        except Exception as e:
            logger.error(f"Gcal health check failed: {e}")
            results["gcal"] = False

    # Notion mirror is optional — only check when a token is configured.
    if os.getenv("NOTION_TOKEN"):
        try:
            from notion.client import NotionClient

            results["notion"] = bool(NotionClient().users_me().get("id"))
        except Exception as e:
            logger.error(f"Notion health check failed: {e}")
            results["notion"] = False

    return results


# Single source of truth for slash commands across CLI, Telegram, and Flask.
COMMANDS = [
    ("/today", "Show today's workout from the plan"),
    ("/plan", "Show the current training plan"),
    ("/log [days]", "Show recent logged sessions (default 7 days)"),
    ("/race", "Show race countdown and training phase"),
    ("/reset", "Clear short-term conversation history"),
    ("/health", "Check system health"),
    ("/reconcile [days]", "Re-mark calendar completion from recent logs (default 14 days)"),
    ("/help", "Show commands"),
]


def format_commands_text(include_quit: bool = False) -> str:
    """Format command list for display. include_quit=True for CLI."""
    lines = [f"{cmd} - {desc}" for cmd, desc in COMMANDS]
    if include_quit:
        lines.append("/quit - Exit")
    return "\n".join(lines)
