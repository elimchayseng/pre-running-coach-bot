import warnings

# Suppress mem0's deprecation warning (it bypasses normal filters)
_original_warn = warnings.warn


def _filtered_warn(message, category=UserWarning, stacklevel=1):
    if "output_format" in str(message):
        return
    _original_warn(message, category, stacklevel + 1)


warnings.warn = _filtered_warn

from companion import chat, reset_session  # noqa: E402, I001
from config import logger  # noqa: E402
from conversation_store import check_redis_health  # noqa: E402
from memory_manager import (  # noqa: E402
    clear_all_memories,
    get_all_memories,
    store_agent_personality,
    store_injury,
    update_goal,
)

# ANSI color codes
GREEN = "\033[92m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"


def print_pre(message: str) -> None:
    """Print PRE's response in green."""
    print(f"{GREEN}PRE: {message}{RESET}")


def print_system(message: str) -> None:
    """Print system message in blue."""
    print(f"{BLUE}{message}{RESET}")


def print_error(message: str) -> None:
    """Print error message in red."""
    print(f"{RED}{message}{RESET}")


def show_help() -> None:
    """Display available commands."""
    print_system("""
Commands:
  /goal <time>         - Update race goal (e.g., /goal 3:25)
  /injury <desc>       - Log injury with 14-day tracking
  /race                - Show race countdown and training phase
  /today               - Show current date and time context
  /history             - Show stored memories
  /reset               - Clear session history (Mem0 memories preserved)
  /clear               - Reset all memories
  /health              - Check system health
  /help                - Show this help
  /quit                - Exit
""")


def check_health() -> bool:
    """Run system health checks."""
    print_system("Running health checks...")

    # Check Redis
    redis_ok = check_redis_health()
    status = "OK" if redis_ok else "FAIL"
    print_system(f"  Redis: {status}")

    # Check Mem0 (try a simple search)
    try:
        from memory_manager import _mem0_search

        _mem0_search("test", limit=1)
        print_system("  Mem0: OK")
        mem0_ok = True
    except Exception as e:
        print_error(f"  Mem0: FAIL ({e})")
        mem0_ok = False

    # Check LLM
    try:
        from config import HEROKU_MODEL, llm_client

        llm_client.chat.completions.create(
            model=HEROKU_MODEL, messages=[{"role": "user", "content": "ping"}], max_tokens=5
        )
        print_system("  LLM: OK")
        llm_ok = True
    except Exception as e:
        print_error(f"  LLM: FAIL ({e})")
        llm_ok = False

    return redis_ok and mem0_ok and llm_ok


def handle_command(cmd: str) -> bool:
    """Handle slash commands. Returns True if should continue loop."""
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/quit":
        print_system("Goodbye! Keep running strong!")
        return False

    elif command == "/help":
        show_help()

    elif command == "/goal":
        if not arg:
            print_system("Usage: /goal <target time>  (e.g., /goal 3:25)")
        else:
            update_goal(arg)
            print_system(f"Goal updated: {arg}")

    elif command == "/injury":
        if not arg:
            print_system("Usage: /injury <description>  (e.g., /injury left knee soreness)")
        else:
            store_injury(arg)
            print_system(f"Injury logged (14-day tracking): {arg}")

    elif command == "/race":
        from temporal_context import DEFAULT_RACE_DATE, get_temporal_context

        ctx = get_temporal_context()
        print_system(f"Race: Boston Marathon - {DEFAULT_RACE_DATE.strftime('%B %d, %Y')}")
        print_system(f"Countdown: {ctx['days_to_race']} days ({ctx['weeks_to_race']} weeks)")
        print_system(f"Phase: {ctx['training_phase']}")

    elif command == "/today":
        from temporal_context import get_temporal_context

        ctx = get_temporal_context()
        print_system(f"Date: {ctx['date']}")
        print_system(f"Time: {ctx['time_of_day']}")
        print_system(f"Days to race: {ctx['days_to_race']}")

    elif command == "/history":
        memories = get_all_memories()
        if not memories:
            print_system("No memories stored yet.")
        else:
            print_system("Stored memories:")
            for mem in memories:
                if mem is None:
                    continue
                metadata = mem.get("metadata") or {}
                memory_text = mem.get("memory", "")
                if metadata:
                    print(f"  - {memory_text} [metadata: {metadata}]")
                else:
                    print(f"  - {memory_text}")

    elif command == "/reset":
        reset_session()
        print_system("Session history cleared. Mem0 memories preserved.")

    elif command == "/clear":
        confirm = input(f"{BLUE}Are you sure? This will delete all memories. (yes/no): {RESET}")
        if confirm.lower() == "yes":
            clear_all_memories()
            print_system("All memories cleared.")
        else:
            print_system("Cancelled.")

    elif command == "/health":
        check_health()

    else:
        print_system(f"Unknown command: {command}. Type /help for available commands.")

    return True


def main():
    """Main chat loop."""
    print_system("=" * 50)
    print_system("Boston Marathon Training Companion")
    print_system("=" * 50)

    # Startup health check
    if not check_redis_health():
        print_error("Warning: Redis connection failed. Session history may not persist.")
        logger.warning("Redis unavailable at startup")

    print_pre("Hey! I'm PRE, your running coach. Ready to help you train for Boston!")
    print_pre("Tell me about your goals, and we'll build a plan together.")
    print_system("Type /help for commands, /quit to exit.\n")

    # Initialize coach personality
    try:
        store_agent_personality()
    except Exception as e:
        logger.warning(f"Failed to store agent personality: {e}")

    # Start with clean session
    reset_session()

    while True:
        try:
            user_input = input("You: ").strip()

            if not user_input:
                continue

            if user_input.startswith("/"):
                if not handle_command(user_input):
                    break
                continue

            response = chat(user_input)
            print_pre(response)
            print()

        except KeyboardInterrupt:
            print_system("\nGoodbye! Keep running strong!")
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            print_error(f"Error: {e}")


if __name__ == "__main__":
    main()
