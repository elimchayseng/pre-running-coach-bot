"""COROS MCP OAuth: dynamic client registration, code exchange, token refresh.

Mirrors google_calendar/auth.py with two COROS-specific differences:

1. No pre-provisioned client credentials. The OAuth client itself is created
   via RFC 7591 dynamic client registration (public client, no secret), so the
   persisted blob carries the registration alongside the tokens:

       {"client_info": {"client_id": ...},
        "tokens": {"access_token": ..., "refresh_token": ...,
                   "expires_at": <unix>, "scope": ...}}

2. COROS ROTATES the refresh token on every refresh (verified in the PR 0
   spike — see docs/coros-mcp.md). Two consequences handled here:
   - the rotated token is persisted BEFORE the new access token is returned
     (a lost write would mean lockout);
   - refreshes are serialized under a module lock so two threads can't both
     spend the same single-use refresh token.

Token storage backends (COROS_TOKENS_BACKEND env var):
    "file" (default) — `.coros_tokens.json` in the repo root, mode 0600.
    "redis"          — Redis key TOKENS_REDIS_KEY; use on Railway so tokens
                       survive deploys. Re-auth from the laptop with
                       `make coros-reauth-prod`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
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

logger = logging.getLogger("pre_coach.coros.auth")

ISSUER = os.getenv("COROS_OAUTH_ISSUER", "https://mcpus.coros.com")
TOKEN_URL = f"{ISSUER}/oauth2/token"
AUTHORIZE_URL = f"{ISSUER}/oauth2/authorize"
REGISTER_URL = f"{ISSUER}/connect/register"
# `offline_access` is what makes COROS issue a refresh token.
SCOPE = "mcp.tools offline_access"

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".coros_tokens.json"
TOKENS_REDIS_KEY = "coros:tokens"

# Refresh when this many seconds (or fewer) remain on the access token.
# COROS access tokens live ~30 days; an hour of leeway keeps any in-flight
# pull comfortably inside validity.
REFRESH_LEEWAY_SECONDS = 3600

# Serializes refresh: COROS rotates refresh tokens, so a concurrent second
# refresh would present an already-consumed token and fail.
_refresh_lock = threading.Lock()


def _backend() -> str:
    """Return the active token storage backend ('file' or 'redis')."""
    return (os.getenv("COROS_TOKENS_BACKEND") or "file").lower()


class CorosAuthError(RuntimeError):
    """Auth failure that callers can distinguish from generic HTTP errors."""


class TokenStorageUnavailable(RuntimeError):
    """The configured token store is fundamentally unreachable (vs. merely
    empty). Lets callers avoid telling the user to re-auth when Redis is
    just down."""


def _read_blob() -> Optional[dict]:
    if _backend() == "redis":
        return _read_blob_redis()
    return _read_blob_file()


def _write_blob(blob: dict) -> None:
    if _backend() == "redis":
        _write_blob_redis(blob)
    else:
        _write_blob_file(blob)


def _read_blob_file() -> Optional[dict]:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read token file at {TOKEN_FILE}: {e}")
        return None


def _write_blob_file(blob: dict) -> None:
    """Atomic file write: write to .tmp + rename. Crash-safe."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=TOKEN_FILE.parent,
        prefix=f".{TOKEN_FILE.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(blob, indent=2))
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


def _read_blob_redis() -> Optional[dict]:
    """Read the token blob from Redis. Raises TokenStorageUnavailable if Redis
    is unreachable (vs. returning None if the key just doesn't exist)."""
    try:
        from conversation_store import _get_redis

        data = _get_redis().get(TOKENS_REDIS_KEY)
    except Exception as e:
        logger.warning(f"Redis unreachable for COROS tokens: {e}")
        raise TokenStorageUnavailable(f"Redis unreachable: {e}") from e
    if not data:
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        logger.warning(f"Could not parse COROS tokens from Redis: {e}")
        return None


def _write_blob_redis(blob: dict) -> None:
    try:
        from conversation_store import _get_redis

        _get_redis().set(TOKENS_REDIS_KEY, json.dumps(blob))
        logger.info("Wrote COROS tokens to Redis (key=%s)", TOKENS_REDIS_KEY)
    except Exception as e:
        logger.error(f"Redis write for COROS tokens failed: {e}")
        raise TokenStorageUnavailable(f"Redis write failed: {e}") from e


