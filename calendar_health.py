"""Calendar health watchdog: keep the training calendar in a healthy state.

Designed to run unattended on a schedule (Railway cron). Each run:

  1. Classifies Google Calendar auth as ok / needs_auth / infra.
  2. If auth is OK, runs a full plan→calendar sync sweep. This is the piece
     the event-driven sync (plan edits + Strava webhooks) can't guarantee:
     if neither fires, the calendar drifts from the SQLite source of truth
     with nothing to self-correct. The sweep reconciles that drift.
  3. If auth needs re-doing — the ONE thing no script can auto-fix while the
     OAuth app is unverified — sends a deduplicated Telegram alert with the
     exact re-auth command, so the calendar never silently rots again.

Root-cause note: the recurring "auth expired" pain comes from the OAuth
consent screen being in *testing* mode (refresh tokens die after 7 days).
Publishing the app to production stops that. See docs/calendar-health.md.

Run:
    ./venv/bin/python calendar_health.py            # check + sync + alert
    ./venv/bin/python calendar_health.py --dry-run  # check + plan ops, no writes
    ./venv/bin/python calendar_health.py --check     # classify auth only, no sync

Exit codes: 0 healthy · 2 needs re-auth · 3 infrastructure (e.g. Redis down).
These let a scheduler/log surface failures distinctly from a routine pass.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google_calendar import auth  # noqa: E402

logger = logging.getLogger("pre_coach.calendar_health")

# Don't re-alert more than once per this window while auth stays broken — a
# cron running every few hours would otherwise spam the chat. Cleared on the
# first healthy run so the next genuine failure alerts immediately.
ALERT_COOLDOWN_HOURS = float(os.getenv("GCAL_HEALTH_ALERT_COOLDOWN_HOURS", "12"))
_ALERT_REDIS_KEY = "gcal:health:last_alert"

# Exit codes (see module docstring).
EXIT_OK = 0
EXIT_NEEDS_AUTH = 2
EXIT_INFRA = 3

_REAUTH_CMD = "python scripts/google_calendar_setup.py auth"

# In-process scheduler (see start_scheduler_if_enabled). Runs inside the web
# worker so it shares the SQLite volume DB ($DATABASE_PATH) and the prod Redis
# token store — a separate Railway service could not (volumes are single-attach).
DEFAULT_INTERVAL_HOURS = 6.0
_SCHEDULER_INITIAL_DELAY_SECONDS = 60.0
_DISABLE_FLAG = "DISABLE_CALENDAR_HEALTH_SCHEDULER"
_scheduler_thread: threading.Thread | None = None


def classify_auth() -> tuple[str, str]:
    """Return (status, detail) where status is 'ok' | 'needs_auth' | 'infra'.

    Distinguishes a genuinely dead/expired refresh token (needs_auth — the
    user must re-run the OAuth flow) from a token store that's merely
    unreachable (infra — Redis down; tokens are NOT lost, so telling the user
    to re-auth would be wrong and destructive of a working grant).
    """
    try:
        tokens = auth._read_tokens()
    except auth.TokenStorageUnavailable as e:
        return ("infra", str(e))

    if not tokens or "refresh_token" not in tokens:
        return ("needs_auth", "no Gcal tokens stored")

    try:
        auth.get_access_token()
    except auth.GcalAuthError as e:
        msg = str(e)
        # get_access_token() re-wraps a storage outage as GcalAuthError; keep
        # treating that as infra, not a re-auth prompt.
        if "storage unavailable" in msg.lower():
            return ("infra", msg)
        return ("needs_auth", msg)
    except Exception as e:  # network blip, unexpected shape, etc.
        return ("infra", str(e))

    return ("ok", "")


def _alert_text() -> str:
    return (
        "⚠️ PRE calendar sync is down.\n\n"
        "Google Calendar auth needs to be refreshed — your training plan has "
        "stopped syncing to the calendar.\n\n"
        f"Re-auth (≈30s):\n  {_REAUTH_CMD}\n\n"
        "If this keeps happening every ~7 days, publish the OAuth app to "
        "production (see docs/calendar-health.md) — that ends the recurring "
        "expiry for good."
    )


def _should_alert(now: float) -> bool:
    """True if we're outside the alert cooldown. Fails OPEN: if the dedup
    store is unreachable we alert anyway — over-notifying beats silence."""
    try:
        from conversation_store import _get_redis

        last = _get_redis().get(_ALERT_REDIS_KEY)
    except Exception as e:
        logger.warning(f"Alert-dedup read failed ({e}); alerting anyway")
        return True
    if not last:
        return True
    try:
        return (now - float(last)) >= ALERT_COOLDOWN_HOURS * 3600
    except (TypeError, ValueError):
        return True


def _record_alert(now: float) -> None:
    try:
        from conversation_store import _get_redis

        _get_redis().set(_ALERT_REDIS_KEY, str(int(now)))
    except Exception as e:
        logger.warning(f"Could not record alert timestamp: {e}")


def _clear_alert_state() -> None:
    """Called after a healthy run so the next failure alerts immediately."""
    try:
        from conversation_store import _get_redis

        _get_redis().delete(_ALERT_REDIS_KEY)
    except Exception as e:
        logger.debug(f"Could not clear alert state: {e}")


def _send_alert(now: float) -> bool:
    """Send the re-auth alert if outside the cooldown. Returns True if sent."""
    if not _should_alert(now):
        logger.info("Auth alert suppressed (within cooldown window)")
        return False
    from strava.notify import send_telegram_text

    # mirror=False: this is an ops alert, not part of the coaching dialogue.
    sent = send_telegram_text(_alert_text(), mirror=False)
    if sent:
        _record_alert(now)
        logger.info("Sent calendar re-auth alert to Telegram")
    else:
        logger.warning("Calendar re-auth alert could not be sent (Telegram unconfigured?)")
    return sent


def _run_sync(dry_run: bool) -> dict:
    from google_calendar import sync
    from state_manager import StateManager

    state = StateManager(ROOT / "state")
    return sync.sync_plan(state, dry_run=dry_run)


def run(dry_run: bool = False, do_sync: bool = True, now: float | None = None) -> int:
    """Execute one watchdog pass. Returns a process exit code."""
    now = time.time() if now is None else now

    status, detail = classify_auth()

    if status == "infra":
        logger.error(f"Calendar token storage unavailable: {detail}")
        print(f"infra: token storage unavailable — {detail}", file=sys.stderr)
        # Not the user's problem to fix by re-authing; surface to ops via logs.
        return EXIT_INFRA

    if status == "needs_auth":
        logger.warning(f"Calendar auth needs re-doing: {detail}")
        print(f"needs_auth: {detail}\n  fix: {_REAUTH_CMD}", file=sys.stderr)
        _send_alert(now)
        return EXIT_NEEDS_AUTH

    # status == "ok"
    if not do_sync:
        print("auth ok")
        _clear_alert_state()
        return EXIT_OK

    try:
        result = _run_sync(dry_run)
    except auth.GcalAuthError as e:
        # Token died between the classify check and the sync write.
        logger.warning(f"Auth failed mid-sync: {e}")
        print(f"needs_auth: {e}\n  fix: {_REAUTH_CMD}", file=sys.stderr)
        _send_alert(now)
        return EXIT_NEEDS_AUTH
    except Exception as e:
        logger.error(f"Sync sweep failed: {e}")
        print(f"infra: sync failed — {e}", file=sys.stderr)
        return EXIT_INFRA

    print(
        f"{'[dry-run] ' if dry_run else ''}auth ok · sync "
        f"inserted={result['inserted']} patched={result['patched']} "
        f"deleted={result['deleted']} unchanged={result['unchanged']}"
    )
    if result.get("errors"):
        # Per-date API errors are transient (not auth) — log, don't alert.
        for err in result["errors"]:
            logger.warning(f"sync error {err['date']}: {err['error']}")

    _clear_alert_state()
    return EXIT_OK


# ---------- in-process scheduler ----------


def scheduler_enabled(env: Mapping[str, str]) -> bool:
    """True when the periodic watchdog should run in this process.

    Auto-enables in prod with no manual flag: requires WEBHOOK_URL (set only in
    the deployed web service — never in pytest, local dev, or the `flask` CLI)
    plus CALENDAR_ID and TELEGRAM_BOT_TOKEN (so it can actually sync and alert).
    Force-off with DISABLE_CALENDAR_HEALTH_SCHEDULER=1; always off under TESTING.
    """
    if (env.get("TESTING") or "").lower() in ("1", "true"):
        return False
    if (env.get(_DISABLE_FLAG) or "").lower() in ("1", "true"):
        return False
    return bool(env.get("WEBHOOK_URL") and env.get("CALENDAR_ID") and env.get("TELEGRAM_BOT_TOKEN"))


def _interval_seconds(env: Mapping[str, str]) -> float:
    try:
        hours = float(env.get("CALENDAR_HEALTH_INTERVAL_HOURS", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_INTERVAL_HOURS
    # Floor guards against a 0/negative value turning the loop into a busy-spin.
    return max(hours, 0.25) * 3600.0


def _run_once_safely() -> int | None:
    """Run one watchdog pass, swallowing any error so the loop thread can never
    die. Returns the exit code, or None if the pass raised."""
    try:
        code = run(do_sync=True)
        logger.info("calendar watchdog pass complete (exit=%s)", code)
        return code
    except Exception:
        logger.exception("calendar watchdog pass raised; will retry next interval")
        return None


def _scheduler_loop(interval_seconds: float, *, _max_iterations: int | None = None) -> None:
    """Sleep briefly to let the worker finish booting, then sweep every
    `interval_seconds`. `_max_iterations` bounds the loop for tests only."""
    time.sleep(_SCHEDULER_INITIAL_DELAY_SECONDS)
    count = 0
    while True:
        _run_once_safely()
        count += 1
        if _max_iterations is not None and count >= _max_iterations:
            return
        time.sleep(interval_seconds)


def start_scheduler_if_enabled(env: Mapping[str, str] | None = None) -> threading.Thread | None:
    """Start the watchdog daemon thread once if gating is satisfied. Called at
    import time from app.py (single gunicorn worker → runs exactly once).
    Returns the thread, or None when disabled."""
    global _scheduler_thread
    env = os.environ if env is None else env
    if not scheduler_enabled(env):
        logger.info("calendar watchdog scheduler disabled (gating not met)")
        return None
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return _scheduler_thread
    interval = _interval_seconds(env)
    t = threading.Thread(
        target=_scheduler_loop,
        args=(interval,),
        daemon=True,
        name="calendar-health-watchdog",
    )
    _scheduler_thread = t
    t.start()
    logger.info("calendar watchdog scheduler started (interval=%.0fs)", interval)
    return t


def main() -> int:
    p = argparse.ArgumentParser(description="Calendar health watchdog: check auth, sweep-sync, alert on failure")
    p.add_argument("--dry-run", action="store_true", help="Run the sync sweep in dry-run (log ops, no writes)")
    p.add_argument("--check", action="store_true", help="Classify auth only; skip the sync sweep")
    args = p.parse_args()
    return run(dry_run=args.dry_run, do_sync=not args.check)


if __name__ == "__main__":
    sys.exit(main())
