"""COROS nightly pull scheduler + auth watchdog.

Structure copied from calendar_health.py (the proven in-process scheduler):
a daemon thread started at import time from app.py, gated to the Railway
container only, with exception-swallowing passes and a deduplicated Telegram
alert when auth dies.

The COROS twist: the pull is NIGHTLY (after the day's data exists), but the
loop is interval-based like calendar_health's. So the loop ticks every
~30 minutes and each tick runs a cheap due-check — due when local time is
past COROS_PULL_HOUR_LOCAL (default 22:00) and tonight's pull hasn't
succeeded yet. The success marker only advances on a clean pass, so a failed
pull retries on the next tick for the rest of the night, and the upsert's
idempotency makes an accidental double-pull harmless.

Run manually:
    ./venv/bin/python -m coros.scheduler            # one due-check-free pass
    ./venv/bin/python -m coros.scheduler --check    # classify auth only

Exit codes: 0 healthy · 2 needs re-auth · 3 infrastructure (e.g. Redis down).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coros import auth  # noqa: E402

logger = logging.getLogger("pre_coach.coros.scheduler")

ALERT_COOLDOWN_HOURS = float(os.getenv("COROS_HEALTH_ALERT_COOLDOWN_HOURS", "24"))
# Per-alert-type dedup keys: the re-auth alert ("run make coros-reauth-prod")
# and the data-staleness alert ("check COROS outage / format change") demand
# different operator actions, so one firing must not suppress the other for
# a cooldown window.
_AUTH_ALERT_KEY = "coros:health:last_alert:auth"
_STALENESS_ALERT_KEY = "coros:health:last_alert:staleness"
_LAST_RUN_REDIS_KEY = "coros:nightly:last_run_date"

EXIT_OK = 0
EXIT_NEEDS_AUTH = 2
EXIT_INFRA = 3

_REAUTH_CMD = "make coros-reauth-prod"

DEFAULT_INTERVAL_MINUTES = 30.0
DEFAULT_PULL_HOUR_LOCAL = 22
_SCHEDULER_INITIAL_DELAY_SECONDS = 90.0
_DISABLE_FLAG = "DISABLE_COROS_SCHEDULER"
_scheduler_thread: threading.Thread | None = None


def classify_auth() -> tuple[str, str]:
    """Return (status, detail) where status is 'ok' | 'needs_auth' | 'infra'.

    Same taxonomy as calendar_health.classify_auth: a dead refresh token
    (needs_auth — re-run the OAuth flow) is NOT the same as an unreachable
    token store (infra — Redis down; tokens aren't lost, so prompting a
    re-auth would needlessly burn a working grant).
    """
    try:
        blob = auth._read_blob()
    except auth.TokenStorageUnavailable as e:
        return ("infra", str(e))

    tokens = (blob or {}).get("tokens") or {}
    if not tokens.get("refresh_token"):
        return ("needs_auth", "no COROS tokens stored")

    try:
        auth.get_access_token()
    except auth.CorosAuthError as e:
        # get_access_token() re-wraps a storage outage as CorosAuthError
        # (raised `from` TokenStorageUnavailable) — that's infra, not a
        # re-auth prompt. Check the cause structurally; the substring is a
        # belt-and-suspenders fallback only.
        if isinstance(e.__cause__, auth.TokenStorageUnavailable) or "storage unavailable" in str(e).lower():
            return ("infra", str(e))
        return ("needs_auth", str(e))
    except Exception as e:  # network blip, unexpected shape, etc.
        return ("infra", str(e))

    return ("ok", "")


def _alert_text() -> str:
    return (
        "⚠️ PRE's COROS sync is down.\n\n"
        "COROS auth needs to be refreshed — sleep/HRV/recovery data has "
        "stopped flowing into the nightly readiness check.\n\n"
        f"Re-auth (≈30s):\n  {_REAUTH_CMD}"
    )


def _should_alert(now: float, key: str = _AUTH_ALERT_KEY) -> bool:
    """True if we're outside the alert cooldown for this alert type. Fails
    OPEN: if the dedup store is unreachable we alert anyway — over-notifying
    beats silence."""
    try:
        from conversation_store import _get_redis

        last = _get_redis().get(key)
    except Exception as e:
        logger.warning(f"Alert-dedup read failed ({e}); alerting anyway")
        return True
    if not last:
        return True
    try:
        return (now - float(last)) >= ALERT_COOLDOWN_HOURS * 3600
    except (TypeError, ValueError):
        return True


def _record_alert(now: float, key: str = _AUTH_ALERT_KEY) -> None:
    try:
        from conversation_store import _get_redis

        _get_redis().set(key, str(int(now)))
    except Exception as e:
        logger.warning(f"Could not record alert timestamp: {e}")


def _clear_alert_state() -> None:
    """Called after a healthy run so the next failure alerts immediately."""
    try:
        from conversation_store import _get_redis

        _get_redis().delete(_AUTH_ALERT_KEY, _STALENESS_ALERT_KEY)
    except Exception as e:
        logger.debug(f"Could not clear alert state: {e}")


def _send_alert(now: float) -> bool:
    """Send the re-auth alert if outside the cooldown. Returns True if sent."""
    if not _should_alert(now):
        logger.info("COROS auth alert suppressed (within cooldown window)")
        return False
    from strava.notify import send_telegram_text

    # mirror=False: ops alert, not part of the coaching dialogue.
    sent = send_telegram_text(_alert_text(), mirror=False)
    if sent:
        _record_alert(now)
        logger.info("Sent COROS re-auth alert to Telegram")
    else:
        logger.warning("COROS re-auth alert could not be sent (Telegram unconfigured?)")
    return sent


# ---------- nightly due-check ----------


def _pull_hour(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        hour = int(env.get("COROS_PULL_HOUR_LOCAL", DEFAULT_PULL_HOUR_LOCAL))
    except (TypeError, ValueError):
        hour = DEFAULT_PULL_HOUR_LOCAL
    return min(max(hour, 0), 23)


def _read_last_run_date() -> Optional[str]:
    """Read the last successful pull's local date. Fails OPEN (None) — an
    unreadable marker means we pull again, and the upsert is idempotent."""
    try:
        from conversation_store import _get_redis

        raw = _get_redis().get(_LAST_RUN_REDIS_KEY)
        return raw.decode() if isinstance(raw, bytes) else raw
    except Exception as e:
        logger.warning(f"Could not read COROS last-run marker ({e}); treating as due")
        return None


def _record_last_run_date(date_iso: str) -> None:
    try:
        from conversation_store import _get_redis

        _get_redis().set(_LAST_RUN_REDIS_KEY, date_iso)
    except Exception as e:
        logger.warning(f"Could not record COROS last-run marker: {e}")


def _is_due(now_local: datetime, last_run_date: Optional[str], pull_hour: int) -> bool:
    """Pure due-check: past tonight's pull hour and not yet run today."""
    if now_local.hour < pull_hour:
        return False
    return last_run_date != now_local.date().isoformat()


