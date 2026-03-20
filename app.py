"""
PRE: Running Coach Bot - Flask webhook server for Telegram
"""
import os
import logging
import asyncio
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot import start_command, handle_message

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize Telegram application
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

telegram_app = None


def get_telegram_app():
    """Get or create and initialize the Telegram application."""
    global telegram_app
    if telegram_app is None:
        telegram_app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .build()
        )
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        # Must initialize before process_update can be called
        asyncio.run(telegram_app.initialize())
    return telegram_app


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint for Railway."""
    return jsonify({"status": "healthy", "bot": "PRE Running Coach"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram webhook updates."""
    try:
        telegram_application = get_telegram_app()
        update = Update.de_json(request.get_json(force=True), telegram_application.bot)

        # Process update asynchronously
        asyncio.run(telegram_application.process_update(update))

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

    asyncio.run(_set_webhook())


# Set up webhook on startup
with app.app_context():
    setup_webhook()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
