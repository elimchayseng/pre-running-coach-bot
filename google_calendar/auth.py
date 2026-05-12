"""Google Calendar OAuth: exchange auth code, refresh access tokens, persist them.

Token storage backends (controlled by GCAL_TOKENS_BACKEND env var):
    "file" (default) — `.gcal_tokens.json` in the repo root, mode 0600.
                       Best for local dev. Survives across runs but NOT
                       across Railway deploys (filesystem is ephemeral).
    "redis"          — store in Redis under key TOKENS_REDIS_KEY.
                       Use on Railway: tokens persist across deploys with
                       no extra config (Redis is already in the stack).

Both shapes: {"refresh_token": str, "access_token": str, "expires_at": int}.

Note: Google's token endpoint returns `expires_in` (seconds), not `expires_at`
(unix timestamp like Strava). We compute `expires_at = now + expires_in` before
persisting so the on-disk shape matches Strava and the rest of the code can
share assumptions.

`get_access_token()` is the workhorse: callers ask for a fresh token and we
refresh transparently when within 60s of expiry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("pre_coach.gcal.auth")

TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".gcal_tokens.json"
TOKENS_REDIS_KEY = "gcal:tokens"

# Refresh when this many seconds (or fewer) remain on the access token.
REFRESH_LEEWAY_SECONDS = 60


def _backend() -> str:
    """Return the active token storage backend ('file' or 'redis')."""
    return (os.getenv("GCAL_TOKENS_BACKEND") or "file").lower()


class GcalAuthError(RuntimeError):
    """Auth failure that callers can distinguish from generic HTTP errors."""


class TokenStorageUnavailable(RuntimeError):
    """The configured token store is reachable-but-empty vs. fundamentally
    unreachable. Lets callers print a helpful message that doesn't tell the
    user to re-auth when Redis is just down."""


def _client_credentials() -> tuple[str, str]:
    cid = os.getenv("GCAL_CLIENT_ID")
    secret = os.getenv("GCAL_CLIENT_SECRET")
    if not cid or not secret:
        raise GcalAuthError("GCAL_CLIENT_ID and GCAL_CLIENT_SECRET must be set")
    return cid, secret


def _read_tokens() -> Optional[dict]:
    if _backend() == "redis":
        return _read_tokens_redis()
    return _read_tokens_file()


def _write_tokens(tokens: dict) -> None:
    if _backend() == "redis":
        _write_tokens_redis(tokens)
    else:
        _write_tokens_file(tokens)


def _read_tokens_file() -> Optional[dict]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read token file at {TOKEN_FILE}: {e}")
        return None


def _write_tokens_file(tokens: dict) -> None:
    """Atomic file write: write to .tmp + rename. Crash-safe."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=TOKEN_FILE.parent,
        prefix=f".{TOKEN_FILE.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(tokens, indent=2))
        os.replace(tmp_path, TOKEN_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _read_tokens_redis() -> Optional[dict]:
    """Read tokens from Redis. Raises TokenStorageUnavailable if Redis is
    unreachable (vs. returning None if the key just doesn't exist).
    """
    try:
        from conversation_store import _get_redis

        data = _get_redis().get(TOKENS_REDIS_KEY)
    except Exception as e:
        logger.warning(f"Redis unreachable for Gcal tokens: {e}")
        raise TokenStorageUnavailable(f"Redis unreachable: {e}") from e
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse Gcal tokens from Redis: {e}")
        return None


def _write_tokens_redis(tokens: dict) -> None:
    try:
        from conversation_store import _get_redis

        _get_redis().set(TOKENS_REDIS_KEY, json.dumps(tokens))
        logger.info("Wrote Gcal tokens to Redis (key=%s)", TOKENS_REDIS_KEY)
    except Exception as e:
        logger.error(f"Redis write for Gcal tokens failed: {e}")
        raise TokenStorageUnavailable(f"Redis write failed: {e}") from e


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """One-time: trade an authorization code for refresh + access tokens.

    Used by `scripts/google_calendar_setup.py auth` after the user authorizes
    the app. Persists to the configured backend and returns the token dict.
    """
    cid, secret = _client_credentials()
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise GcalAuthError(f"code exchange failed: {resp.status_code} {resp.text}")
    body = resp.json()
    if "refresh_token" not in body:
        raise GcalAuthError(
            "Google did not return a refresh_token. Re-run auth with "
            "prompt=consent and access_type=offline, and revoke prior grants "
            "at https://myaccount.google.com/permissions if needed."
        )
    expires_at = int(time.time()) + int(body.get("expires_in", 0))
    tokens = {
        "refresh_token": body["refresh_token"],
        "access_token": body["access_token"],
        "expires_at": expires_at,
    }
    _write_tokens(tokens)
    logger.info("Wrote initial Gcal tokens (backend=%s)", _backend())
    return tokens


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _refresh(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token. Retries on
    transient network errors only — 4xx propagates immediately."""
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
        raise GcalAuthError(f"token refresh failed: {resp.status_code} {resp.text}")
    body = resp.json()
    expires_at = int(time.time()) + int(body.get("expires_in", 0))
    # Google does NOT rotate refresh tokens on refresh — fall back to the one
    # we already have. (The .get() is for parity with Strava's shape.)
    return {
        "refresh_token": body.get("refresh_token", refresh_token),
        "access_token": body["access_token"],
        "expires_at": expires_at,
    }


def get_access_token() -> str:
    """Return a valid access token, refreshing if expired or near-expiry.

    Raises GcalAuthError if no token is stored (run setup first) or if
    refresh fails.
    """
    try:
        tokens = _read_tokens()
    except TokenStorageUnavailable as e:
        raise GcalAuthError(
            f"Gcal token storage unavailable ({_backend()} backend): {e}. "
            "Infrastructure issue, not auth — tokens are not lost. "
            "Verify REDIS_URL connectivity."
        ) from e

    if not tokens or "refresh_token" not in tokens:
        location = f"Redis key {TOKENS_REDIS_KEY}" if _backend() == "redis" else f"{TOKEN_FILE}"
        raise GcalAuthError(f"No Gcal tokens at {location}. Run `python scripts/google_calendar_setup.py auth` first.")

    expires_at = int(tokens.get("expires_at") or 0)
    now = int(time.time())
    if tokens.get("access_token") and expires_at - now > REFRESH_LEEWAY_SECONDS:
        return tokens["access_token"]

    logger.info("Gcal access token expired or stale; refreshing")
    refreshed = _refresh(tokens["refresh_token"])
    merged = {**tokens, **refreshed}
    _write_tokens(merged)
    return merged["access_token"]


def health_check() -> bool:
    """Return True if we can produce a working access token, False otherwise."""
    try:
        get_access_token()
        return True
    except Exception as e:
        logger.warning(f"Gcal auth health check failed: {e}")
        return False
