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

    # COROS is optional — only check when token storage is configured
    # (COROS_TOKENS_BACKEND set in prod; a local token file in dev). The
    # gate sits OUTSIDE the try (mirroring the Notion block) so an
    # unconfigured COROS never reports unhealthy. The check itself is
    # deliberately passive — token blob present + well-formed, plus data
    # freshness — and NEVER refreshes: a probe-path refresh would rotate
    # the single-use refresh token where a gunicorn timeout could kill the
    # worker between rotation and persist (unrecoverable lockout). Real
    # refresh validity is exercised by the nightly scheduler's watchdog.
    coros_configured = False
    try:
        from coros.auth import TOKEN_FILE as _coros_token_file

        coros_configured = bool(os.getenv("COROS_TOKENS_BACKEND")) or _coros_token_file.exists()
    except Exception:  # noqa: BLE001 — import failure = not configured
        pass
    if coros_configured:
        try:
            from coros.auth import health_check as coros_health

            results["coros"] = coros_health()
            if results["coros"]:
                # Freshness: a token can be valid while the nightly pull has
                # been failing for days (non-auth breakage is otherwise
                # invisible — exit codes only reach logs). A table with rows
                # but none recent means the pull STOPPED; a fully empty
                # table is a fresh install and stays healthy.
                from state_manager import StateManager

                state = StateManager()
                if not state.get_daily_health(days=3) and state.get_daily_health(days=3650):
                    logger.warning("COROS auth ok but no daily_health rows in 3 days")
                    results["coros"] = False
        except Exception as e:
            logger.error(f"COROS health check failed: {e}")
            results["coros"] = False

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
