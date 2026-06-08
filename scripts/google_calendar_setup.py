"""One-time Google Calendar setup CLI + ongoing diagnostic tool.

Subcommands:
    auth        Walk through OAuth via a one-shot loopback HTTP listener,
                store tokens in the configured backend. Pass --no-listener
                (or --code <code>) to use the OOB paste-code flow inside a
                container shell where the loopback redirect can't work.
    status      Show env, tokens, calendar metadata, sync state.
    sync        Push plan.md weekly table to the PRE Training calendar.
    purge       Delete every pre_managed event in [today-365d, today+365d].
                Useful for resetting during dev or backing out the integration.

GCP setup (one-time, manual):
    1. https://console.cloud.google.com → enable Google Calendar API
    2. OAuth consent screen → External, add yourself as a Test User
    3. Create OAuth client → application type **Desktop app** (this gives you
       a client_id/client_secret with http://127.0.0.1 as a valid redirect)
    4. Copy client_id and client_secret into .env as GCAL_CLIENT_ID and
       GCAL_CLIENT_SECRET
    5. In the Google Calendar UI: create a new "PRE Training" calendar →
       Settings → "Integrate calendar" → copy the Calendar ID into .env as
       CALENDAR_ID

Known gotcha: while the OAuth consent screen is in "testing" mode, refresh
tokens for unverified apps expire after 7 days. When you next see a refresh
error, re-run `python scripts/google_calendar_setup.py auth`.

To END this recurring expiry, *publish* the app (Cloud Console → OAuth consent
screen → "Publish App"). Publishing to production — even WITHOUT going through
Google verification — stops the 7-day cap; refresh tokens then persist until
revoked. You'll click through a one-time "unverified app" warning. Full
verification is only needed to drop that warning / exceed 100 users. See
docs/calendar-health.md. The `calendar_health.py` watchdog Telegram-alerts you
if a token ever does die so the calendar never silently stops syncing.

Usage:
    ./venv/bin/python scripts/google_calendar_setup.py auth
    ./venv/bin/python scripts/google_calendar_setup.py auth --no-listener
    ./venv/bin/python scripts/google_calendar_setup.py auth --code "<code>"
    ./venv/bin/python scripts/google_calendar_setup.py status
    ./venv/bin/python scripts/google_calendar_setup.py sync --dry-run
    ./venv/bin/python scripts/google_calendar_setup.py sync
"""

from __future__ import annotations

import argparse
import http.server
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from google_calendar import auth, client, sync  # noqa: E402

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 8765
REDIRECT_URI = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}/"
# OOB ("out of band") redirect: Google shows the code on a page and the user
# pastes it into the terminal. Used by the --no-listener / --code flow for
# container shells (e.g. `railway shell`) where the loopback redirect can't
# work because the browser runs on the laptop, not the container.
#
# Deprecation note: Google announced the deprecation of the OOB redirect
# (urn:ietf:wg:oauth:2.0:oob) on 2022-10-03, and newer OAuth Desktop clients
# may reject it with HTTP 400 invalid_request at the token endpoint. If that
# happens, the fallback is the default listener flow (run `auth` without
# --no-listener / --code) from a host that can reach 127.0.0.1.
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
SCOPE = "https://www.googleapis.com/auth/calendar.events"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


