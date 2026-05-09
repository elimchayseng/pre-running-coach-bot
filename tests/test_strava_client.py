"""Tests for strava.client — webhook subscription idempotency and rate-limit
handling. The HTTP layer is mocked at the requests level."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from strava import client


@pytest.fixture(autouse=True)
def _strava_creds(monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "test_id")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "test_secret")


# ---------- ensure_subscription idempotency ----------


class TestEnsureSubscription:
    def test_creates_when_no_existing(self, monkeypatch):
        list_resp = MagicMock(status_code=200, json=lambda: [])
        post_resp = MagicMock(status_code=201, json=lambda: {"id": 555})
        monkeypatch.setattr(requests, "get", MagicMock(return_value=list_resp))
        monkeypatch.setattr(requests, "post", MagicMock(return_value=post_resp))

        sub_id, action = client.ensure_subscription("https://example.com/strava/webhook", "vt")
        assert sub_id == 555
        assert action == "created"

    def test_keeps_when_callback_matches(self, monkeypatch):
        existing = [{"id": 999, "callback_url": "https://example.com/strava/webhook"}]
        list_resp = MagicMock(status_code=200, json=lambda: existing)
        post_call = MagicMock()
        delete_call = MagicMock()
        monkeypatch.setattr(requests, "get", MagicMock(return_value=list_resp))
        monkeypatch.setattr(requests, "post", post_call)
        monkeypatch.setattr(requests, "delete", delete_call)

        sub_id, action = client.ensure_subscription("https://example.com/strava/webhook", "vt")
        assert sub_id == 999
        assert action == "kept"
        post_call.assert_not_called()
        delete_call.assert_not_called()

    def test_replaces_stale(self, monkeypatch):
        existing = [{"id": 111, "callback_url": "https://OLD.example.com/strava/webhook"}]
        list_resp = MagicMock(status_code=200, json=lambda: existing)
        delete_resp = MagicMock(status_code=204)
        post_resp = MagicMock(status_code=201, json=lambda: {"id": 222})
        get_mock = MagicMock(return_value=list_resp)
        delete_mock = MagicMock(return_value=delete_resp)
        post_mock = MagicMock(return_value=post_resp)
        monkeypatch.setattr(requests, "get", get_mock)
        monkeypatch.setattr(requests, "delete", delete_mock)
        monkeypatch.setattr(requests, "post", post_mock)

        sub_id, action = client.ensure_subscription("https://NEW.example.com/strava/webhook", "vt")
        assert sub_id == 222
        assert action == "replaced"
        delete_mock.assert_called_once()
        post_mock.assert_called_once()


# ---------- 429 handling ----------


class TestRateLimit:
    def test_429_raises_StravaRateLimitError_with_retry_after(self, monkeypatch):
        # First, mock get_access_token so we don't actually try Strava OAuth.
        from strava import auth as _auth

        monkeypatch.setattr(_auth, "get_access_token", lambda: "fake_token")

        resp = MagicMock(
            status_code=429,
            headers={"Retry-After": "60", "X-RateLimit-Usage": "100,1000", "X-RateLimit-Limit": "100,1000"},
            text="Too Many Requests",
        )
        monkeypatch.setattr(requests, "get", MagicMock(return_value=resp))
        with pytest.raises(client.StravaRateLimitError) as exc:
            client._get("/athlete")
        assert exc.value.retry_after == 60


# ---------- _client_creds error path ----------


class TestClientCreds:
    def test_missing_creds_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
        monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
        with pytest.raises(client.StravaAPIError) as exc:
            client._client_creds()
        assert "STRAVA_CLIENT_ID" in str(exc.value) or "STRAVA_CLIENT_SECRET" in str(exc.value)
