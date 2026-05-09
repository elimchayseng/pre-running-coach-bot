"""Strava API v3 client: activities + push subscriptions.

Auth is supplied by `strava.auth.get_access_token()` (refreshes transparently).
HTTP retries on connection / 5xx errors via tenacity. 401 forces a one-shot
token refresh + retry. 429 raises StravaRateLimitError so the caller can
back off intelligently.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import auth

logger = logging.getLogger("pre_coach.strava.client")

API_BASE = "https://www.strava.com/api/v3"
SUBSCRIPTIONS_URL = f"{API_BASE}/push_subscriptions"
TIMEOUT_SECONDS = 20


class StravaAPIError(RuntimeError):
    """Non-retryable API error (4xx other than 401)."""


class StravaRateLimitError(StravaAPIError):
    """Strava rate limit hit (HTTP 429). Carries the Retry-After if present."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _client_creds() -> dict:
    """Return {client_id, client_secret} from env, or raise StravaAPIError."""
    cid = os.getenv("STRAVA_CLIENT_ID")
    secret = os.getenv("STRAVA_CLIENT_SECRET")
    if not cid or not secret:
        raise StravaAPIError("STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set")
    return {"client_id": cid, "client_secret": secret}


def _headers() -> dict:
    return {"Authorization": f"Bearer {auth.get_access_token()}"}


def _check_rate_limit(resp: requests.Response, path: str) -> None:
    if resp.status_code != 429:
        return
    usage = resp.headers.get("X-RateLimit-Usage", "?")
    limit = resp.headers.get("X-RateLimit-Limit", "?")
    retry_after = resp.headers.get("Retry-After")
    try:
        ra = int(retry_after) if retry_after else None
    except ValueError:
        ra = None
    msg = f"Strava rate limit on {path}: usage={usage}, limit={limit}, retry_after={retry_after}s"
    logger.warning(msg)
    raise StravaRateLimitError(msg, retry_after=ra)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _get(path: str, params: Optional[dict] = None) -> dict | list:
    url = f"{API_BASE}{path}"
    resp = requests.get(url, headers=_headers(), params=params or {}, timeout=TIMEOUT_SECONDS)
    if resp.status_code == 401:
        # Token went stale between get_access_token() and the request.
        # Force a refresh and retry once inline.
        logger.info("401 from Strava on %s; forcing token refresh", path)
        auth.get_access_token()  # refreshes if needed
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {auth.get_access_token()}"},
            params=params or {},
            timeout=TIMEOUT_SECONDS,
        )
    _check_rate_limit(resp, path)
    if resp.status_code >= 500:
        # tenacity retries on ConnectionError/Timeout — turn 5xx into Timeout.
        raise requests.Timeout(f"Strava 5xx on {path}: {resp.status_code}")
    if resp.status_code >= 400:
        raise StravaAPIError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# ---------- Activities ----------


def get_activity(activity_id: int, include_all_efforts: bool = True) -> dict:
    """Fetch full activity detail.

    Includes laps, splits_metric, splits_standard inline. `include_all_efforts`
    must be true to populate `best_efforts` (best 1mi/2mi/5K/10K within the run).
    """
    return _get(  # type: ignore[return-value]
        f"/activities/{activity_id}",
        params={"include_all_efforts": "true" if include_all_efforts else "false"},
    )


def list_activities(after: int, per_page: int = 30, max_pages: int = 10) -> list[dict]:
    """List athlete activities since `after` (unix timestamp). Returns SUMMARIES.

    Backfill must call get_activity() per ID to enrich with laps + splits.
    """
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        batch = _get(
            "/athlete/activities",
            params={"after": after, "per_page": per_page, "page": page},
        )
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < per_page:
            break
    return out


def get_athlete() -> dict:
    """GET /athlete — used by the `status` command for a quick whoami."""
    return _get("/athlete")  # type: ignore[return-value]


# ---------- Webhook subscriptions ----------


def subscribe_webhook(callback_url: str, verify_token: str) -> int:
    """Register a push subscription. Returns the subscription id.

    Strava limits to ONE subscription per API app. If a subscription
    already exists pointing somewhere else, this will fail with 400.
    Use `ensure_subscription()` for a self-healing version.
    """
    data = {**_client_creds(), "callback_url": callback_url, "verify_token": verify_token}
    resp = requests.post(SUBSCRIPTIONS_URL, data=data, timeout=TIMEOUT_SECONDS)
    if resp.status_code >= 400:
        raise StravaAPIError(f"subscribe failed: {resp.status_code} {resp.text}")
    return int(resp.json()["id"])


def list_subscriptions() -> list[dict]:
    resp = requests.get(SUBSCRIPTIONS_URL, params=_client_creds(), timeout=TIMEOUT_SECONDS)
    if resp.status_code >= 400:
        raise StravaAPIError(f"list subs failed: {resp.status_code} {resp.text}")
    body = resp.json()
    return body if isinstance(body, list) else []


def delete_subscription(sub_id: int) -> None:
    resp = requests.delete(
        f"{SUBSCRIPTIONS_URL}/{sub_id}",
        data=_client_creds(),
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise StravaAPIError(f"delete sub failed: {resp.status_code} {resp.text}")


def ensure_subscription(callback_url: str, verify_token: str) -> tuple[int, str]:
    """Self-healing subscribe.

    - If a subscription already exists with the same callback_url, return its id
      with action="kept".
    - If a subscription exists with a different callback_url, delete it and
      register the new one. Returns the new id with action="replaced".
    - If no subscription exists, register and return action="created".
    """
    existing = list_subscriptions()
    for sub in existing:
        if sub.get("callback_url") == callback_url:
            return int(sub["id"]), "kept"

    for sub in existing:
        sub_id = int(sub["id"])
        logger.info("Deleting stale subscription %s -> %s", sub_id, sub.get("callback_url"))
        delete_subscription(sub_id)

    new_id = subscribe_webhook(callback_url, verify_token)
    return new_id, ("replaced" if existing else "created")
