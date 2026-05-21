"""Thin Notion REST client.

A hand-rolled ``requests`` wrapper rather than the ``notion-client`` SDK: the
2026-03-11 API (data-sources model, ``/v1/views``, the Markdown API) is new
enough that the SDK lags it, and the mirror needs exact control over the
``Notion-Version`` header and the new endpoints.

SQLite is the source of truth — every Notion write here is best-effort. The
caller (notion/mirror.py, added in Phase 1B.2) runs writes in a daemon thread
and swallows failures, so a Notion outage never breaks the bot.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Optional

import requests

logger = logging.getLogger("pre_coach.notion.client")

API_BASE = "https://api.notion.com/v1"
# The 2026-05-13 platform release ships under this API version. Pinned so a
# future Notion version bump can't silently change request/response shapes.
DEFAULT_API_VERSION = "2026-03-11"

_MAX_RETRIES = 3
_TIMEOUT_S = 30


class NotionError(Exception):
    """A Notion API call failed (non-retryable, or out of retries)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    """True when a Notion token is configured. Every mirror call site
    short-circuits on this so the bot runs fine without Notion wired up."""
    return bool(os.getenv("NOTION_TOKEN"))


class NotionClient:
    """Minimal Notion API client scoped to what the mirror needs."""

    def __init__(self, token: Optional[str] = None, version: Optional[str] = None):
        self.token = token or os.getenv("NOTION_TOKEN") or ""
        self.version = version or os.getenv("NOTION_API_VERSION") or DEFAULT_API_VERSION
        self._session = requests.Session()

    # ---------- transport ----------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.version,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        """Issue one request with retry on 429 / 5xx.

        - 429: honour ``Retry-After`` (capped), retry up to 3 times.
        - 5xx: exponential backoff (1s, 2s, 4s) + jitter, up to 3 times.
        - other 4xx: raise ``NotionError`` immediately (caller swallows).
        """
        url = f"{API_BASE}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, json=payload, headers=self._headers(), timeout=_TIMEOUT_S)
            except requests.RequestException as e:
                last_exc = e
                if attempt == _MAX_RETRIES:
                    raise NotionError(f"network error after {attempt} retries: {e}") from e
                time.sleep(_backoff(attempt))
                continue

            if resp.status_code == 429:
                if attempt == _MAX_RETRIES:
                    raise NotionError("rate limited (429) — out of retries", status=429)
                retry_after = _retry_after_seconds(resp)
                logger.warning("Notion 429; sleeping %.1fs (attempt %d)", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue

            if 500 <= resp.status_code < 600:
                if attempt == _MAX_RETRIES:
                    raise NotionError(f"server error {resp.status_code} — out of retries", status=resp.status_code)
                time.sleep(_backoff(attempt))
                continue

            if resp.status_code >= 400:
                raise NotionError(f"{method} {path} -> {resp.status_code}: {_err_text(resp)}", status=resp.status_code)

            return resp.json() if resp.content else {}

        # Unreachable, but keeps the type checker happy.
        raise NotionError(f"{method} {path} failed: {last_exc}")

    # ---------- endpoints ----------

    def users_me(self) -> dict:
        """GET /users/me — the integration's own bot user. Used as a health probe."""
        return self._request("GET", "/users/me")

    def search(self, query: str = "", filter_: Optional[dict] = None) -> dict:
        """POST /search — find objects the integration can see.

        Paginates: ``/search`` returns at most 100 results per page, so a
        single call could miss a match at workspace scale. Follows
        ``next_cursor`` until ``has_more`` is false (capped at 20 pages so a
        huge workspace can't spin forever). Returns all results aggregated
        under ``{"results": [...]}``.
        """
        results: list = []
        cursor: Optional[str] = None
        for _ in range(20):
            payload: dict[str, Any] = {"query": query}
            if filter_:
                payload["filter"] = filter_
            if cursor:
                payload["start_cursor"] = cursor
            page = self._request("POST", "/search", payload)
            results.extend(page.get("results", []))
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return {"results": results}

    def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        """POST /databases — create a database with one initial data source.

        Returns the database object; the data source id is at
        ``response["data_sources"][0]["id"]``.
        """
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": properties},
        }
        return self._request("POST", "/databases", payload)

    def retrieve_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def create_page(self, data_source_id: str, properties: dict, markdown: Optional[str] = None) -> dict:
        """POST /pages — create a page under a data source, optionally with a
        Markdown body."""
        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if markdown:
            payload["markdown"] = markdown
        return self._request("POST", "/pages", payload)

    def query_data_source(self, data_source_id: str, filter_: Optional[dict] = None) -> dict:
        """POST /data_sources/:id/query — query rows.

        Not paginated: the mirror only ever filters on the unique source_key,
        which yields 0 or 1 result. A general paginating query is a Phase 2
        concern (bidirectional sync).
        """
        payload: dict[str, Any] = {}
        if filter_:
            payload["filter"] = filter_
        return self._request("POST", f"/data_sources/{data_source_id}/query", payload)

    def update_page(self, page_id: str, properties: Optional[dict] = None, in_trash: Optional[bool] = None) -> dict:
        """PATCH /pages/:id — update properties and/or trash the page."""
        payload: dict[str, Any] = {}
        if properties is not None:
            payload["properties"] = properties
        if in_trash is not None:
            payload["in_trash"] = in_trash
        return self._request("PATCH", f"/pages/{page_id}", payload)

    def replace_page_markdown(self, page_id: str, markdown: str) -> dict:
        """PATCH /pages/:id/markdown — replace the whole page body."""
        return self._request(
            "PATCH",
            f"/pages/{page_id}/markdown",
            {
                "type": "replace_content",
                "replace_content": {"new_str": markdown, "allow_deleting_content": True},
            },
        )

    def create_view(
        self,
        database_id: str,
        data_source_id: str,
        name: str,
        view_type: str,
        filter_: Optional[dict] = None,
        sorts: Optional[list] = None,
        extra: Optional[dict] = None,
    ) -> dict:
        """POST /views — add a view to an existing database."""
        payload: dict[str, Any] = {
            "database_id": database_id,
            "data_source_id": data_source_id,
            "name": name,
            "type": view_type,
        }
        if filter_:
            payload["filter"] = filter_
        if sorts:
            payload["sorts"] = sorts
        if extra:
            payload.update(extra)
        return self._request("POST", "/views", payload)

    def list_views(self, database_id: str) -> dict:
        """GET /views?database_id=:id — list views on a database.

        Two-stage because Notion's list endpoint only returns ``{object, id}``
        per result. The bootstrap dedupes on ``name``, so we fetch each view
        individually (``GET /views/:id``) to populate the full object. A
        mirror DB has ~5 views; the per-view fetches are well under Notion's
        3 req/s limit, and the bootstrap runs manually off the request path.
        """
        ids: list[str] = []
        cursor: Optional[str] = None
        for _ in range(20):
            path = f"/views?database_id={database_id}"
            if cursor:
                path = f"{path}&start_cursor={cursor}"
            page = self._request("GET", path)
            for v in page.get("results", []):
                vid = v.get("id")
                if vid:
                    ids.append(vid)
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break
        results = [self._request("GET", f"/views/{vid}") for vid in ids]
        return {"results": results}


# ---------- helpers ----------


def _backoff(attempt: int) -> float:
    """Exponential backoff (1s, 2s, 4s) with jitter."""
    return (2**attempt) + random.uniform(0, 0.5)


def _retry_after_seconds(resp: requests.Response) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        return min(float(raw), 30.0) if raw else 1.0
    except (TypeError, ValueError):
        return 1.0


def _err_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body)
    except ValueError:
        return resp.text[:300]
