"""Tests for coros.client — bundle assembly, per-tool isolation, auth abort.

The MCP transport itself is monkeypatched; what's under test is the
load-bearing orchestration: an auth failure must abort the whole bundle
(so the watchdog classifies needs_auth) while any other per-tool failure
skips just that tool.
"""

from __future__ import annotations

import pytest

from coros import client
from coros.auth import CorosAuthError


class TestFetchDailyBundle:
    def test_auth_error_aborts_immediately(self, monkeypatch):
        calls = []

        def _fail(name, args):
            calls.append(name)
            raise CorosAuthError("dead token")

        monkeypatch.setattr(client, "call_tool_text", _fail)
        with pytest.raises(CorosAuthError):
            client.fetch_daily_bundle(days=2, timezone="UTC")
        assert calls == [client.BUNDLE_TOOLS[0]]  # no further tools attempted

    def test_per_tool_failure_skips_only_that_tool(self, monkeypatch):
        def _flaky(name, args):
            if name == "querySleepData":
                raise RuntimeError("boom")
            return "ok"

        monkeypatch.setattr(client, "call_tool_text", _flaky)
        bundle = client.fetch_daily_bundle(days=2, timezone="UTC")
        assert "querySleepData" not in bundle
        assert len(bundle) == len(client.BUNDLE_TOOLS) - 1

    def test_all_tools_down_returns_empty_bundle(self, monkeypatch):
        def _down(name, args):
            raise RuntimeError("mcp unreachable")

        monkeypatch.setattr(client, "call_tool_text", _down)
        assert client.fetch_daily_bundle(days=2, timezone="UTC") == {}


class TestBundleArgs:
    def test_recovery_takes_no_args(self):
        assert client._bundle_args("queryRecoveryStatus", 4, "UTC") == {}

    def test_training_load_takes_days_only(self):
        assert client._bundle_args("queryTrainingLoadAssessment", 4, "UTC") == {"days": 4}

    def test_sleep_requires_empty_date_range(self):
        args = client._bundle_args("querySleepData", 4, "America/New_York")
        assert args == {"days": 4, "timezone": "America/New_York", "startDate": "", "endDate": ""}

    def test_default_tools_take_days_and_timezone(self):
        args = client._bundle_args("queryDailyHealthData", 7, "America/New_York")
        assert args == {"days": 7, "timezone": "America/New_York"}


class TestDefaultTimezone:
    def test_user_timezone_env(self, monkeypatch):
        monkeypatch.setenv("USER_TIMEZONE", "America/New_York")
        assert client._default_timezone() == "America/New_York"

    def test_fallback_utc(self, monkeypatch):
        monkeypatch.delenv("USER_TIMEZONE", raising=False)
        assert client._default_timezone() == "UTC"