# ---------- one watchdog/pull pass ----------


def run(now: float | None = None, dry_run: bool = False, do_pull: bool = True) -> int:
    """Execute one pass: classify auth, then pull if healthy.

    Returns a process exit code. The caller (scheduler loop or CLI) owns the
    due-check; this function always pulls when asked.
    """
    now = time.time() if now is None else now

    status, detail = classify_auth()

    if status == "infra":
        logger.error(f"COROS token storage unavailable: {detail}")
        print(f"infra: token storage unavailable — {detail}", file=sys.stderr)
        _attempt_staleness_alert(now)
        return EXIT_INFRA

    if status == "needs_auth":
        logger.warning(f"COROS auth needs re-doing: {detail}")
        print(f"needs_auth: {detail}\n  fix: {_REAUTH_CMD}", file=sys.stderr)
        _send_alert(now)
        return EXIT_NEEDS_AUTH

    if not do_pull:
        print("auth ok")
        _clear_alert_state()
        return EXIT_OK

    try:
        from coros import ingest
        from state_manager import StateManager

        state = StateManager(ROOT / "state")
        result = ingest.run_nightly_pull(state, dry_run=dry_run)
    except auth.CorosAuthError as e:
        # Token died between the classify check and the pull.
        logger.warning(f"COROS auth failed mid-pull: {e}")
        print(f"needs_auth: {e}\n  fix: {_REAUTH_CMD}", file=sys.stderr)
        _send_alert(now)
        return EXIT_NEEDS_AUTH
    except Exception as e:
        logger.error(f"COROS pull failed: {e}")
        print(f"infra: pull failed — {e}", file=sys.stderr)
        _attempt_staleness_alert(now)
        return EXIT_INFRA

    print(
        f"{'[dry-run] ' if dry_run else ''}auth ok · pull dates={len(result['dates'])} fields={result['fields_parsed']}"
    )
    for err in result.get("errors", []):
        # Partial-tool problems are diagnostics; zero-data is a failure (below).
        logger.warning(f"COROS pull issue: {err}")

    if not result.get("ok", bool(result["dates"])):
        # Nothing usable parsed (format change / all tools down). Fail the
        # pass so the success marker doesn't advance — the loop retries
        # tonight, and the staleness alert fires if it keeps happening.
        print("infra: pull produced no usable data", file=sys.stderr)
        _maybe_send_staleness_alert(state, now)
        return EXIT_INFRA

    if not dry_run and result["dates"]:
        _run_readiness_checkin(state)

    _clear_alert_state()
    return EXIT_OK