# ---------- printing helpers ----------


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ! {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def _section(title: str) -> None:
    print(f"\n{title}")


# ---------- auth ----------


class _OAuthHandler(http.server.BaseHTTPRequestHandler):
    """One-shot handler for the OAuth callback. Stashes (code, state) on the
    server instance, then 200s with a friendly close-this-tab message."""

    def do_GET(self):  # noqa: N802 — stdlib name
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        # Browsers (Chrome especially) sometimes preload /favicon.ico before
        # following the OAuth redirect. If we accept that as "the" callback,
        # we'd miss the real ?code=... request. Only treat requests carrying
        # the OAuth response params as the callback we're waiting for.
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return
        self.server.received = params  # type: ignore[attr-defined]
        body = (
            b"<html><body style='font-family: sans-serif; padding: 2em;'>"
            b"<h2>Authorization received.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        # Silence default request logging — the user already sees CLI output.
        pass


def _build_auth_url(client_id: str, redirect_uri: str, state_token: str) -> str:
    """Build the Google OAuth authorize URL.

    Factored out so the OOB / loopback redirect_uri branch can be unit-tested
    without standing up an HTTP server or doing any network I/O.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # `prompt=consent` forces issuance of a refresh_token even if the user
        # has previously authorized this client. Without it Google will skip
        # the consent screen on re-auth and not return a refresh_token.
        "prompt": "consent",
        "state": state_token,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _exchange_and_report(code: str, redirect_uri: str) -> int:
    """Exchange `code` for tokens and print a result line. Shared by both flows."""
    try:
        auth.exchange_code_for_tokens(code, redirect_uri)
    except Exception as e:
        print(f"\nAuth failed: {e}", file=sys.stderr)
        # OOB redirect was deprecated 2022-10-03; newer Desktop OAuth clients
        # reject it at the token endpoint with HTTP 4xx + error=invalid_request.
        msg = str(e)
        if redirect_uri == OOB_REDIRECT_URI and "invalid_request" in msg and "400" in msg:
            print(
                "\nHint: Google rejected the OOB redirect_uri. This auth method "
                "is deprecated. Run without --no-listener from a host that can "
                "reach 127.0.0.1, or use a PKCE loopback flow.",
                file=sys.stderr,
            )
        print(
            "\nCommon causes:\n"
            "  - GCAL_CLIENT_SECRET is wrong\n"
            "  - The OAuth client in GCP is not type 'Desktop app'\n"
            "  - You revoked the app at https://myaccount.google.com/permissions\n"
            "    but Google didn't return a refresh_token (re-run; prompt=consent\n"
            "    forces it but the consent screen must actually appear)\n"
            "  - The code was already used or expired (single-use, short TTL)",
            file=sys.stderr,
        )
        return 1
    print(f"\n✓ Wrote tokens to {auth._backend()} backend.")
    return 0


def _cmd_auth_oob(provided_code: Optional[str]) -> int:
    """Non-listener flow for non-interactive container shells.

    Uses the OOB redirect_uri so Google displays the code on a page the user
    can copy. We either accept `--code` directly or prompt via input(). No
    HTTP listener is bound — works inside `railway shell` etc.
    """
    cid = os.getenv("GCAL_CLIENT_ID")
    secret = os.getenv("GCAL_CLIENT_SECRET")
    if not cid or not secret:
        print("GCAL_CLIENT_ID and GCAL_CLIENT_SECRET must be set in .env", file=sys.stderr)
        return 1

    if provided_code:
        return _exchange_and_report(provided_code, OOB_REDIRECT_URI)

    state_token = secrets.token_urlsafe(24)
    url = _build_auth_url(cid, OOB_REDIRECT_URI, state_token)
    print(
        f"\nBackend: {auth._backend()}\n\n"
        "1. Open this URL in your browser and authorize:\n\n"
        f"   {url}\n\n"
        "2. Google will display the authorization code. Copy it.\n"
    )
    try:
        code = input("Paste code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return 1
    if not code:
        print("No code provided.", file=sys.stderr)
        return 1
    return _exchange_and_report(code, OOB_REDIRECT_URI)


def cmd_auth(args: argparse.Namespace) -> int:
    # Container/non-interactive shells: skip the loopback listener entirely.
    # Using `--code` implies `--no-listener` since the listener serves no
    # purpose when the code is already in hand.
    if getattr(args, "code", None) or getattr(args, "no_listener", False):
        return _cmd_auth_oob(getattr(args, "code", None))

    cid = os.getenv("GCAL_CLIENT_ID")
    secret = os.getenv("GCAL_CLIENT_SECRET")
    if not cid or not secret:
        print("GCAL_CLIENT_ID and GCAL_CLIENT_SECRET must be set in .env", file=sys.stderr)
        return 1

    state_token = secrets.token_urlsafe(24)
    url = _build_auth_url(cid, REDIRECT_URI, state_token)

    try:
        server = http.server.HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _OAuthHandler)
    except OSError as e:
        print(
            f"Could not bind {LOOPBACK_HOST}:{LOOPBACK_PORT}: {e}\n"
            "Another process may already be listening. Stop it and retry.",
            file=sys.stderr,
        )
        return 1

    server.received = None  # type: ignore[attr-defined]
    print(
        f"\nBackend: {auth._backend()}\n"
        f"Listening on {REDIRECT_URI} for one OAuth callback.\n\n"
        "Open this URL in your browser and authorize:\n\n"
        f"   {url}\n"
    )
    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        # Loop until we get the actual OAuth callback (not /favicon.ico etc).
        # _OAuthHandler.do_GET only sets `received` when the request carries
        # `code` or `error`; other paths get a 404 and we keep listening.
        while server.received is None:  # type: ignore[attr-defined]
            server.handle_request()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 1
    finally:
        server.server_close()

    received = server.received  # type: ignore[attr-defined]
    if not received:
        print("No response received.", file=sys.stderr)
        return 1
    if received.get("error"):
        print(f"OAuth error: {received['error']}", file=sys.stderr)
        return 1
    if received.get("state") != state_token:
        print("OAuth state mismatch — aborting (possible CSRF).", file=sys.stderr)
        return 1
    code = received.get("code")
    if not code:
        print("No code in callback.", file=sys.stderr)
        return 1

    return _exchange_and_report(code, REDIRECT_URI)


# ---------- status ----------


def cmd_status(_: argparse.Namespace) -> int:
    rc = 0
    print("=== Google Calendar integration status ===")

    _section("Environment:")
    env_groups = [
        ("Required for OAuth", ["GCAL_CLIENT_ID", "GCAL_CLIENT_SECRET"]),
        ("Required for sync", ["CALENDAR_ID"]),
        ("Token storage", ["GCAL_TOKENS_BACKEND"]),
    ]
    for label, names in env_groups:
        print(f"  [{label}]")
        for n in names:
            v = os.getenv(n)
            if v:
                _ok(f"{n} set")
            elif n == "GCAL_TOKENS_BACKEND":
                _warn(f"{n} unset — defaulting to 'file' (local dev OK; Railway should be 'redis')")
            else:
                _fail(f"{n} MISSING")
                rc = 1

    _section("Token storage:")
    backend = auth._backend()
    print(f"  backend: {backend}")
    try:
        try:
            tokens = auth._read_tokens()
        except auth.TokenStorageUnavailable as e:
            _fail(f"Storage unreachable: {e}")
            print("    -> Infrastructure issue, not auth. Verify REDIS_URL connectivity.")
            rc = 1
            tokens = None

        if tokens is None:
            _warn("No tokens stored. Run: python scripts/google_calendar_setup.py auth")
            rc = 1
        elif "refresh_token" not in tokens:
            _fail("Token blob present but missing refresh_token (corrupt). Re-auth.")
            rc = 1
        else:
            exp = int(tokens.get("expires_at") or 0)
            remaining = exp - int(time.time())
            _ok(f"refresh_token present (length {len(tokens['refresh_token'])})")
            _ok(f"access_token present, expires in {remaining}s ({remaining // 60}m)")
            if remaining < auth.REFRESH_LEEWAY_SECONDS:
                _warn("access_token within refresh leeway — next call will refresh")
    except auth.GcalAuthError as e:
        _fail(f"Auth check failed: {e}")
        rc = 1

    _section("Google Calendar API:")
    cid = os.getenv("CALENDAR_ID") or ""
    if not cid:
        _fail("CALENDAR_ID unset — skipping API round-trip")
        rc = 1
    else:
        # We deliberately don't call GET /calendars/{cid}: that endpoint needs
        # the broader `calendar.readonly` scope, while we only request the
        # narrower `calendar.events` scope (which lets us read+write events
        # but not calendar metadata). Listing our own managed events is a
        # tighter check anyway — it confirms both calendar id correctness
        # and scope sufficiency in one round-trip.
        try:
            from datetime import timedelta

            from temporal_context import today_local

            today = today_local()
            tmin = f"{(today - timedelta(days=60)).isoformat()}T00:00:00Z"
            tmax = f"{(today + timedelta(days=60)).isoformat()}T00:00:00Z"
            managed = client.list_managed_events(tmin, tmax)
            _ok(f"calendar reachable; {len(managed)} managed events in ±60d window")
        except client.GcalCalendarMissingError as e:
            _fail(f"Calendar not found: {e}")
            rc = 1
        except Exception as e:
            _fail(f"list_managed_events failed: {e}")
            rc = 1

    _section("Local sync state:")
    summary = sync.get_last_sync_summary()
    if summary:
        _ok(f"{summary['count']} entries; last synced at {summary['last_synced_at']}")
        print(f"    source: {summary['source']}")
    else:
        _warn("No sync state yet. Run: python scripts/google_calendar_setup.py sync")

    print()
    return rc


# ---------- sync ----------


def cmd_sync(args: argparse.Namespace) -> int:
    from state_manager import StateManager

    state = StateManager(ROOT / "state")
    try:
        result = sync.sync_plan(state, dry_run=args.dry_run)
    except Exception as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        return 1
    print(
        f"{'[dry-run] ' if args.dry_run else ''}"
        f"inserted={result['inserted']}  patched={result['patched']}  "
        f"deleted={result['deleted']}  unchanged={result['unchanged']}"
    )
    if result["errors"]:
        print("\nErrors:")
        for e in result["errors"]:
            print(f"  - {e['date']}: {e['error']}")
        return 1
    return 0


# ---------- purge ----------


def cmd_purge(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "Refusing to purge without --yes. This deletes every pre_managed event in the calendar within ±365 days.",
            file=sys.stderr,
        )
        return 1
    from datetime import timedelta

    from temporal_context import today_local

    today = today_local()
    tmin = f"{(today - timedelta(days=365)).isoformat()}T00:00:00Z"
    tmax = f"{(today + timedelta(days=365)).isoformat()}T00:00:00Z"
    try:
        events = client.list_managed_events(tmin, tmax)
    except Exception as e:
        print(f"List failed: {e}", file=sys.stderr)
        return 1

    deleted = 0
    errors = 0
    for ev in events:
        ev_id = ev.get("id")
        if not ev_id:
            continue
        try:
            client.delete_event(ev_id)
            deleted += 1
        except Exception as e:
            errors += 1
            print(f"  delete {ev_id} failed: {e}", file=sys.stderr)

    # Wipe sync state too — it's now stale.
    try:
        from state_manager import StateManager

        StateManager().save_gcal_sync_state({})
    except Exception as e:
        print(f"  warning: failed to clear sync state: {e}", file=sys.stderr)

    print(f"✓ Purged {deleted} events ({errors} errors)")
    return 0 if errors == 0 else 1


# ---------- entrypoint ----------


def main() -> int:
    p = argparse.ArgumentParser(description="Google Calendar integration setup + diagnostics")
    sub = p.add_subparsers(dest="cmd", required=True)
    auth_p = sub.add_parser(
        "auth",
        help="OAuth via loopback listener (default) or OOB paste-code flow (--no-listener / --code)",
    )
    auth_p.add_argument(
        "--code",
        help="Pre-obtained authorization code (OOB flow). Skips listener and prompt; exchanges immediately.",
    )
    auth_p.add_argument(
        "--no-listener",
        action="store_true",
        help=(
            "Skip the loopback HTTP listener. Prints an OOB authorize URL "
            "and prompts for the code. Use inside container shells (e.g. "
            "`railway shell`) where the loopback redirect can't work."
        ),
    )
    auth_p.set_defaults(func=cmd_auth)
    sub.add_parser("status", help="Show env / tokens / calendar / sync state").set_defaults(func=cmd_status)

    sync_p = sub.add_parser("sync", help="Push plan.md table to the calendar")
    sync_p.add_argument("--dry-run", action="store_true", help="Log planned ops; make no API writes")
    sync_p.set_defaults(func=cmd_sync)

    purge_p = sub.add_parser("purge", help="Delete every pre_managed event in ±365d")
    purge_p.add_argument("--yes", action="store_true", help="Required confirmation flag")
    purge_p.set_defaults(func=cmd_purge)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
