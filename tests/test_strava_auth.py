"""Tests for strava.auth — primarily the Redis vs file backend split.

The HTTP paths (exchange_code_for_tokens, _refresh) are exercised manually
during the smoke test rather than mocked here; they're thin wrappers over
requests.post.
"""

from __future__ import annotations

import json
import time

import pytest

from strava import auth


@pytest.fixture
def file_backend(monkeypatch, tmp_path):
    """Force file backend with a temp token path."""
    monkeypatch.setenv("STRAVA_TOKENS_BACKEND", "file")
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    yield tmp_path / "tokens.json"


@pytest.fixture
def redis_backend(monkeypatch, fake_redis):
    """Force redis backend; fake_redis fixture (from conftest) wires the client."""
    monkeypatch.setenv("STRAVA_TOKENS_BACKEND", "redis")
    yield


# ---------- file backend ----------


class TestFileBackend:
    def test_read_returns_none_when_missing(self, file_backend):
        assert auth._read_tokens() is None

    def test_write_then_read(self, file_backend):
        tokens = {"refresh_token": "r1", "access_token": "a1", "expires_at": 9999999999}
        auth._write_tokens(tokens)
        assert auth._read_tokens() == tokens

    def test_write_sets_0600_mode(self, file_backend):
        auth._write_tokens({"refresh_token": "x", "access_token": "y", "expires_at": 1})
        mode = file_backend.stat().st_mode & 0o777
        assert mode == 0o600


# ---------- redis backend ----------


class TestRedisBackend:
    def test_read_returns_none_when_missing(self, redis_backend):
        assert auth._read_tokens() is None

    def test_write_then_read(self, redis_backend):
        tokens = {"refresh_token": "r1", "access_token": "a1", "expires_at": 9999999999}
        auth._write_tokens(tokens)
        assert auth._read_tokens() == tokens

    def test_write_persists_under_known_key(self, redis_backend, fake_redis):
        auth._write_tokens({"refresh_token": "x", "access_token": "y", "expires_at": 1})
        raw = fake_redis.get(auth.TOKENS_REDIS_KEY)
        assert raw is not None
        assert json.loads(raw)["refresh_token"] == "x"

    def test_does_not_touch_file(self, redis_backend, monkeypatch, tmp_path):
        # Point TOKEN_FILE at a tmp path. When backend is redis, writes
        # should go to Redis, leaving this file untouched.
        token_path = tmp_path / "tokens.json"
        monkeypatch.setattr(auth, "TOKEN_FILE", token_path)
        auth._write_tokens({"refresh_token": "x", "access_token": "y", "expires_at": 1})
        assert not token_path.exists()


# ---------- get_access_token ----------


class TestGetAccessToken:
    def test_raises_when_no_tokens(self, file_backend):
        with pytest.raises(auth.StravaAuthError):
            auth.get_access_token()

    def test_returns_cached_when_not_expired(self, file_backend):
        future = int(time.time()) + 3600
        auth._write_tokens({"refresh_token": "r", "access_token": "cached", "expires_at": future})
        assert auth.get_access_token() == "cached"

    def test_redis_backend_error_includes_redis_in_message(self, redis_backend):
        with pytest.raises(auth.StravaAuthError) as exc:
            auth.get_access_token()
        assert "Redis" in str(exc.value) or "redis" in str(exc.value)


class TestStorageUnreachableSurfaces:
    """When Redis itself is unreachable, the error message must NOT tell the
    user to re-auth — that's misleading. It should say infrastructure issue.
    """

    def test_redis_unreachable_raises_token_storage_unavailable(self, monkeypatch):
        monkeypatch.setenv("STRAVA_TOKENS_BACKEND", "redis")

        # Force conversation_store._get_redis to raise (simulate Redis down)
        import conversation_store

        def _boom():
            raise ConnectionError("connection refused")

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        with pytest.raises(auth.TokenStorageUnavailable):
            auth._read_tokens_redis()

    def test_get_access_token_translates_unavailable_to_infra_message(self, monkeypatch, tmp_path):
        # Redis-backend, with Redis down.
        monkeypatch.setenv("STRAVA_TOKENS_BACKEND", "redis")
        import conversation_store

        def _boom():
            raise ConnectionError("connection refused")

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        with pytest.raises(auth.StravaAuthError) as exc:
            auth.get_access_token()
        msg = str(exc.value)
        # The message should NOT tell the user to re-auth.
        assert "scripts/strava_setup.py auth" not in msg
        assert "infrastructure" in msg.lower() or "unreachable" in msg.lower() or "unavailable" in msg.lower()


class TestRefreshRetry:
    """The _refresh function should retry on transient network errors but
    propagate on Strava 4xx (e.g., revoked refresh token)."""

    def test_refresh_retries_on_connection_error(self, monkeypatch):
        import requests as _requests

        from strava import auth as _auth

        # Two ConnectionErrors then success
        attempts = {"n": 0}

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "access_token": "new_access",
                    "refresh_token": "rotated_refresh",
                    "expires_at": 9999999999,
                }

        def _post(url, data, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _requests.ConnectionError("flaky")
            return _Resp()

        monkeypatch.setenv("STRAVA_CLIENT_ID", "x")
        monkeypatch.setenv("STRAVA_CLIENT_SECRET", "y")
        monkeypatch.setattr(_requests, "post", _post)
        # Tenacity wraps the function; retry count includes the initial call.
        result = _auth._refresh.retry_with(stop=__import__("tenacity").stop_after_attempt(3))("rt")
        assert result["access_token"] == "new_access"
        assert attempts["n"] == 3

    def test_refresh_4xx_does_not_retry(self, monkeypatch):
        import requests as _requests

        from strava import auth as _auth

        attempts = {"n": 0}

        class _Resp:
            status_code = 400
            text = "invalid refresh"

            def json(self):
                return {}

        def _post(*a, **kw):
            attempts["n"] += 1
            return _Resp()

        monkeypatch.setenv("STRAVA_CLIENT_ID", "x")
        monkeypatch.setenv("STRAVA_CLIENT_SECRET", "y")
        monkeypatch.setattr(_requests, "post", _post)
        with pytest.raises(auth.StravaAuthError):
            _auth._refresh("rt")
        assert attempts["n"] == 1  # No retry on 4xx
