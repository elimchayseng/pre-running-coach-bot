"""Shared health check logic used by CLI, Telegram, and Flask."""

from config import HEROKU_MODEL, logger


def run_health_checks() -> dict[str, bool]:
    """Run health checks against Redis, Mem0, and LLM. Returns dict of component -> ok."""
    results = {}

    # Redis
    from conversation_store import check_redis_health

    results["redis"] = check_redis_health()

    # Mem0
    try:
        from memory_manager import _mem0_search

        _mem0_search("health", limit=1)
        results["mem0"] = True
    except Exception as e:
        logger.error(f"Mem0 health check failed: {e}")
        results["mem0"] = False

    # LLM
    try:
        from config import llm_client

        llm_client.chat.completions.create(
            model=HEROKU_MODEL, messages=[{"role": "user", "content": "ping"}], max_tokens=5
        )
        results["llm"] = True
    except Exception as e:
        logger.error(f"LLM health check failed: {e}")
        results["llm"] = False

    return results


# Single source of truth for slash commands
COMMANDS = [
    ("/goal <time>", "Update race goal (e.g., /goal 3:25)"),
    ("/injury <desc>", "Log injury with 14-day tracking"),
    ("/race", "Show race countdown and training phase"),
    ("/today", "Show current date and time context"),
    ("/history", "Show stored memories"),
    ("/reset", "Clear session history (memories kept)"),
    ("/forgetall", "Delete all memories (requires confirmation)"),
    ("/health", "Check system health"),
    ("/help", "Show commands"),
]


def format_commands_text(include_quit: bool = False) -> str:
    """Format command list for display. include_quit=True for CLI."""
    lines = [f"{cmd} - {desc}" for cmd, desc in COMMANDS]
    if include_quit:
        lines.append("/quit - Exit")
    return "\n".join(lines)
