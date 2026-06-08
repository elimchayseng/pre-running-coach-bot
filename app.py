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
    handle_message,
    health_command,
    help_command,
    log_command,
    plan_command,
    race_command,
    reconcile_command,
    reset_command,
    start_command,
    today_command,
)

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# The Phase 1A.2 DB cutover runs in gunicorn's on_starting hook
# (see gunicorn.conf.py) so it happens once before workers serve traffic and
# never as a side effect of importing this module.

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


def _run_async(coro, timeout: float = 180):
    """Run a coroutine on the persistent event loop and return its result.

    The default 180s ceiling is sized for the slowest path that goes through
    here: a plan-edit Telegram update where the tool-use loop re-emits the
    full plan.md as a tool-call argument. Now that the webhook acks 200 and
    delegates `process_update` to a background thread, no caller of this
    helper is on a gunicorn worker — so a longer wait does not block other
    requests. Startup callers (`setup_webhook`, `initialize`) make short
    network calls and finish well under this ceiling.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


def get_telegram_app():
    """Get or create and initialize the Telegram application."""
    global telegram_app
    if telegram_app is None:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("today", today_command))
        telegram_app.add_handler(CommandHandler("plan", plan_command))
        telegram_app.add_handler(CommandHandler("log", log_command))
        telegram_app.add_handler(CommandHandler("race", race_command))
        telegram_app.add_handler(CommandHandler("reset", reset_command))
        telegram_app.add_handler(CommandHandler("health", health_command))
        telegram_app.add_handler(CommandHandler("reconcile", reconcile_command))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        # Must initialize before process_update can be called.
        # Startup paths should fail fast; the 180s default is sized for
        # in-flight Telegram updates, not boot-time network calls.
        _run_async(telegram_app.initialize(), timeout=30)
    return telegram_app


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint for Railway — checks Redis and the LLM."""
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


@app.route("/strava/webhook", methods=["GET"])
def strava_webhook_verify():
    """Strava subscription handshake.

    Strava GETs callback_url with ?hub.mode=subscribe&hub.challenge=X&hub.verify_token=Y
    after we POST a subscription request. We verify the token matches our
    shared secret and echo the challenge back.
    """
    expected = os.getenv("STRAVA_VERIFY_TOKEN")
    got = request.args.get("hub.verify_token", "")
    if not expected or got != expected:
        logger.warning("Strava verify request rejected: token mismatch")
        return jsonify({"status": "forbidden"}), 403
    challenge = request.args.get("hub.challenge", "")
    return jsonify({"hub.challenge": challenge}), 200


@app.route("/strava/webhook", methods=["POST"])
def strava_webhook_event():
    """Receive a Strava activity event.

    Strava expects a 200 within 2s — we ack immediately and process in a
    background thread.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    logger.info(
        "Strava webhook event: aspect=%s object=%s id=%s",
        payload.get("aspect_type"),
        payload.get("object_type"),
        payload.get("object_id"),
    )
    try:
        from strava.handler import handle_event

        threading.Thread(target=handle_event, args=(payload,), daemon=True).start()
    except Exception as e:
        logger.error(f"Failed to dispatch Strava event: {e}")
    return jsonify({"status": "ok"}), 200


@app.route("/sessions/<int:session_id>/reflection", methods=["PUT"])
def put_session_reflection(session_id: int):
    """Bridge endpoint: the Notion Worker calls this when the athlete edits a
    Reflection property on a PRE Sessions page in Notion.

    Auth: ``Authorization: Bearer <WORKER_BRIDGE_SECRET>``. The same secret is
    configured on the Worker via ``ntn workers env set
    WORKER_BRIDGE_SECRET``. Without ``WORKER_BRIDGE_SECRET`` set on Railway,
    the endpoint refuses every request — no unauthenticated fallback.

    Body: ``{"reflection": "text"}`` or ``{"reflection": null}``. Empty /
    whitespace-only strings normalize to NULL (clears the cell). Returns 200
    with the new state, 404 when no session matches the id, 400 on malformed
    body, 401/403 on bad auth.

    Architecture: ``docs/notion-workers-architecture.md``.
    """
    expected = os.getenv("WORKER_BRIDGE_SECRET")
    if not expected:
        logger.warning("Reflection bridge called but WORKER_BRIDGE_SECRET is unset")
        return jsonify({"status": "forbidden"}), 403
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer ") :] != expected:
        logger.warning("Reflection bridge rejected: invalid bearer token")
        return jsonify({"status": "forbidden"}), 403

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict) or "reflection" not in payload:
        return jsonify({"status": "bad_request", "error": "missing 'reflection' field"}), 400
    raw = payload["reflection"]
    if raw is not None and not isinstance(raw, str):
        return jsonify({"status": "bad_request", "error": "'reflection' must be string or null"}), 400

    from state_manager import StateManager

    state = StateManager()
    updated = state.set_session_reflection(session_id, raw)
    logger.info(
        "reflection bridge: session_id=%s len=%s updated=%s",
        session_id,
        len(raw) if isinstance(raw, str) else 0,
        updated,
    )
    if not updated:
        return jsonify({"status": "not_found", "session_id": session_id}), 404
    return jsonify({"status": "ok", "session_id": session_id, "reflection": (raw or None)}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram webhook updates.

    Telegram requires a fast 2xx ack or it retries the same update_id on
    backoff. The companion agent's tool-use loop can take >30s on plan-edit
    turns, which previously blew gunicorn's worker timeout and caused
    Telegram retry storms (issues #15, #17). We now mirror the /strava/webhook
    pattern: parse, ack 200 immediately, and process the update in a daemon
    thread. The Telegram handlers themselves reply to the user via the bot
    API, so the response body here is just an HTTP ack.
    """
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
    except Exception:
        # Parse / init failure. Ack so Telegram doesn't retry forever, but
        # log the real traceback (the previous `f"Webhook error: {e}"` form
        # silently produced empty log lines for several exception types).
        logger.exception("Webhook parse/init error")
        return jsonify({"status": "ok"}), 200

    update_id = getattr(update, "update_id", None)

    def _process_in_background() -> None:
        try:
            _run_async(telegram_application.process_update(update))
        except Exception:
            logger.exception("Telegram update processing failed update_id=%s", update_id)

    threading.Thread(target=_process_in_background, daemon=True).start()
    return jsonify({"status": "ok"}), 200


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

    # Startup network call — fail fast rather than inherit the 180s default
    # _run_async ceiling (which is sized for in-flight Telegram update
    # processing, not boot-time setWebhook).
    _run_async(_set_webhook(), timeout=30)


# Set up webhook on startup
with app.app_context():
    setup_webhook()

# Start the in-process calendar watchdog (auto-enables in prod; no-op locally /
# in tests). Runs inside this worker so it shares the SQLite volume DB and the
# Redis token store. Import-time placement (not gunicorn on_starting) keeps it
# in the serving worker, alongside the asyncio loop and _SYNC_STATE_LOCK.
import calendar_health  # noqa: E402

calendar_health.start_scheduler_if_enabled()


if __name__ == "__main__":
    # Direct dev run (prod goes through gunicorn, which runs the cutover in
    # its on_starting hook). Mirror that here so `python app.py` is safe too.
    from pathlib import Path

    from scripts.cutover_to_unified_sessions import cutover

    _db = Path(os.getenv("DATABASE_PATH") or "state/coach.db")
    if _db.exists():
        cutover(_db)

    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
