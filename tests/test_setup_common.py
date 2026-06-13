"""Tests for scripts/_setup_common.py — the OAuth setup scaffolding shared by
coros_setup.py and google_calendar_setup.py (issue #57).

The favicon-preload guard is the bug-prone bit: a one-shot loopback listener
that accepted the first GET would consume a browser's /favicon.ico preload and
miss the real ?code= callback. Extracting it means the guard is verified once
and both flows inherit it.
"""

from __future__ import annotations

from scripts._setup_common import is_oauth_callback, make_oauth_callback_handler


class TestIsOAuthCallback:
    def test_code_is_callback(self):
        assert is_oauth_callback({"code": "abc", "state": "x"}) is True

    def test_error_is_callback(self):
        assert is_oauth_callback({"error": "access_denied"}) is True

    def test_favicon_preload_is_not_callback(self):
        # GET /favicon.ico → no query params → must be ignored so the listener
        # keeps waiting for the real redirect.
        assert is_oauth_callback({}) is False

    def test_unrelated_params_are_not_callback(self):
        assert is_oauth_callback({"foo": "bar"}) is False


class TestMakeOAuthCallbackHandler:
    def test_factory_returns_distinct_handler_classes(self):
        coros = make_oauth_callback_handler("COROS")
        gcal = make_oauth_callback_handler("Google Calendar")
        # Each is a fresh BaseHTTPRequestHandler subclass with a do_GET.
        assert coros is not gcal
        assert hasattr(coros, "do_GET")
        assert hasattr(gcal, "do_GET")
