"""PRE running coach — CLI."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from companion import chat, reset_session
from config import logger
from conversation_store import check_redis_health
from health import format_commands_text, run_health_checks
from state_manager import StateManager
from temporal_context import build_temporal_prompt, get_temporal_context

GREEN = "\033[92m"
BLUE = "\033[94m"
RED = "\033[91m"
RESET = "\033[0m"

STATE_DIR = Path(__file__).resolve().parent / "state"
_state: StateManager | None = None


def _get_state() -> StateManager:
    global _state
    if _state is None:
        _state = StateManager(STATE_DIR)
    return _state


def print_pre(message: str) -> None:
    print(f"{GREEN}PRE: {message}{RESET}")


def print_system(message: str) -> None:
    print(f"{BLUE}{message}{RESET}")


def print_error(message: str) -> None:
    print(f"{RED}{message}{RESET}")


def show_help() -> None:
    print_system(f"\nCommands:\n{format_commands_text(include_quit=True)}\n")


def check_health() -> bool:
    print_system("Running health checks...")
    results = run_health_checks()
    for component, ok in results.items():
        status = "OK" if ok else "FAIL"
        printer = print_system if ok else print_error
        printer(f"  {component.capitalize()}: {status}")
    return all(results.values())


def handle_command(cmd: str) -> bool:
    """Handle slash commands. Returns True if loop should continue."""
    parts = cmd.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command == "/quit":
        print_system("Goodbye! Keep running strong!")
        return False

    if command == "/help":
        show_help()

    elif command == "/today":
        state = _get_state()
        today = date.today()
        w = state.get_todays_workout(today)
        if not w["found"]:
            print_system(f"No workout prescribed for {today.isoformat()}.")
        elif w["is_rest_day"]:
            print_system(f"{today.strftime('%a %b %d')}: rest day. {w['notes']}".strip())
        else:
            print_system(f"{today.strftime('%a %b %d')}: {w['workout']}")
            if w["pace_target"] and w["pace_target"] != "—":
                print_system(f"  Pace: {w['pace_target']}")
            if w["notes"]:
                print_system(f"  Notes: {w['notes']}")

    elif command == "/plan":
        plan = _get_state().load_plan()
        if not plan.strip():
            print_system("No plan set.")
        else:
            print(plan)

    elif command == "/log":
        days = 7
        if arg:
            try:
                days = int(arg.strip())
            except ValueError:
                pass
        sessions = _get_state().get_recent_sessions(days=days)
        if not sessions:
            print_system(f"No sessions logged in the last {days} days.")
        else:
            print_system(f"Last {days} days ({len(sessions)} entries):")
            for s in sessions[-15:]:
                miles = f" {s['miles']}mi" if s.get("miles") else ""
                pace = f" @ {s['pace_avg']}" if s.get("pace_avg") else ""
                notes = f" — {s['notes']}" if s.get("notes") else ""
                print(f"  {s.get('date', '?')} {s.get('type', '?')}{miles}{pace}{notes}")

    elif command == "/race":
        ctx = get_temporal_context()
        if ctx["days_to_race"] is None:
            print_system("No target race configured in athlete.yaml.")
        else:
            print_system(build_temporal_prompt())

    elif command == "/reset":
        reset_session()
        print_system("Session history cleared.")

    elif command == "/health":
        check_health()

    else:
        print_system(f"Unknown command: {command}. Type /help for available commands.")

    return True


def main():
    print_system("=" * 50)
    print_system("PRE Running Coach")
    print_system("=" * 50)

    if not check_redis_health():
        print_error("Warning: Redis connection failed. Session history may not persist.")
        logger.warning("Redis unavailable at startup")

    print_pre("Hey, PRE here. What's the plan today?")
    print_system("Type /help for commands, /quit to exit.\n")

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
