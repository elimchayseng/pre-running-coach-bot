"""Google Calendar API v3 client: events + calendar metadata.

Auth is supplied by `google_calendar.auth.get_access_token()` (refreshes
transparently). HTTP retries on connection / 5xx errors via tenacity. 401
forces a one-shot token refresh + retry. 429 raises GcalRateLimitError so the
caller can back off intelligently.

All event operations target `os.getenv("CALENDAR_ID")` — the dedicated
"PRE Training" calendar the user creates in the Calendar UI. We never touch
any other calendar.
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

logger = logging.getLogger("pre_coach.gcal.client")

API_BASE = "https://www.googleapis.com/calendar/v3"
TIMEOUT_SECONDS = 20


class GcalAPIError(RuntimeError):
    """Non-retryable API error (4xx other than 401/404/409/410/429)."""


class GcalRateLimitError(GcalAPIError):
    """Gcal rate limit hit (HTTP 429). Carries the Retry-After if present."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class GcalEventExistsError(GcalAPIError):
    """Insert hit a 409 — event with this id already exists. Caller should
    fall through to patch_event."""


class GcalCalendarMissingError(GcalAPIError):
    """Calendar 404 on a request targeting a specific calendar id. The user
    deleted the calendar or CALENDAR_ID is wrong — don't silently recreate."""


def _calendar_id() -> str:
    cid = os.getenv("CALENDAR_ID")
    if not cid:
        raise GcalAPIError("CALENDAR_ID must be set (the dedicated PRE Training calendar id)")
    return cid


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }


def _check_rate_limit(resp: requests.Response, path: str) -> None:
    if resp.status_code != 429:
        return
    retry_after = resp.headers.get("Retry-After")
    try:
        ra = int(retry_after) if retry_after else None
    except ValueError:
        ra = None
    msg = f"Gcal rate limit on {path}: retry_after={retry_after}s"
    logger.warning(msg)
    raise GcalRateLimitError(msg, retry_after=ra)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
)
def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
) -> Optional[dict]:
    """Issue an authenticated request. Returns parsed JSON, or None for 204.

    - 401 → force refresh + retry once inline.
    - 404 → GcalCalendarMissingError (when path is calendar-scoped) or
            None (DELETE event 404 is success — handled by callers).
    - 409 → GcalEventExistsError (insert idempotency conflict).
    - 410 → None (already deleted).
    - 429 → GcalRateLimitError.
    - 5xx → reraise as requests.Timeout so tenacity retries.
    - other 4xx → GcalAPIError.
    """
    url = f"{API_BASE}{path}"
    resp = requests.request(
        method,
        url,
        headers=_headers(),
        params=params or {},
        json=json,
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code == 401:
        logger.info("401 from Gcal on %s; forcing token refresh", path)
        auth.get_access_token()  # refreshes if needed
        resp = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {auth.get_access_token()}",
                "Content-Type": "application/json",
            },
            params=params or {},
            json=json,
            timeout=TIMEOUT_SECONDS,
        )
    _check_rate_limit(resp, path)
    if resp.status_code == 409:
        raise GcalEventExistsError(f"{method} {path} -> 409 conflict: {resp.text[:200]}")
    if resp.status_code == 410:
        return None  # gone (already deleted) — treat as success
    if resp.status_code >= 500:
        raise requests.Timeout(f"Gcal 5xx on {path}: {resp.status_code}")
    if resp.status_code == 404:
        # 404 on event delete is "already gone" — let the caller decide.
        # On other operations, propagate as missing-calendar when path is
        # calendar-scoped.
        if "/calendars/" in path:
            raise GcalCalendarMissingError(
                f"{method} {path} -> 404. Calendar id may be wrong or the calendar was deleted. Check CALENDAR_ID."
            )
        raise GcalAPIError(f"{method} {path} -> 404: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise GcalAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


# ---------- Events ----------


def insert_event(event: dict) -> dict:
    """POST /calendars/{cid}/events. Raises GcalEventExistsError on 409."""
    cid = _calendar_id()
    result = _request("POST", f"/calendars/{cid}/events", json=event)
    return result or {}


def patch_event(event_id: str, patch: dict) -> dict:
    """PATCH /calendars/{cid}/events/{event_id}."""
    cid = _calendar_id()
    result = _request("PATCH", f"/calendars/{cid}/events/{event_id}", json=patch)
    return result or {}


def delete_event(event_id: str) -> None:
    """DELETE /calendars/{cid}/events/{event_id}.

    Treats 404/410 as success (already gone). Calendar-level 404 is not
    possible here because the path is event-scoped.
    """
    cid = _calendar_id()
    url = f"{API_BASE}/calendars/{cid}/events/{event_id}"
    # Use raw request so we can swallow 404 cleanly without the calendar-404
    # special-case in _request triggering.
    resp = requests.delete(url, headers=_headers(), timeout=TIMEOUT_SECONDS)
    if resp.status_code == 401:
        auth.get_access_token()
        resp = requests.delete(
            url,
            headers={"Authorization": f"Bearer {auth.get_access_token()}"},
            timeout=TIMEOUT_SECONDS,
        )
    if resp.status_code in (200, 204, 404, 410):
        return
    _check_rate_limit(resp, f"DELETE /events/{event_id}")
    if resp.status_code >= 500:
        raise requests.Timeout(f"Gcal 5xx on delete {event_id}: {resp.status_code}")
    raise GcalAPIError(f"DELETE event {event_id} -> {resp.status_code}: {resp.text[:200]}")


def list_managed_events(time_min: str, time_max: str) -> list[dict]:
    """List events on the calendar marked with our private extended property
    `pre_managed=1`. Returns a single page (max 2500 events) — more than
    enough for ±60 days of daily events.

    Args:
        time_min: ISO 8601 timestamp (RFC3339), e.g. "2026-04-01T00:00:00Z".
        time_max: same format.
    """
    cid = _calendar_id()
    body = _request(
        "GET",
        f"/calendars/{cid}/events",
        params={
            "privateExtendedProperty": "pre_managed=1",
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "maxResults": 2500,
        },
    )
    if not body:
        return []
    items = body.get("items", [])
    return items if isinstance(items, list) else []


def get_calendar(calendar_id: str) -> dict:
    """GET /calendars/{cid}. Used by status to verify access."""
    body = _request("GET", f"/calendars/{calendar_id}")
    return body or {}
