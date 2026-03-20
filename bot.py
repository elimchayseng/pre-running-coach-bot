"""
PRE: Running Coach Bot - Telegram handlers using shared companion pipeline
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from companion import chat as companion_chat
from companion import reset_session
from health import format_commands_text, run_health_checks
from memory_manager import (
    USER_ID,
    clear_all_memories,
    get_all_memories,
    store_injury,
    update_goal,
)
from temporal_context import get_race_date, get_temporal_context

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def get_user_id(update: Update) -> str:
    """Always use the shared USER_ID so Telegram hits the same mem0 memories."""
    return USER_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    commands_text = format_commands_text()
    welcome_message = (
        f"Hey {user.first_name}! I'm PRE, your running coach bot.\n\n"
        "I can help you with:\n"
        "- Training plans and workout suggestions\n"
        "- Race preparation and pacing strategies\n"
        "- Recovery and injury prevention\n"
        "- Motivation and goal setting\n\n"
        f"Commands:\n{commands_text}\n\n"
        "Just send me a message about your running goals or questions!"
    )
    await update.message.reply_text(welcome_message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(f"Commands:\n{format_commands_text()}")


async def goal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /goal command."""
    arg = " ".join(context.args) if context.args else ""
    if not arg:
        await update.message.reply_text("Usage: /goal <target time>  (e.g., /goal 3:25)")
        return
    update_goal(arg)
    await update.message.reply_text(f"Goal updated: {arg}")


async def injury_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /injury command."""
    arg = " ".join(context.args) if context.args else ""
    if not arg:
        await update.message.reply_text("Usage: /injury <description>  (e.g., /injury left knee soreness)")
        return
    store_injury(arg)
    await update.message.reply_text(f"Injury logged (14-day tracking): {arg}")


async def race_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /race command."""
    ctx = get_temporal_context()
    race_date = get_race_date()
    await update.message.reply_text(
        f"Race: Boston Marathon - {race_date.strftime('%B %d, %Y')}\n"
        f"Countdown: {ctx['days_to_race']} days ({ctx['weeks_to_race']} weeks)\n"
        f"Phase: {ctx['training_phase']}"
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /today command."""
    ctx = get_temporal_context()
    await update.message.reply_text(
        f"Date: {ctx['date']}\nTime: {ctx['time_of_day']}\nDays to race: {ctx['days_to_race']}"
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history command."""
    memories = get_all_memories()
    if not memories:
        await update.message.reply_text("No memories stored yet.")
        return

    lines = []
    for mem in memories:
        if mem is None:
            continue
        metadata = mem.get("metadata") or {}
        memory_text = mem.get("memory", "")
        if metadata:
            lines.append(f"- {memory_text} [metadata: {metadata}]")
        else:
            lines.append(f"- {memory_text}")

    text = "Stored memories:\n" + "\n".join(lines) if lines else "No memories stored yet."
    # Telegram has a 4096 char limit per message
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    await update.message.reply_text(text)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reset command."""
    user_id = get_user_id(update)
    reset_session(user_id)
    await update.message.reply_text("Session history cleared. Mem0 memories preserved.")


async def forget_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /forgetall command with confirmation."""
    args = context.args
    if args and args[0].lower() == "confirm":
        clear_all_memories()
        await update.message.reply_text("All memories deleted permanently.")
    else:
        await update.message.reply_text(
            "This will permanently delete ALL stored memories.\nSend /forgetall confirm to proceed."
        )


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /health command."""
    results = run_health_checks()
    lines = [f"{component.capitalize()}: {'OK' if ok else 'FAIL'}" for component, ok in results.items()]
    await update.message.reply_text("Health Check:\n" + "\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming messages using the shared companion pipeline."""
    user_message = update.message.text
    user_id = get_user_id(update)

    logger.info(f"Message from {user_id}: {user_message[:50]}...")

    try:
        # Use the same pipeline as CLI
        response = companion_chat(user_message, user_id=user_id)
        await update.message.reply_text(response)

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please try again in a moment.")
