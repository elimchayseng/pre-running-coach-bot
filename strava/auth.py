"""Strava OAuth: exchange auth code, refresh access tokens, persist to disk.

Token file `.strava_tokens.json` (gitignored) stores:
    {"refresh_token": str, "access_token": str, "expires_at": int}

`get_access_token()` is the workhorse: callers ask for a fresh token and we
refresh transparently when within 60s of expiry.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("pre_coach.strava.auth")

TOKEN_URL = "https://www.strava.com/oauth/token"
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".strava_tokens.json"

# Refresh when this many seconds (or fewer) remain on the access token.
REFRESH_LEEWAY_SECONDS = 60


class StravaAuthError(RuntimeError):
    """Auth failure that callers can distinguish from generic HTTP errors."""


def _client_credentials() -> tuple[str, str]:
    cid = os.getenv("STRAVA_CLIENT_ID")
    secret = os.getenv("STRAVA_CLIENT_SECRET")
    if not cid or not secret:
        raise StravaAuthError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set")
    return cid, secret


def _read_tokens() -> Optional[dict]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read token file at {TOKEN_FILE}: {e}")
        return None


def _write_tokens(tokens: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    # 0600 so the secret isn't world-readable on shared hosts.
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def exchange_code_for_tokens(code: str) -> dict:
    """One-time: trade an authorization code for refresh + access tokens.

    Used by `scripts/strava_setup.py auth` after the user authorizes the app.
    Persists to `.strava_tokens.json` and returns the token dict.
    """
    cid, secret = _client_credentials()
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise StravaAuthError(f"code exchange failed: {resp.status_code} {resp.text}")
    body = resp.json()
    tokens = {
        "refresh_token": body["refresh_token"],
        "access_token": body["access_token"],
        "expires_at": body["expires_at"],
        "athlete_id": body.get("athlete", {}).get("id"),
    }
    _write_tokens(tokens)
    logger.info("Wrote initial Strava tokens to %s", TOKEN_FILE)
    return tokens


def _refresh(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token."""
    cid, secret = _client_credentials()
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise StravaAuthError(f"token refresh failed: {resp.status_code} {resp.text}")
    body = resp.json()
    # Strava may rotate the refresh token; persist whatever we got back.
    return {
        "refresh_token": body.get("refresh_token", refresh_token),
        "access_token": body["access_token"],
        "expires_at": body["expires_at"],
    }


def get_access_token() -> str:
    """Return a valid access token, refreshing if expired or near-expiry.

    Raises StravaAuthError if no token file exists (run setup first) or if
    refresh fails.
    """
    tokens = _read_tokens()
    if not tokens or "refresh_token" not in tokens:
        raise StravaAuthError(f"No Strava tokens at {TOKEN_FILE}. Run `python scripts/strava_setup.py auth` first.")

    expires_at = int(tokens.get("expires_at") or 0)
    now = int(time.time())
    if tokens.get("access_token") and expires_at - now > REFRESH_LEEWAY_SECONDS:
        return tokens["access_token"]

    logger.info("Strava access token expired or stale; refreshing")
    refreshed = _refresh(tokens["refresh_token"])
    # Preserve athlete_id and any other fields not returned by the refresh.
    merged = {**tokens, **refreshed}
    _write_tokens(merged)
    return merged["access_token"]


def health_check() -> bool:
    """Return True if we can produce a working access token, False otherwise.

    Used by /health. Refreshes if needed; logs but does not raise on failure.
    """
    try:
        get_access_token()
        return True
    except Exception as e:
        logger.warning(f"Strava auth health check failed: {e}")
        return False
