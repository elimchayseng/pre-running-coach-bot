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
