"""Tests for the Notion mirror plumbing (Phase 1B.1): client, schema, bootstrap.

No live API — the client's HTTP layer is exercised with a fake requests
session.
"""

from __future__ import annotations

import pytest

from notion import schema
from notion.client import NotionClient, NotionError, _backoff, _retry_after_seconds, enabled
from scripts.notion_bootstrap import _find_database, _norm, _plain_title

# ---------------- schema ----------------


class TestSchema:
    def test_source_key_helpers(self):
        assert schema.session_key(7) == "sid:7"
        assert schema.journal_key(3) == "jid:3"
        assert schema.plan_change_key(12) == "cid:12"
        assert schema.review_key(1) == "rid:1"

    def test_sessions_properties_shape(self):
        props = schema.SESSIONS_PROPERTIES
        assert props["Title"] == {"title": {}}
        assert props["Date"] == {"date": {}}
        assert schema.SOURCE_KEY in props
        assert props["Status"]["select"]["options"][0]["name"] == "planned"
        assert props["Strava URL"] == {"url": {}}

    def test_journal_has_tags_multiselect(self):
        tags = schema.JOURNAL_PROPERTIES["Tags"]["multi_select"]["options"]
        assert {o["name"] for o in tags} == {"travel", "illness", "soreness", "life", "decision"}

    def test_relation_properties_carry_data_source_id(self):
        pc = schema.plan_changes_properties("ds-abc")
        assert pc["Triggered by"]["relation"]["data_source_id"] == "ds-abc"
        rv = schema.reviews_properties("ds-abc")
        assert rv["Session"]["relation"]["data_source_id"] == "ds-abc"


# ---------------- client: helpers ----------------


class TestClientHelpers:
    def test_enabled_reflects_token(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        assert enabled() is False
        monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
        assert enabled() is True

    def test_headers_pin_version(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "ntn_secret")
        c = NotionClient(version="2026-03-11")
        h = c._headers()
        assert h["Authorization"] == "Bearer ntn_secret"
        assert h["Notion-Version"] == "2026-03-11"

    def test_backoff_grows(self):
        assert _backoff(0) < _backoff(2) + 1  # 1s-ish < 4s-ish range

    def test_retry_after_caps_and_defaults(self):
        class _R:
            headers = {"Retry-After": "999"}

        assert _retry_after_seconds(_R()) == 30.0  # capped

        class _R2:
            headers: dict = {}

        assert _retry_after_seconds(_R2()) == 1.0  # default


# ---------------- client: HTTP layer ----------------


class _FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = str(self._body)

    def json(self):
        return self._body


class _FakeSession:
    """Replays a queued list of responses, recording each request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url, json))
        return self._responses.pop(0)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
    monkeypatch.setattr("notion.client.time.sleep", lambda *_: None)
    return NotionClient()


class TestClientRequest:
    def test_success_returns_json(self, client):
        client._session = _FakeSession([_FakeResp(200, {"id": "u1", "name": "PRE"})])
        assert client.users_me() == {"id": "u1", "name": "PRE"}

    def test_4xx_raises_notion_error(self, client):
        client._session = _FakeSession([_FakeResp(400, {"message": "bad request"})])
        with pytest.raises(NotionError) as exc:
            client.users_me()
        assert exc.value.status == 400

    def test_429_is_retried_then_succeeds(self, client):
        client._session = _FakeSession(
            [
                _FakeResp(429, {}, headers={"Retry-After": "0"}),
                _FakeResp(200, {"id": "ok"}),
            ]
        )
        assert client.users_me()["id"] == "ok"

    def test_5xx_is_retried_then_succeeds(self, client):
        client._session = _FakeSession([_FakeResp(503, {}), _FakeResp(200, {"id": "ok"})])
        assert client.users_me()["id"] == "ok"

    def test_429_out_of_retries_raises(self, client):
        client._session = _FakeSession([_FakeResp(429, {}, headers={"Retry-After": "0"})] * 4)
        with pytest.raises(NotionError):
            client.users_me()

    def test_create_database_payload_shape(self, client):
        session = _FakeSession([_FakeResp(200, {"id": "db1", "data_sources": [{"id": "ds1"}]})])
        client._session = session
        client.create_database("page1", "PRE Sessions", schema.SESSIONS_PROPERTIES)
        method, url, payload = session.calls[0]
        assert method == "POST" and url.endswith("/databases")
        assert payload["parent"] == {"type": "page_id", "page_id": "page1"}
        assert payload["initial_data_source"]["properties"] is schema.SESSIONS_PROPERTIES


# ---------------- bootstrap helpers ----------------


class TestBootstrapHelpers:
    def test_plain_title_prefers_plain_text(self):
        assert _plain_title([{"plain_text": "PRE Sessions"}]) == "PRE Sessions"

    def test_plain_title_falls_back_to_text_content(self):
        assert _plain_title([{"text": {"content": "PRE Journal"}}]) == "PRE Journal"

    def test_norm_strips_dashes(self):
        assert _norm("ab-cd-ef") == _norm("abcdef") == "abcdef"

    def test_find_database_matches_data_source_under_parent(self):
        class _C:
            def search(self, query):
                return {
                    "results": [
                        {
                            "object": "data_source",
                            "id": "ds-1",
                            "title": [{"plain_text": "PRE Sessions"}],
                            "parent": {"type": "database_id", "database_id": "db-1"},
                            "database_parent": {"type": "page_id", "page_id": "parent-page"},
                        }
                    ]
                }

        assert _find_database(_C(), "PRE Sessions", "parent-page") == ("db-1", "ds-1")

    def test_find_database_ignores_wrong_page_and_trash(self):
        class _C:
            def search(self, query):
                return {
                    "results": [
                        {
                            "object": "data_source",
                            "id": "ds-x",
                            "title": [{"plain_text": "PRE Sessions"}],
                            "parent": {"database_id": "db-x"},
                            "database_parent": {"page_id": "some-other-page"},
                        },
                        {
                            "object": "data_source",
                            "id": "ds-trash",
                            "in_trash": True,
                            "title": [{"plain_text": "PRE Sessions"}],
                            "parent": {"database_id": "db-t"},
                            "database_parent": {"page_id": "parent-page"},
                        },
                    ]
                }

        assert _find_database(_C(), "PRE Sessions", "parent-page") is None
