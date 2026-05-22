"""PRE Telegram handlers.

Slash commands (no agent round-trip): /start, /help, /today, /plan, /log,
/race, /reset, /health, /reconcile. Free-text messages route to
companion.chat which runs the full tool-use loop.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from companion import chat as companion_chat
from companion import reset_session
from health import format_commands_text, run_health_checks
from state_manager import StateManager
from temporal_context import build_temporal_prompt, get_temporal_context, today_local

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent / "state"
_state: StateManager | None = None


def _get_state() -> StateManager:
    global _state
    if _state is None:
        _state = StateManager(STATE_DIR)
    return _state


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    welcome = (
        f"Hey {user.first_name}, I'm PRE.\n\n"
        f"Commands:\n{format_commands_text()}\n\n"
        "Send a message about your training and I'll respond."
    )
    await update.message.reply_text(welcome)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Commands:\n{format_commands_text()}")


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fast path: today's prescribed workout straight from plan.md, no LLM.
    Renders every slot on multi-session days, prefixed with [AM]/[PM] or [k/N]."""
    state = _get_state()
    today = today_local()
    workouts = state.get_todays_workouts(today)
    if not workouts:
        await update.message.reply_text(
            f"No workout prescribed for {today.isoformat()}. Send a message and I'll fill it in."
        )
        return
    header = today.strftime("%a %b %d")
    blocks: list[str] = []
    for w in workouts:
        prefix = f"[{w['slot_label']}] " if w["slot_label"] else ""
        if w["is_rest_day"]:
            blocks.append(f"{prefix}rest day. {w['notes']}".strip())
            continue
        lines = [
            f"{prefix}{w['workout']}",
            f"Pace: {w['pace_target']}" if w["pace_target"] and w["pace_target"] != "—" else None,
            w["notes"] or None,
        ]
        blocks.append("\n".join(line for line in lines if line))
    body = "\n\n".join(blocks)
    await update.message.reply_text(f"{header}:\n{body}" if len(workouts) > 1 else f"{header}: {body}")


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Print the current plan (truncated if needed for Telegram's 4096 limit)."""
    plan = _get_state().render_plan()
    if not plan.strip():
        await update.message.reply_text("No plan set. Tell me about your goals and I'll draft one.")
        return
    if len(plan) > 4000:
        plan = plan[:4000] + "\n... (truncated)"
    await update.message.reply_text(plan)


async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Print recent sessions. Optional arg: days (default 7)."""
    days = 7
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass
    sessions = _get_state().get_recent_sessions(days=days)
    if not sessions:
        await update.message.reply_text(f"No sessions logged in the last {days} days.")
        return
    lines = [f"Last {days} days ({len(sessions)} entries):"]
    for s in sessions[-15:]:
        miles = f" {s['miles']}mi" if s.get("miles") else ""
        pace = f" @ {s['pace_avg']}" if s.get("pace_avg") else ""
        notes = f" — {s['notes']}" if s.get("notes") else ""
        lines.append(f"  {s.get('date', '?')} {s.get('type', '?')}{miles}{pace}{notes}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    await update.message.reply_text(text)


async def race_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = get_temporal_context()
    if ctx["days_to_race"] is None:
        await update.message.reply_text("No target race configured in athlete.yaml.")
        return
    await update.message.reply_text(build_temporal_prompt())


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_session()
    await update.message.reply_text("Session history cleared.")


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    results = run_health_checks()
    lines = [f"{component.capitalize()}: {'OK' if ok else 'FAIL'}" for component, ok in results.items()]
    await update.message.reply_text("Health Check:\n" + "\n".join(lines))


async def reconcile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Walk recent logs and re-mark gcal completion. Optional arg: days (default 14)."""
    days = 14
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass

    from google_calendar import sync

    try:
        summary = sync.reconcile_completion(_get_state(), days_back=days)
    except Exception as e:
        logger.error(f"reconcile failed: {e}")
        await update.message.reply_text(f"Reconcile failed: {type(e).__name__}: {e}")
        return

    corrected = summary["corrected"]
    orphans = summary["orphans"]
    errors = summary["errors"]
    lines = [
        f"Reconcile (last {days} days):",
        f"  corrected: {len(corrected)}",
        f"  skipped (no log): {len(summary['skipped'])}",
        f"  orphans (completed in gcal, no log): {len(orphans)}",
        f"  errors: {len(errors)}",
    ]
    if corrected:
        lines.append("\nCorrected:")
        for c in corrected[:10]:
            lines.append(f"  {c['date']} ({c['kind']})")
    if orphans:
        lines.append("\nOrphans (review manually):")
        for o in orphans[:10]:
            lines.append(f"  {o['date']} → {o['event_id']}")
    if errors:
        lines.append("\nErrors:")
        for e in errors[:5]:
            lines.append(f"  {e['date']}: {e['error']}")
    await update.message.reply_text("\n".join(lines))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route free-text messages through the companion agent."""
    user_message = update.message.text
    chat_id = update.effective_chat.id if update.effective_chat else None
    # Log chat_id at INFO so it shows up in Railway logs — helpful for the
    # one-time USER_TELEGRAM_CHAT_ID setup.
    logger.info(f"Message from chat_id={chat_id}: {user_message[:50]}...")
    try:
        response = companion_chat(user_message)
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        await update.message.reply_text("Sorry, I encountered an error. Please try again in a moment.")
