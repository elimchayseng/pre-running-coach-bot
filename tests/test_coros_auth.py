"""Tests for coros.auth — backend split, blob shape, refresh-rotation safety.

Mirror of tests/test_gcal_auth.py with the COROS-specific additions: the blob
carries client_info alongside tokens (dynamic client registration — no env
credentials), and refresh ROTATES the refresh token, so persistence-before-
return and refresh serialization get explicit coverage.
"""

from __future__ import annotations

import json
import time

import pytest

from coros import auth


def _blob(expires_in: int = 3600 * 24 * 30, refresh: str = "rt-1") -> dict:
    return {
        "client_info": {"client_id": "cid-123"},
        "tokens": {
            "access_token": "at-1",
            "refresh_token": refresh,
            "expires_at": int(time.time()) + expires_in,
            "scope": "mcp.tools offline_access",
        },
    }


@pytest.fixture
def file_backend(monkeypatch, tmp_path):
    """Force file backend with a temp token path."""
    monkeypatch.setenv("COROS_TOKENS_BACKEND", "file")
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    yield tmp_path / "tokens.json"


@pytest.fixture
def redis_backend(monkeypatch, fake_redis):
    """Force redis backend; fake_redis fixture (from conftest) wires the client."""
    monkeypatch.setenv("COROS_TOKENS_BACKEND", "redis")
    yield


# ---------- file backend ----------


class TestFileBackend:
    def test_read_returns_none_when_missing(self, file_backend):
        assert auth._read_blob() is None

    def test_write_then_read_roundtrip(self, file_backend):
        blob = _blob()
        auth._write_blob(blob)
        assert auth._read_blob() == blob

    def test_write_is_atomic_no_tmp_left_behind(self, file_backend):
        auth._write_blob(_blob())
        leftovers = [p for p in file_backend.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_file_mode_0600(self, file_backend):
        auth._write_blob(_blob())
        assert (file_backend.stat().st_mode & 0o777) == 0o600

    def test_corrupt_file_reads_as_none(self, file_backend):
        file_backend.write_text("{not json")
        assert auth._read_blob() is None


# ---------- redis backend ----------


class TestRedisBackend:
    def test_read_returns_none_when_missing(self, redis_backend):
        assert auth._read_blob() is None

    def test_write_then_read_roundtrip(self, redis_backend):
        blob = _blob()
        auth._write_blob(blob)
        assert auth._read_blob() == blob

    def test_redis_down_raises_storage_unavailable(self, monkeypatch):
        monkeypatch.setenv("COROS_TOKENS_BACKEND", "redis")

        def _boom():
            raise ConnectionError("redis down")

        import conversation_store

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        with pytest.raises(auth.TokenStorageUnavailable):
            auth._read_blob()

    def test_get_access_token_wraps_storage_unavailable(self, monkeypatch):
        monkeypatch.setenv("COROS_TOKENS_BACKEND", "redis")

        def _boom():
            raise ConnectionError("redis down")

        import conversation_store

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        with pytest.raises(auth.CorosAuthError) as exc:
            auth.get_access_token()
        # Must NOT tell the user to re-auth — tokens aren't lost.
        assert "Infrastructure issue" in str(exc.value)


# ---------- get_access_token ----------


class TestGetAccessToken:
    def test_no_tokens_raises_with_setup_hint(self, file_backend):
        with pytest.raises(auth.CorosAuthError) as exc:
            auth.get_access_token()
        assert "coros_setup.py auth" in str(exc.value)

    def test_missing_client_info_raises(self, file_backend):
        blob = _blob()
        del blob["client_info"]
        auth._write_blob(blob)
        with pytest.raises(auth.CorosAuthError):
            auth.get_access_token()

    def test_fresh_token_returned_without_refresh(self, file_backend, monkeypatch):
        auth._write_blob(_blob())

        def _no_refresh(*a, **k):
            raise AssertionError("refresh should not be called for a fresh token")

        monkeypatch.setattr(auth, "_refresh_request", _no_refresh)
        assert auth.get_access_token() == "at-1"

    def test_stale_token_triggers_refresh_and_persists_rotation(self, file_backend, monkeypatch):
        auth._write_blob(_blob(expires_in=10))  # inside leeway

        monkeypatch.setattr(
            auth,
            "_refresh_request",
            lambda rt, cid: {
                "access_token": "at-2",
                "refresh_token": "rt-2",  # rotated
                "expires_in": 2591999,
            },
        )
        assert auth.get_access_token() == "at-2"
        # The rotated refresh token MUST be on disk before we returned.
        stored = json.loads(file_backend.read_text())
        assert stored["tokens"]["refresh_token"] == "rt-2"
        assert stored["tokens"]["access_token"] == "at-2"

    def test_refresh_without_rotation_keeps_old_refresh_token(self, file_backend, monkeypatch):
        auth._write_blob(_blob(expires_in=10))
        monkeypatch.setattr(
            auth,
            "_refresh_request",
            lambda rt, cid: {"access_token": "at-2", "expires_in": 100},  # no refresh_token
        )
        auth.get_access_token()
        stored = json.loads(file_backend.read_text())
        assert stored["tokens"]["refresh_token"] == "rt-1"

    def test_concurrent_threads_refresh_once(self, file_backend, monkeypatch):
        """Refresh tokens are single-use (rotation) — N threads racing a stale
        token must produce exactly one refresh request."""
        import threading

        auth._write_blob(_blob(expires_in=10))
        calls = []

        def _refresh(rt, cid):
            calls.append(rt)
            time.sleep(0.05)  # widen the race window
            return {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 2591999}

        monkeypatch.setattr(auth, "_refresh_request", _refresh)
        results = []
        threads = [threading.Thread(target=lambda: results.append(auth.get_access_token())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == ["at-2"] * 4
        assert len(calls) == 1


# ---------- token response normalization ----------


class TestTokenNormalization:
    def test_expires_in_becomes_expires_at(self):
        before = int(time.time())
        tokens = auth._tokens_from_response(
            {"access_token": "a", "refresh_token": "r", "expires_in": 100, "scope": "s"}
        )
        assert before + 100 <= tokens["expires_at"] <= int(time.time()) + 100

    def test_missing_refresh_token_raises(self):
        with pytest.raises(auth.CorosAuthError) as exc:
            auth._tokens_from_response({"access_token": "a", "expires_in": 100})
        assert "offline_access" in str(exc.value)


# ---------- health check ----------


class TestHealthCheck:
    def test_true_when_token_fresh(self, file_backend):
        auth._write_blob(_blob())
        assert auth.health_check() is True

    def test_false_when_no_tokens(self, file_backend):
        assert auth.health_check() is False
