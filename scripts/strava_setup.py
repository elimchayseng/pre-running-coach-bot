"""One-time Strava setup CLI.

Subcommands:
    auth        Walk through OAuth code exchange, write .strava_tokens.json.
    subscribe   POST a webhook subscription pointing at <WEBHOOK_URL>/strava/webhook.
    list-subs   List active push subscriptions for this app.
    unsubscribe Delete a subscription by id.

Usage:
    ./venv/bin/python scripts/strava_setup.py auth
    ./venv/bin/python scripts/strava_setup.py subscribe
    ./venv/bin/python scripts/strava_setup.py list-subs
    ./venv/bin/python scripts/strava_setup.py unsubscribe <id>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from strava import auth, client  # noqa: E402

REDIRECT_URI = "http://localhost"  # Strava requires a domain match; we use the code from the URL bar
SCOPES = "activity:read,read"


def cmd_auth(_: argparse.Namespace) -> int:
    cid = os.getenv("STRAVA_CLIENT_ID")
    if not cid:
        print("STRAVA_CLIENT_ID is not set in .env", file=sys.stderr)
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
        "1. Open this URL in your browser and authorize:\n"
        f"\n   {url}\n\n"
        "2. After authorizing, your browser will redirect to a URL like:\n"
        f"   {REDIRECT_URI}/?state=&code=<long_code>&scope=read,activity:read\n\n"
        "3. Copy the value of `code` from the URL bar (the long hex string).\n"
    )
    code = input("Paste code: ").strip()
    if not code:
        print("No code provided.", file=sys.stderr)
        return 1
    try:
        tokens = auth.exchange_code_for_tokens(code)
    except Exception as e:
        print(f"Auth failed: {e}", file=sys.stderr)
        return 1
    print("✓ Wrote tokens to .strava_tokens.json")
    print(f"  athlete_id: {tokens.get('athlete_id')}")
    print(f"  expires_at: {tokens.get('expires_at')}")
    return 0


def cmd_subscribe(_: argparse.Namespace) -> int:
    base = os.getenv("WEBHOOK_URL")
    verify = os.getenv("STRAVA_VERIFY_TOKEN")
    if not base or not verify:
        print("WEBHOOK_URL and STRAVA_VERIFY_TOKEN must be set in .env", file=sys.stderr)
        return 1
    callback = f"{base.rstrip('/')}/strava/webhook"
    try:
        sub_id = client.subscribe_webhook(callback, verify)
    except Exception as e:
        print(f"Subscribe failed: {e}", file=sys.stderr)
        return 1
    print(f"✓ Subscription {sub_id} → {callback}")
    return 0


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


def main() -> int:
    p = argparse.ArgumentParser(description="Strava integration setup")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="OAuth code exchange").set_defaults(func=cmd_auth)
    sub.add_parser("subscribe", help="Register webhook subscription").set_defaults(func=cmd_subscribe)
    sub.add_parser("list-subs", help="List subscriptions").set_defaults(func=cmd_list_subs)
    unsub = sub.add_parser("unsubscribe", help="Delete subscription")
    unsub.add_argument("sub_id", help="Subscription id from list-subs")
    unsub.set_defaults(func=cmd_unsubscribe)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