def _attempt_staleness_alert(now: float) -> None:
    """Infra-failure paths deserve the staleness alert too — without it a
    persistent pull exception loops nightly with no Telegram signal (exit
    codes only reach logs). Guarded: whatever broke the pass may also break
    StateManager construction."""
    try:
        from state_manager import StateManager

        _maybe_send_staleness_alert(StateManager(ROOT / "state"), now)
    except Exception as e:
        logger.warning(f"Could not attempt staleness alert: {e}")


def _maybe_send_staleness_alert(state, now: float) -> None:
    """Alert (cooldown-deduped) when pulls have failed long enough that
    readiness data is going stale — the non-auth analog of the re-auth
    alert. Without it, persistent infra failures are invisible: exit codes
    only reach logs, and the quiet-night ping policy makes silence look
    healthy."""
    try:
        from temporal_context import today_local

        # Only PARSED metrics count as fresh: ingest's raw-insurance row is
        # written even when zero fields parse, so mere row existence would
        # keep this alert permanently suppressed during the format-change
        # scenario it exists for.
        latest = state.latest_metric_date()
        if latest and date.fromisoformat(latest) >= today_local() - timedelta(days=1):
            return  # metrics still fresh — one failed night isn't alertable
        if not _should_alert(now, _STALENESS_ALERT_KEY):
            return
        from strava.notify import send_telegram_text

        sent = send_telegram_text(
            "⚠️ PRE's COROS pull has been failing — no fresh readiness data "
            "for 2+ days (auth is fine; likely a COROS outage or output "
            "format change). Check logs: railway logs | grep -i coros",
            mirror=False,
        )
        if sent:
            _record_alert(now, _STALENESS_ALERT_KEY)
    except Exception:
        logger.exception("COROS staleness alert failed")


def _run_readiness_checkin(state) -> None:
    """Phase 2: nightly LLM check of tomorrow's plan against tonight's
    vitals. Best-effort — a failure here never fails the pull pass (the
    data is already stored; the check-in re-runs tomorrow night)."""
    try:
        from coros.review import run_readiness_review

        text = run_readiness_review(state)
        if text:
            from strava.notify import send_telegram_text

            send_telegram_text(text, mirror=True)
            logger.info("Sent nightly readiness check-in to Telegram")
    except Exception:
        logger.exception("Readiness check-in failed (pull already persisted)")


