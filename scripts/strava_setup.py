"""One-time Strava setup CLI + ongoing diagnostic tool.

Subcommands:
    auth        Walk through OAuth code exchange, write tokens.
    status      Show env, tokens, athlete, and active subscriptions. Use this
                first when something seems broken.
    subscribe   Idempotent: replaces any existing subscription if it points
                at a different URL; no-op if it already points at this app.
    list-subs   List active push subscriptions for this app.
    unsubscribe Delete a subscription by id (use list-subs to find it).

Usage:
    ./venv/bin/python scripts/strava_setup.py auth
    ./venv/bin/python scripts/strava_setup.py status
    ./venv/bin/python scripts/strava_setup.py subscribe
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from strava import auth, client  # noqa: E402

REDIRECT_URI = "http://localhost"
# activity:read_all is required to fetch activities marked "Only You" or with
# privacy zones. Without it, get_activity returns 404 even for activities our
# webhook subscription is notified about. activity:read alone is too narrow
# for any user with default-private activities.
SCOPES = "activity:read_all,read"


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


def cmd_auth(_: argparse.Namespace) -> int:
    cid = os.getenv("STRAVA_CLIENT_ID")
    if not cid:
        print("STRAVA_CLIENT_ID is not set in .env (or environment)", file=sys.stderr)
        return 1
    if not os.getenv("STRAVA_CLIENT_SECRET"):
        print("STRAVA_CLIENT_SECRET is not set in .env (or environment)", file=sys.stderr)
        return 1

    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "approval_prompt": "auto",
        "scope": SCOPES,
    }
    url = f"https://www.strava.com/oauth/authorize?{urlencode(params)}"
    print(
        f"\nBackend: {auth._backend()}\n\n"
        "1. Open this URL in your browser and authorize:\n"
        f"\n   {url}\n\n"
        "2. After authorizing, your browser will redirect to a URL like:\n"
        f"   {REDIRECT_URI}/?state=&code=<long_code>&scope=read,activity:read\n\n"
        "3. Copy the value of `code` from the URL (the long hex string).\n"
    )
    code = input("Paste code: ").strip()
    if not code:
        print("No code provided.", file=sys.stderr)
        return 1
    try:
        tokens = auth.exchange_code_for_tokens(code)
    except Exception as e:
        print(f"\nAuth failed: {e}", file=sys.stderr)
        print(
            "\nCommon causes:\n"
            "  - Code already used or expired (codes are single-use, ~30s lifetime)\n"
            "  - Authorization Callback Domain in your Strava API app settings\n"
            "    doesn't match 'localhost'\n"
            "  - STRAVA_CLIENT_SECRET is wrong",
            file=sys.stderr,
        )
        return 1
    print(f"\n✓ Wrote tokens to {auth._backend()} backend.")
    print(f"  athlete_id: {tokens.get('athlete_id')}")
    return 0


# ---------- status ----------


def cmd_status(_: argparse.Namespace) -> int:
    """Comprehensive 'is everything wired?' check."""
    rc = 0
    print("=== Strava integration status ===")

    _section("Environment:")
    env_groups = [
        ("Required for API", ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]),
        ("Required for webhook", ["STRAVA_VERIFY_TOKEN", "WEBHOOK_URL"]),
        ("Required for Telegram ping", ["USER_TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN"]),
        ("Token storage", ["STRAVA_TOKENS_BACKEND"]),
    ]
    for label, names in env_groups:
        print(f"  [{label}]")
        for n in names:
            v = os.getenv(n)
            if v:
                _ok(f"{n} set")
            elif n == "STRAVA_TOKENS_BACKEND":
                _warn(f"{n} unset — defaulting to 'file' (local dev OK; Railway should be 'redis')")
            else:
                _fail(f"{n} MISSING")
                rc = 1

    _section("Token storage:")
    backend = auth._backend()
    print(f"  backend: {backend}")
    try:
        # Probe the storage layer directly first (distinguishes "redis down" from "no token")
        try:
            tokens = auth._read_tokens()
        except auth.TokenStorageUnavailable as e:
            _fail(f"Storage unreachable: {e}")
            print(
                "    -> Infrastructure issue, not auth. Verify REDIS_URL connectivity\n"
                "       (redis-cli -u $REDIS_URL ping). Tokens are not lost.",
            )
            rc = 1
            tokens = None

        if tokens is None:
            _warn("No tokens stored. Run: python scripts/strava_setup.py auth")
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
    except auth.StravaAuthError as e:
        _fail(f"Auth check failed: {e}")
        rc = 1

    _section("Strava API:")
    try:
        athlete = client.get_athlete()
        name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
        _ok(f"GET /athlete: {name} (id={athlete.get('id')})")
    except Exception as e:
        _fail(f"GET /athlete failed: {e}")
        rc = 1

    _section("Webhook subscriptions:")
    try:
        subs = client.list_subscriptions()
        if not subs:
            _warn("No active subscription. Run: python scripts/strava_setup.py subscribe")
            rc = 1
        else:
            expected = (os.getenv("WEBHOOK_URL") or "").rstrip("/") + "/strava/webhook"
            for s in subs:
                cb = s.get("callback_url", "")
                match = " (matches WEBHOOK_URL)" if expected and cb == expected else ""
                _ok(f"id={s.get('id')}  callback={cb}{match}")
                if expected and cb != expected:
                    _warn(f"callback differs from expected {expected} — run subscribe to replace")
                    rc = 1
    except Exception as e:
        _fail(f"list_subscriptions failed: {e}")
        rc = 1

    print()
    return rc


# ---------- subscribe ----------


def cmd_subscribe(_: argparse.Namespace) -> int:
    base = os.getenv("WEBHOOK_URL")
    verify = os.getenv("STRAVA_VERIFY_TOKEN")
    if not base:
        print("WEBHOOK_URL is not set", file=sys.stderr)
        return 1
    if not verify:
        print(
            "STRAVA_VERIFY_TOKEN is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(24))"',
            file=sys.stderr,
        )
        return 1

    callback = f"{base.rstrip('/')}/strava/webhook"
    try:
        sub_id, action = client.ensure_subscription(callback, verify)
    except Exception as e:
        print(f"Subscribe failed: {e}", file=sys.stderr)
        print(
            "\nCommon causes:\n"
            "  - WEBHOOK_URL must be HTTPS and reachable from the public internet\n"
            "  - Strava verify failed: STRAVA_VERIFY_TOKEN on Railway must match\n"
            "    what this script sends, AND your /strava/webhook GET handler\n"
            "    must echo back hub.challenge\n"
            "  - The app must already be deployed and serving traffic before you\n"
            "    run subscribe.",
            file=sys.stderr,
        )
        return 1

    verbs = {"created": "Created", "kept": "Already pointed at", "replaced": "Replaced stale subscription with"}
    print(f"✓ {verbs[action]} subscription {sub_id} → {callback}")
    return 0


# ---------- list / unsubscribe ----------


def cmd_list_subs(_: argparse.Namespace) -> int:
    try:
        subs = client.list_subscriptions()
    except Exception as e:
        print(f"List failed: {e}", file=sys.stderr)
        return 1
    if not subs:
        print("(no active subscriptions)")
        return 0
    for s in subs:
        print(f"  id={s.get('id')}  callback={s.get('callback_url')}  created={s.get('created_at')}")
    return 0


def cmd_unsubscribe(args: argparse.Namespace) -> int:
    try:
        client.delete_subscription(int(args.sub_id))
    except Exception as e:
        print(f"Unsubscribe failed: {e}", file=sys.stderr)
        return 1
    print(f"✓ Deleted subscription {args.sub_id}")
    return 0


# ---------- entrypoint ----------


def main() -> int:
    p = argparse.ArgumentParser(description="Strava integration setup + diagnostics")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="OAuth code exchange").set_defaults(func=cmd_auth)
    sub.add_parser("status", help="Show env / tokens / athlete / subs").set_defaults(func=cmd_status)
    sub.add_parser("subscribe", help="Register or replace webhook subscription").set_defaults(func=cmd_subscribe)
    sub.add_parser("list-subs", help="List subscriptions").set_defaults(func=cmd_list_subs)
    unsub = sub.add_parser("unsubscribe", help="Delete subscription")
    unsub.add_argument("sub_id", help="Subscription id from list-subs")
    unsub.set_defaults(func=cmd_unsubscribe)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
