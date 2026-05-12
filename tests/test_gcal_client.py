"""Tests for google_calendar.client — error translation and 401 inline refresh.
The HTTP layer is mocked at the requests level."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from google_calendar import auth as gcal_auth
from google_calendar import client


@pytest.fixture(autouse=True)
def _gcal_env(monkeypatch):
    monkeypatch.setenv("GCAL_CLIENT_ID", "test_id")
    monkeypatch.setenv("GCAL_CLIENT_SECRET", "test_secret")
    monkeypatch.setenv("CALENDAR_ID", "test_cal_id@group.calendar.google.com")
    # Bypass real OAuth — return a static token.
    monkeypatch.setattr(gcal_auth, "get_access_token", lambda: "fake_access_token")


class TestInsertEvent:
    def test_409_raises_event_exists_error(self, monkeypatch):
        resp = MagicMock(status_code=409, text="duplicate id", content=b"")
        monkeypatch.setattr(requests, "request", MagicMock(return_value=resp))
        with pytest.raises(client.GcalEventExistsError):
            client.insert_event({"id": "pretrain20260511", "summary": "easy 4mi"})

    def test_200_returns_body(self, monkeypatch):
        body = {"id": "pretrain20260511", "summary": "easy 4mi"}
        resp = MagicMock(
            status_code=200,
            content=b"{}",
            json=lambda: body,
        )
        monkeypatch.setattr(requests, "request", MagicMock(return_value=resp))
        out = client.insert_event({"id": "pretrain20260511"})
        assert out == body


class TestPatchEvent:
    def test_patches_existing(self, monkeypatch):
        body = {"id": "pretrain20260511", "summary": "updated"}
        resp = MagicMock(status_code=200, content=b"{}", json=lambda: body)
        request_mock = MagicMock(return_value=resp)
        monkeypatch.setattr(requests, "request", request_mock)
        out = client.patch_event("pretrain20260511", {"summary": "updated"})
        assert out == body
        assert request_mock.call_args.args[0] == "PATCH"


class TestDeleteEvent:
    def test_204_succeeds(self, monkeypatch):
        resp = MagicMock(status_code=204)
        monkeypatch.setattr(requests, "delete", MagicMock(return_value=resp))
        client.delete_event("pretrain20260511")  # no exception

    def test_404_treated_as_success(self, monkeypatch):
        resp = MagicMock(status_code=404, text="not found")
        monkeypatch.setattr(requests, "delete", MagicMock(return_value=resp))
        client.delete_event("pretrain20260511")  # no exception

    def test_410_treated_as_success(self, monkeypatch):
        resp = MagicMock(status_code=410, text="gone")
        monkeypatch.setattr(requests, "delete", MagicMock(return_value=resp))
        client.delete_event("pretrain20260511")

    def test_other_error_raises(self, monkeypatch):
        resp = MagicMock(status_code=403, text="forbidden", headers={})
        monkeypatch.setattr(requests, "delete", MagicMock(return_value=resp))
        with pytest.raises(client.GcalAPIError):
            client.delete_event("pretrain20260511")


class TestRateLimit:
    def test_429_raises_rate_limit_error_with_retry_after(self, monkeypatch):
        resp = MagicMock(
            status_code=429,
            headers={"Retry-After": "30"},
            text="too many",
            content=b"",
        )
        monkeypatch.setattr(requests, "request", MagicMock(return_value=resp))
        with pytest.raises(client.GcalRateLimitError) as exc:
            client.insert_event({"id": "pretrain20260511"})
        assert exc.value.retry_after == 30


class TestUnauthorizedRetry:
    def test_401_forces_refresh_and_retries_once(self, monkeypatch):
        responses = [
            MagicMock(status_code=401, text="expired", content=b""),
            MagicMock(
                status_code=200,
                content=b"{}",
                json=lambda: {"id": "pretrain20260511"},
            ),
        ]
        request_mock = MagicMock(side_effect=responses)
        monkeypatch.setattr(requests, "request", request_mock)

        refresh_calls = {"n": 0}

        def _fake_token():
            refresh_calls["n"] += 1
            return "fake_access_token"

        monkeypatch.setattr(gcal_auth, "get_access_token", _fake_token)

        out = client.insert_event({"id": "pretrain20260511"})
        assert out["id"] == "pretrain20260511"
        # 2 underlying requests: original + retry after refresh.
        assert request_mock.call_count == 2


class TestCalendarMissing:
    def test_404_on_calendar_get_raises_missing(self, monkeypatch):
        resp = MagicMock(status_code=404, text="not found", content=b"")
        monkeypatch.setattr(requests, "request", MagicMock(return_value=resp))
        with pytest.raises(client.GcalCalendarMissingError):
            client.get_calendar("nonexistent_cal_id")


class TestCalendarIdRequired:
    def test_missing_calendar_id_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("CALENDAR_ID", raising=False)
        with pytest.raises(client.GcalAPIError) as exc:
            client._calendar_id()
        assert "CALENDAR_ID" in str(exc.value)


class TestListManagedEvents:
    def test_returns_items_list(self, monkeypatch):
        body = {"items": [{"id": "pretrain20260511"}, {"id": "pretrain20260512"}]}
        resp = MagicMock(status_code=200, content=b"{}", json=lambda: body)
        monkeypatch.setattr(requests, "request", MagicMock(return_value=resp))
        items = client.list_managed_events("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z")
        assert len(items) == 2
        assert items[0]["id"] == "pretrain20260511"

    def test_filters_via_private_extended_property(self, monkeypatch):
        body = {"items": []}
        resp = MagicMock(status_code=200, content=b"{}", json=lambda: body)
        request_mock = MagicMock(return_value=resp)
        monkeypatch.setattr(requests, "request", request_mock)
        client.list_managed_events("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z")
        kwargs = request_mock.call_args.kwargs
        assert kwargs["params"]["privateExtendedProperty"] == "pre_managed=1"
