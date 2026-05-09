"""Strava API v3 client: activities + push subscriptions.

Auth is supplied by `strava.auth.get_access_token()` (refreshes transparently).
HTTP retries on connection / 5xx errors, mirrors the tenacity pattern used in
`conversation_store.py`.
"""

from __future__ import annotations

import logging
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
TIMEOUT_SECONDS = 20


class StravaAPIError(RuntimeError):
    """Non-retryable API error (4xx other than 401)."""


def _headers() -> dict:
    return {"Authorization": f"Bearer {auth.get_access_token()}"}


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
        logger.info("401 from Strava; forcing token refresh and retrying once")
        auth.get_access_token()  # refreshes if needed
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {auth.get_access_token()}"},
            params=params or {},
            timeout=TIMEOUT_SECONDS,
        )
    if resp.status_code >= 500:
        # tenacity retries on ConnectionError/Timeout but raise_for_status
        # surfaces a non-retryable HTTPError on 5xx; turn it into Timeout
        # to trigger the retry decorator instead.
        raise requests.Timeout(f"Strava 5xx: {resp.status_code}")
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


# ---------- Webhook subscriptions ----------


def subscribe_webhook(callback_url: str, verify_token: str) -> int:
    """Register a push subscription. Returns the subscription id.

    Strava will GET callback_url with hub.challenge before confirming —
    your app must echo the challenge back. There can only be one
    subscription per Strava API app.
    """
    import os

    cid = os.getenv("STRAVA_CLIENT_ID")
    secret = os.getenv("STRAVA_CLIENT_SECRET")
    resp = requests.post(
        "https://www.strava.com/api/v3/push_subscriptions",
        data={
            "client_id": cid,
            "client_secret": secret,
            "callback_url": callback_url,
            "verify_token": verify_token,
        },
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise StravaAPIError(f"subscribe failed: {resp.status_code} {resp.text}")
    return int(resp.json()["id"])


def list_subscriptions() -> list[dict]:
    import os

    cid = os.getenv("STRAVA_CLIENT_ID")
    secret = os.getenv("STRAVA_CLIENT_SECRET")
    resp = requests.get(
        "https://www.strava.com/api/v3/push_subscriptions",
        params={"client_id": cid, "client_secret": secret},
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise StravaAPIError(f"list subs failed: {resp.status_code} {resp.text}")
    return resp.json() if isinstance(resp.json(), list) else []


def delete_subscription(sub_id: int) -> None:
    import os

    cid = os.getenv("STRAVA_CLIENT_ID")
    secret = os.getenv("STRAVA_CLIENT_SECRET")
    resp = requests.delete(
        f"https://www.strava.com/api/v3/push_subscriptions/{sub_id}",
        data={"client_id": cid, "client_secret": secret},
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code >= 400:
        raise StravaAPIError(f"delete sub failed: {resp.status_code} {resp.text}")