def register_client(redirect_uri: str) -> dict:
    """RFC 7591 dynamic client registration. Returns the registration document
    (client_id etc.); the caller persists it in the blob via
    exchange_code_for_tokens. Public client — no secret is issued."""
    resp = requests.post(
        REGISTER_URL,
        json={
            "client_name": "PRE Running Coach",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        raise CorosAuthError(f"client registration failed: {resp.status_code} {resp.text}")
    return resp.json()


def _tokens_from_response(body: dict, fallback_refresh: Optional[str] = None) -> dict:
    """Normalize a token-endpoint response to the persisted shape
    (expires_in -> absolute expires_at, like the Strava/Gcal blobs)."""
    refresh = body.get("refresh_token") or fallback_refresh
    if not refresh:
        raise CorosAuthError(
            "COROS did not return a refresh_token. Ensure the requested scope "
            f"includes offline_access (got scope={body.get('scope')!r})."
        )
    return {
        "access_token": body["access_token"],
        "refresh_token": refresh,
        "expires_at": int(time.time()) + int(body.get("expires_in", 0)),
        "scope": body.get("scope"),
    }


def exchange_code_for_tokens(
    code: str, redirect_uri: str, code_verifier: str, client_info: dict
) -> dict:
    """One-time: trade an authorization code (PKCE) for refresh + access
    tokens. Persists {client_info, tokens} and returns the blob."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_info["client_id"],
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise CorosAuthError(f"code exchange failed: {resp.status_code} {resp.text}")
    blob = {"client_info": client_info, "tokens": _tokens_from_response(resp.json())}
    _write_blob(blob)
    logger.info("Wrote initial COROS tokens (backend=%s)", _backend())
    return blob


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _refresh_request(refresh_token: str, client_id: str) -> dict:
    """POST the refresh grant. Retries transient network errors only — a 4xx
    means the refresh token is dead (rotated away or revoked) and propagates
    immediately as CorosAuthError."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise CorosAuthError(f"token refresh failed: {resp.status_code} {resp.text}")
    return resp.json()


def get_access_token() -> str:
    """Return a valid access token, refreshing (and persisting the rotated
    refresh token) if expired or near-expiry.

    Raises CorosAuthError if no tokens are stored (run setup first) or if
    refresh fails.
    """
    try:
        blob = _read_blob()
    except TokenStorageUnavailable as e:
        raise CorosAuthError(
            f"COROS token storage unavailable ({_backend()} backend): {e}. "
            "Infrastructure issue, not auth — tokens are not lost. "
            "Verify REDIS_URL connectivity."
        ) from e

    tokens = (blob or {}).get("tokens") or {}
    client_info = (blob or {}).get("client_info") or {}
    if not tokens.get("refresh_token") or not client_info.get("client_id"):
        location = f"Redis key {TOKENS_REDIS_KEY}" if _backend() == "redis" else f"{TOKEN_FILE}"
        raise CorosAuthError(
            f"No COROS tokens at {location}. Run `python scripts/coros_setup.py auth` first."
        )

    now = int(time.time())
    if tokens.get("access_token") and int(tokens.get("expires_at") or 0) - now > REFRESH_LEEWAY_SECONDS:
        return tokens["access_token"]

    with _refresh_lock:
        # Re-read inside the lock: another thread may have just refreshed and
        # rotated the token while we were waiting.
        blob = _read_blob() or blob
        tokens = blob["tokens"]
        now = int(time.time())
        if tokens.get("access_token") and int(tokens.get("expires_at") or 0) - now > REFRESH_LEEWAY_SECONDS:
            return tokens["access_token"]

        logger.info("COROS access token expired or stale; refreshing")
        body = _refresh_request(tokens["refresh_token"], blob["client_info"]["client_id"])
        blob["tokens"] = _tokens_from_response(body, fallback_refresh=tokens["refresh_token"])
        # Persist FIRST: COROS rotates the refresh token, so returning before
        # the write sticks would risk losing the only valid credential.
        _write_blob(blob)
        return blob["tokens"]["access_token"]


def health_check() -> bool:
    """Return True if we can produce a working access token, False otherwise."""
    try:
        get_access_token()
        return True
    except Exception as e:
        logger.warning(f"COROS auth health check failed: {e}")
        return False
