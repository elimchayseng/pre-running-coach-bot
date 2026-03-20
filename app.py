"""
PRE: Running Coach Bot - Flask webhook server for Telegram
"""

import asyncio
import logging
import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot import (
    clear_command,
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
from companion import chat as companion_chat

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
        telegram_app.add_handler(CommandHandler("clear", clear_command))
        telegram_app.add_handler(CommandHandler("health", health_command))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        # Must initialize before process_update can be called
        _run_async(telegram_app.initialize())
    return telegram_app


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint for Railway."""
    return jsonify({"status": "healthy", "bot": "PRE Running Coach"})


@app.route("/chat", methods=["GET"])
def chat_page():
    """Serve the web chat UI."""
    return render_template("chat.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """API endpoint for web chat — runs the same pipeline as Telegram."""
    try:
        data = request.get_json(force=True)
        message = data.get("message", "").strip()
        user_id = data.get("user_id", "web_test")

        if not message:
            return jsonify({"error": "Empty message"}), 400

        reply = companion_chat(message, user_id=user_id)
        return jsonify({"reply": reply})
    except Exception as e:
        logger.error(f"Web chat error: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram webhook updates."""
    try:
        telegram_application = get_telegram_app()
        update = Update.de_json(request.get_json(force=True), telegram_application.bot)

        # Process update on the persistent event loop
        _run_async(telegram_application.process_update(update))

        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def setup_webhook():
    """Register webhook URL with Telegram."""
    if not TELEGRAM_BOT_TOKEN or not WEBHOOK_URL:
        logger.warning("Missing TELEGRAM_BOT_TOKEN or WEBHOOK_URL - skipping webhook setup")
        return

    webhook_endpoint = f"{WEBHOOK_URL}/webhook"
    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    async def _set_webhook():
        await bot.set_webhook(url=webhook_endpoint)
        logger.info(f"Webhook set to: {webhook_endpoint}")

    _run_async(_set_webhook())


# Set up webhook on startup
with app.app_context():
    setup_webhook()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
