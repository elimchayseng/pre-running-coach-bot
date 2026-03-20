"""
PRE: Running Coach Bot - Flask webhook server for Telegram
"""

import asyncio
import logging
import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot import (
    forget_all_command,
    goal_command,
    handle_message,
    health_command,
    help_command,
    history_command,
    injury_command,
    race_command,
    reset_command,
    start_command,
    today_command,
)

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize Telegram application
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

telegram_app = None

# --- Persistent event loop in a background thread ---
_loop = asyncio.new_event_loop()


def _start_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


_loop_thread = threading.Thread(target=_start_loop, args=(_loop,), daemon=True)
_loop_thread.start()


def _run_async(coro):
    """Run a coroutine on the persistent event loop and return its result."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


def get_telegram_app():
    """Get or create and initialize the Telegram application."""
    global telegram_app
    if telegram_app is None:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("goal", goal_command))
        telegram_app.add_handler(CommandHandler("injury", injury_command))
        telegram_app.add_handler(CommandHandler("race", race_command))
        telegram_app.add_handler(CommandHandler("today", today_command))
        telegram_app.add_handler(CommandHandler("history", history_command))
        telegram_app.add_handler(CommandHandler("reset", reset_command))
        telegram_app.add_handler(CommandHandler("forgetall", forget_all_command))
        telegram_app.add_handler(CommandHandler("health", health_command))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        # Must initialize before process_update can be called
        _run_async(telegram_app.initialize())
    return telegram_app


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint for Railway — checks Redis and Mem0."""
    from health import run_health_checks

    results = run_health_checks()
    all_ok = all(results.values())
    status_code = 200 if all_ok else 503
    return jsonify(
        {
            "status": "healthy" if all_ok else "degraded",
            "bot": "PRE Running Coach",
            **{k: ("ok" if v else "fail") for k, v in results.items()},
        }
    ), status_code


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram webhook updates."""
    # T8: Verify the request comes from Telegram via secret_token
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret:
        token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token_header != webhook_secret:
            logger.warning("Webhook request rejected: invalid secret token")
            return jsonify({"status": "forbidden"}), 403

    try:
        telegram_application = get_telegram_app()
        update = Update.de_json(request.get_json(force=True), telegram_application.bot)

        # Process update on the persistent event loop
        _run_async(telegram_application.process_update(update))

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        # T9: Don't leak internal error details in response
        return jsonify({"status": "error", "message": "Internal processing error"}), 500


def setup_webhook():
    """Register webhook URL with Telegram."""
    if not TELEGRAM_BOT_TOKEN or not WEBHOOK_URL:
        logger.warning("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL - skipping webhook setup")
        return

    webhook_endpoint = f"{WEBHOOK_URL}/webhook"
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    async def _set_webhook():
        kwargs = {"url": webhook_endpoint}
        if webhook_secret:
            kwargs["secret_token"] = webhook_secret
        await bot.set_webhook(**kwargs)
        logger.info(f"Webhook set to: {webhook_endpoint}")

    _run_async(_set_webhook())


# Set up webhook on startup
with app.app_context():
    setup_webhook()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