# ---------- in-process scheduler ----------


def scheduler_enabled(env: Mapping[str, str]) -> bool:
    """True when the nightly scheduler should run in this process.

    Same gating philosophy as calendar_health.scheduler_enabled: Railway-only
    (a Railway-injected runtime var is the prod discriminator, so pytest /
    local runs never pull into the wrong DB), needs TELEGRAM_BOT_TOKEN for
    the failure alert, always off under TESTING, force-off with
    DISABLE_COROS_SCHEDULER=1. Deliberately does NOT require CALENDAR_ID —
    COROS is independent of the calendar integration.
    """
    if (env.get("TESTING") or "").lower() in ("1", "true"):
        return False
    if (env.get(_DISABLE_FLAG) or "").lower() in ("1", "true"):
        return False
    on_railway = bool(
        env.get("RAILWAY_ENVIRONMENT") or env.get("RAILWAY_SERVICE_NAME") or env.get("RAILWAY_PROJECT_ID")
    )
    return bool(on_railway and env.get("TELEGRAM_BOT_TOKEN"))


def _interval_seconds(env: Mapping[str, str]) -> float:
    try:
        minutes = float(env.get("COROS_SCHEDULER_INTERVAL_MINUTES", DEFAULT_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_INTERVAL_MINUTES
    # Floor guards against a 0/negative value turning the loop into a busy-spin.
    return max(minutes, 5.0) * 60.0


def _tick_once_safely() -> int | None:
    """One scheduler tick: due-check, then a full pass if due. Swallows any
    error so the loop thread can never die. Returns the pass's exit code,
    None when not due or when the tick raised."""
    try:
        from temporal_context import now_local

        local_now = now_local()
        if not _is_due(local_now, _read_last_run_date(), _pull_hour()):
            return None
        code = run(do_pull=True)
        if code == EXIT_OK:
            _record_last_run_date(local_now.date().isoformat())
        logger.info("COROS nightly pass complete (exit=%s)", code)
        return code
    except Exception:
        logger.exception("COROS scheduler tick raised; will retry next interval")
        return None


def _scheduler_loop(interval_seconds: float, *, _max_iterations: int | None = None) -> None:
    """Sleep briefly to let the worker finish booting, then tick every
    `interval_seconds`. `_max_iterations` bounds the loop for tests only."""
    time.sleep(_SCHEDULER_INITIAL_DELAY_SECONDS)
    count = 0
    while True:
        _tick_once_safely()
        count += 1
        if _max_iterations is not None and count >= _max_iterations:
            return
        time.sleep(interval_seconds)


def start_scheduler_if_enabled(env: Mapping[str, str] | None = None) -> threading.Thread | None:
    """Start the nightly-pull daemon thread once if gating is satisfied.
    Called at import time from app.py (single gunicorn worker → runs exactly
    once). Returns the thread, or None when disabled."""
    global _scheduler_thread
    env = os.environ if env is None else env
    if not scheduler_enabled(env):
        logger.info("COROS scheduler disabled (gating not met)")
        return None
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return _scheduler_thread
    interval = _interval_seconds(env)
    t = threading.Thread(
        target=_scheduler_loop,
        args=(interval,),
        daemon=True,
        name="coros-nightly-pull",
    )
    _scheduler_thread = t
    t.start()
    logger.info(
        "COROS scheduler started (tick=%.0fs, pull hour=%02d:00 local)",
        interval,
        _pull_hour(),
    )
    return t


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(description="COROS nightly pull: check auth, pull daily health, alert on failure")
    p.add_argument("--dry-run", action="store_true", help="Fetch + parse but write nothing")
    p.add_argument("--check", action="store_true", help="Classify auth only; skip the pull")
    args = p.parse_args()
    return run(dry_run=args.dry_run, do_pull=not args.check)


if __name__ == "__main__":
    sys.exit(main())
