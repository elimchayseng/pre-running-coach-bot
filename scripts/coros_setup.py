"""One-time COROS MCP setup CLI + ongoing diagnostic tool.

Subcommands:
    auth        OAuth (PKCE, dynamic client registration) via a one-shot
                loopback HTTP listener; store tokens in the configured backend.
    status      Show env, token state, and a live MCP round-trip.
    pull        Run the nightly pull manually (--days N, --dry-run).

Unlike Google Calendar there is no GCP-console step: the OAuth client is
created automatically via dynamic client registration on first auth. You only
log into your COROS account in the browser.

COROS rotates refresh tokens on every refresh and access tokens live ~30 days
(see docs/coros-mcp.md). If prod auth ever dies, the watchdog Telegram-alerts
and `make coros-reauth-prod` re-auths in one command (browser on the laptop,
token written to Railway's Redis).

Usage:
    ./venv/bin/python scripts/coros_setup.py auth
    ./venv/bin/python scripts/coros_setup.py status
    ./venv/bin/python scripts/coros_setup.py pull --days 7 --dry-run
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from coros import auth, client  # noqa: E402

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 8766
REDIRECT_URI = f"http://localhost:{LOOPBACK_PORT}/callback"


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
        # following the OAuth redirect. Only treat requests carrying the OAuth
        # response params as the callback we're waiting for.
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return
        self.server.received = params  # type: ignore[attr-defined]
        body = (
            b"<html><body style='font-family: sans-serif; padding: 2em;'>"
            b"<h2>COROS authorization received.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _build_auth_url(client_id: str, state_token: str, code_challenge: str) -> str:
    """Build the COROS OAuth authorize URL (PKCE S256, public client)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": auth.SCOPE,
        "state": state_token,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{auth.AUTHORIZE_URL}?{urlencode(params)}"


def cmd_auth(_: argparse.Namespace) -> int:
    # Reuse a previously registered client if the blob has one (registration
    # is idempotent-ish but each call mints a new client_id; reuse keeps the
    # grant history tidy on COROS's side).
    try:
        blob = auth._read_blob() or {}
    except auth.TokenStorageUnavailable as e:
        print(f"Token storage unreachable: {e}", file=sys.stderr)
        return 1
    client_info = blob.get("client_info")
    if not client_info:
        try:
            client_info = auth.register_client(REDIRECT_URI)
        except Exception as e:
            print(f"Dynamic client registration failed: {e}", file=sys.stderr)
            return 1
        print(f"Registered OAuth client: {client_info['client_id']}")

    verifier, challenge = _pkce_pair()
    state_token = secrets.token_urlsafe(24)
    url = _build_auth_url(client_info["client_id"], state_token, challenge)

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
        "Open this URL in your browser and log into COROS:\n\n"
        f"   {url}\n"
    )
    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        while server.received is None:  # type: ignore[attr-defined]
            server.handle_request()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 1
    finally:
        server.server_close()

    received = server.received  # type: ignore[attr-defined]
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

    try:
        auth.exchange_code_for_tokens(code, REDIRECT_URI, verifier, client_info)
    except Exception as e:
        print(f"\nAuth failed: {e}", file=sys.stderr)
        return 1
    print(f"\n✓ Wrote tokens to {auth._backend()} backend.")
    return 0


# ---------- status ----------


def cmd_status(_: argparse.Namespace) -> int:
    rc = 0
    print("=== COROS MCP integration status ===")

    _section("Environment:")
    backend_env = os.getenv("COROS_TOKENS_BACKEND")
    if backend_env:
        _ok(f"COROS_TOKENS_BACKEND={backend_env}")
    else:
        _warn("COROS_TOKENS_BACKEND unset — defaulting to 'file' (local dev OK; Railway should be 'redis')")
    _ok(f"COROS_MCP_URL={client.MCP_URL}")
    tz = os.getenv("USER_TIMEZONE")
    if tz:
        _ok(f"USER_TIMEZONE={tz}")
    else:
        _warn("USER_TIMEZONE unset — daily metrics will be bucketed in UTC")

    _section("Token storage:")
    print(f"  backend: {auth._backend()}")
    try:
        blob = auth._read_blob()
    except auth.TokenStorageUnavailable as e:
        _fail(f"Storage unreachable: {e}")
        print("    -> Infrastructure issue, not auth. Verify REDIS_URL connectivity.")
        return 1

    tokens = (blob or {}).get("tokens") or {}
    client_info = (blob or {}).get("client_info") or {}
    if not blob:
        _warn("No tokens stored. Run: python scripts/coros_setup.py auth")
        return 1
    if not client_info.get("client_id"):
        _fail("Blob present but missing client_info (corrupt). Re-auth.")
        rc = 1
    else:
        _ok(f"client_id: {client_info['client_id']}")
    if not tokens.get("refresh_token"):
        _fail("Blob present but missing refresh_token (corrupt). Re-auth.")
        rc = 1
    else:
        remaining = int(tokens.get("expires_at") or 0) - int(time.time())
        _ok(f"refresh_token present (length {len(tokens['refresh_token'])})")
        _ok(f"access_token expires in {remaining}s (~{remaining // 86400}d)")
        if remaining < auth.REFRESH_LEEWAY_SECONDS:
            _warn("access_token within refresh leeway — next call will refresh (and rotate the refresh token)")

    _section("COROS MCP API:")
    try:
        info = client.query_user_info()
        first_line = next((ln for ln in info.replace('\\n', '\n').splitlines() if ln.strip()), "")
        _ok(f"queryUserInfo round-trip OK ({first_line.strip()[:60]})")
    except Exception as e:
        _fail(f"queryUserInfo failed: {e}")
        rc = 1

    print()
    return rc


# ---------- pull ----------


def cmd_pull(args: argparse.Namespace) -> int:
    from coros import ingest
    from state_manager import StateManager

    state = StateManager(ROOT / "state")
    try:
        result = ingest.run_nightly_pull(state, days=args.days, dry_run=args.dry_run)
    except Exception as e:
        print(f"Pull failed: {e}", file=sys.stderr)
        return 1
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}dates={result['dates']}  fields_parsed={result['fields_parsed']}")
    if result["errors"]:
        print("\nErrors:")
        for e in result["errors"]:
            print(f"  - {e}")
        return 1
    return 0


# ---------- entrypoint ----------


def main() -> int:
    p = argparse.ArgumentParser(description="COROS MCP integration setup + diagnostics")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="OAuth via loopback listener (PKCE + dynamic client registration)").set_defaults(
        func=cmd_auth
    )
    sub.add_parser("status", help="Show env / tokens / live MCP round-trip").set_defaults(func=cmd_status)

    pull_p = sub.add_parser("pull", help="Run the nightly health pull manually")
    pull_p.add_argument("--days", type=int, default=None, help="Backfill window (default COROS_BACKFILL_DAYS or 4)")
    pull_p.add_argument("--dry-run", action="store_true", help="Parse and print; write nothing")
    pull_p.set_defaults(func=cmd_pull)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
