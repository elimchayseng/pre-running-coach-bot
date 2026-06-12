"""Spike: verify headless OAuth against the official COROS MCP server.

Throwaway script (PR 0). Answers the go/no-go questions before any real
COROS integration is built:

1. OAuth discovery works            -> verified: /.well-known/oauth-authorization-server
2. Dynamic client registration      -> verified: POST /connect/register (201, public client)
3. Refresh tokens issued + usable   -> `auth` then `refresh` modes
4. Headless replay                  -> `replay` mode (no browser, persisted tokens only)
5. Fixture capture                  -> `fixtures` mode (raw tool text -> tests/fixtures/coros/)

Usage:
    python scripts/coros_spike.py auth       # one-time interactive browser login
    python scripts/coros_spike.py replay     # headless: call queryDailyHealthData
    python scripts/coros_spike.py refresh    # force a refresh-token exchange
    python scripts/coros_spike.py fixtures   # capture all query tool outputs

Tokens land in .coros_spike_tokens.json (gitignored; deleted after PR 0).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = REPO_ROOT / ".coros_spike_tokens.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "coros"

MCP_URL = os.getenv("COROS_MCP_URL", "https://mcpus.coros.com/mcp")
ISSUER = "https://mcpus.coros.com"
AUTHZ_ENDPOINT = f"{ISSUER}/oauth2/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/oauth2/token"
REGISTER_ENDPOINT = f"{ISSUER}/connect/register"
REDIRECT_PORT = 8766
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPE = "mcp.tools offline_access"

QUERY_TOOLS = {
    "queryDailyHealthData": {"days": 7, "timezone": "America/New_York"},
    "querySleepData": {"days": 7, "timezone": "America/New_York", "startDate": "", "endDate": ""},
    "queryHrvAssessment": {"days": 7, "timezone": "America/New_York"},
    "queryRestingHeartRate": {"days": 7, "timezone": "America/New_York"},
    "queryStressLevel": {"days": 7, "timezone": "America/New_York"},
    "queryAvgHeartRate": {"days": 7, "timezone": "America/New_York"},
    "queryRecoveryStatus": {},
    "queryTrainingLoadAssessment": {"days": 7},
    "queryUserInfo": {},
}


def _load() -> dict:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return {}


def _save(blob: dict) -> None:
    TOKEN_FILE.write_text(json.dumps(blob, indent=2))
    os.chmod(TOKEN_FILE, 0o600)
    print(f"saved -> {TOKEN_FILE}")


def _register_client() -> dict:
    resp = requests.post(
        REGISTER_ENDPOINT,
        json={
            "client_name": "PRE Running Coach (spike)",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot loopback listener for the OAuth redirect."""

    code: str | None = None
    state: str | None = None
    expected_state: str = ""

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>COROS auth complete \xe2\x80\x94 you can close this tab.</h2>")

    def log_message(self, *args):  # silence
        pass


def cmd_auth() -> int:
    blob = _load()
    client = blob.get("client_info")
    if not client:
        client = _register_client()
        print(f"registered client_id={client['client_id']}")

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _CallbackHandler.expected_state = state

    auth_url = f"{AUTHZ_ENDPOINT}?" + urlencode(
        {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    server = HTTPServer(("localhost", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\nOpen this URL in your browser and log into COROS:\n")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    deadline = time.time() + 600
    while _CallbackHandler.code is None and time.time() < deadline:
        time.sleep(0.5)
    server.shutdown()

    if _CallbackHandler.code is None:
        print("timed out waiting for OAuth callback", file=sys.stderr)
        return 1
    if _CallbackHandler.state != state:
        print("state mismatch (CSRF check failed)", file=sys.stderr)
        return 1

    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": _CallbackHandler.code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client["client_id"],
            "code_verifier": verifier,
        },
        timeout=15,
    )
    print(f"token exchange -> {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500], file=sys.stderr)
        return 1
    tokens = resp.json()
    print(
        "got: access_token"
        + (", refresh_token" if tokens.get("refresh_token") else " (NO refresh_token!)")
        + f", expires_in={tokens.get('expires_in')}s, scope={tokens.get('scope')}"
    )
    tokens["obtained_at"] = int(time.time())
    _save({"client_info": client, "tokens": tokens})
    return 0


def _refresh(blob: dict) -> dict:
    resp = requests.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "refresh_token",
            "refresh_token": blob["tokens"]["refresh_token"],
            "client_id": blob["client_info"]["client_id"],
        },
        timeout=15,
    )
    print(f"refresh -> {resp.status_code}")
    resp.raise_for_status()
    new = resp.json()
    rotated = new.get("refresh_token") not in (None, blob["tokens"]["refresh_token"])
    print(
        f"new access_token (expires_in={new.get('expires_in')}s); "
        f"refresh_token {'ROTATED' if rotated else 'unchanged/absent -> keeping old'}"
    )
    if not new.get("refresh_token"):
        new["refresh_token"] = blob["tokens"]["refresh_token"]
    new["obtained_at"] = int(time.time())
    blob["tokens"] = new
    _save(blob)
    return blob


def _ensure_fresh(blob: dict) -> dict:
    tok = blob["tokens"]
    age = int(time.time()) - tok.get("obtained_at", 0)
    if age >= tok.get("expires_in", 0) - 60:
        print(f"access token stale (age={age}s) -> refreshing")
        blob = _refresh(blob)
    return blob


async def _call_tool(access_token: str, name: str, args: dict) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {access_token}"}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            parts = [c.text for c in result.content if getattr(c, "text", None)]
            return "\n".join(parts)


def cmd_replay() -> int:
    blob = _load()
    if not blob.get("tokens"):
        print("no tokens — run `auth` first", file=sys.stderr)
        return 1
    blob = _ensure_fresh(blob)
    text = asyncio.run(
        _call_tool(blob["tokens"]["access_token"], "queryDailyHealthData", {"days": 2, "timezone": "America/New_York"})
    )
    print("\n--- queryDailyHealthData (headless) ---\n")
    print(text)
    print("\nHEADLESS REPLAY: OK")
    return 0


def cmd_refresh() -> int:
    blob = _load()
    if not blob.get("tokens", {}).get("refresh_token"):
        print("no refresh_token — run `auth` first", file=sys.stderr)
        return 1
    _refresh(blob)
    return 0


def cmd_fixtures() -> int:
    blob = _load()
    if not blob.get("tokens"):
        print("no tokens — run `auth` first", file=sys.stderr)
        return 1
    blob = _ensure_fresh(blob)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    token = blob["tokens"]["access_token"]
    for name, args in QUERY_TOOLS.items():
        try:
            text = asyncio.run(_call_tool(token, name, args))
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: FAILED ({exc})")
            continue
        out = FIXTURES_DIR / f"{name}.txt"
        out.write_text(text)
        print(f"{name}: {len(text)} chars -> {out.relative_to(REPO_ROOT)}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    return {
        "auth": cmd_auth,
        "replay": cmd_replay,
        "refresh": cmd_refresh,
        "fixtures": cmd_fixtures,
    }.get(cmd, lambda: (print(__doc__), 2)[1])()


if __name__ == "__main__":
    sys.exit(main())
