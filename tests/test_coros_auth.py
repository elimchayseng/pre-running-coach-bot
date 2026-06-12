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


@pytest.fixture(autouse=True)
def _reset_unsaved_blob():
    """get_access_token holds a module-level fallback blob when persistence
    fails — never let one test's state leak into the next."""
    auth._unsaved_blob = None
    yield
    auth._unsaved_blob = None


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

    def test_refresh_4xx_not_retried(self, file_backend, monkeypatch):
        """4xx means the refresh token is dead — tenacity must not re-present
        it (each retry of a single-use token makes things worse)."""
        calls = []

        class _Resp:
            status_code = 400
            text = "invalid_grant"

        monkeypatch.setattr(auth.requests, "post", lambda *a, **k: calls.append(1) or _Resp())
        with pytest.raises(auth.CorosAuthError):
            auth._refresh_request("rt", "cid")
        assert len(calls) == 1

    def test_refresh_timeout_not_retried(self, monkeypatch):
        """A read timeout may mean COROS already rotated the token server-side;
        retrying would re-present the consumed token. Timeout must propagate."""
        calls = []

        def _timeout(*a, **k):
            calls.append(1)
            raise auth.requests.Timeout("read timed out")

        monkeypatch.setattr(auth.requests, "post", _timeout)
        with pytest.raises(auth.requests.Timeout):
            auth._refresh_request("rt", "cid")
        assert len(calls) == 1

    def test_adopts_token_rotated_by_another_process(self, file_backend, monkeypatch):
        """Cross-process race heal: refresh 4xxs because another process
        (e.g. make coros-status-prod) already spent the refresh token —
        re-read storage and adopt the newer grant instead of failing."""
        auth._write_blob(_blob(expires_in=10, refresh="rt-old"))

        def _refresh(rt, cid):
            if rt == "rt-old":
                raise auth.CorosAuthError("token refresh failed: 400 invalid_grant")
            return {"access_token": "at-new", "refresh_token": "rt-newer", "expires_in": 2591999}

        monkeypatch.setattr(auth, "_refresh_request", _refresh)
        # Simulate the other process's rotation landing in storage between
        # our read and our failed refresh: patch _read_blob to return the
        # rotated blob on the in-lock re-read after failure.
        rotated = _blob(expires_in=10, refresh="rt-rotated")
        reads = []
        real_read = auth._read_blob

        def _read():
            reads.append(1)
            # First reads see the stale blob; after the failed refresh the
            # adoption re-read sees the rotated one.
            return rotated if len(reads) >= 3 else real_read()

        monkeypatch.setattr(auth, "_read_blob", _read)
        assert auth.get_access_token() == "at-new"

    def test_write_failure_keeps_rotated_token_in_memory(self, file_backend, monkeypatch):
        """If persisting the rotated blob fails, the token must survive in
        memory (and flush later) — losing it means permanent lockout."""
        auth._unsaved_blob = None
        auth._write_blob(_blob(expires_in=10))
        monkeypatch.setattr(
            auth,
            "_refresh_request",
            lambda rt, cid: {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 2591999},
        )
        real_write = auth._write_blob_file
        fail_next = {"on": True}

        def _flaky_write(blob):
            if fail_next["on"]:
                raise auth.TokenStorageUnavailable("redis blip")
            real_write(blob)

        monkeypatch.setattr(auth, "_write_blob", _flaky_write)
        # Refresh succeeds, persist fails -> token still returned, blob held.
        assert auth.get_access_token() == "at-2"
        assert auth._unsaved_blob["tokens"]["refresh_token"] == "rt-2"
        # Next call flushes the held blob once the store recovers.
        fail_next["on"] = False
        assert auth.get_access_token() == "at-2"
        assert auth._unsaved_blob is None
        auth._unsaved_blob = None  # leave no state for other tests

    def test_register_client_failure_raises(self, monkeypatch):
        class _Resp:
            status_code = 400
            text = "bad request"

        monkeypatch.setattr(auth.requests, "post", lambda *a, **k: _Resp())
        with pytest.raises(auth.CorosAuthError):
            auth.register_client("http://localhost:1/cb")

    def test_exchange_persists_blob_get_access_token_can_read(self, file_backend, monkeypatch):
        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"access_token": "at-x", "refresh_token": "rt-x", "expires_in": 2591999, "scope": "s"}

        monkeypatch.setattr(auth.requests, "post", lambda *a, **k: _Resp())
        auth.exchange_code_for_tokens("code", "http://localhost:1/cb", "verifier", {"client_id": "cid-9"})
        assert auth.get_access_token() == "at-x"


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

    def test_missing_expires_in_gets_conservative_floor(self):
        """Without the floor, expires_at == now and EVERY call would
        refresh-and-rotate — multiplying the rotation-loss window."""
        import time as _time

        tokens = auth._tokens_from_response({"access_token": "a", "refresh_token": "r"})
        assert tokens["expires_at"] >= int(_time.time()) + auth.FALLBACK_EXPIRES_IN - 5


# ---------- health check ----------


class TestHealthCheck:
    def test_true_when_token_fresh(self, file_backend):
        auth._write_blob(_blob())
        assert auth.health_check() is True

    def test_false_when_no_tokens(self, file_backend):
        assert auth.health_check() is False

    def test_passive_never_refreshes(self, file_backend, monkeypatch):
        """The probe-path check must NOT rotate the single-use refresh token
        — a gunicorn timeout mid-rotation would be unrecoverable."""
        auth._write_blob(_blob(expires_in=10))  # stale: an active check would refresh

        def _boom(*a, **k):
            raise AssertionError("health_check must not hit the token endpoint")

        monkeypatch.setattr(auth, "_refresh_request", _boom)
        assert auth.health_check() is True  # blob well-formed = healthy


class TestRunHealthChecksCorosGate:
    def test_absent_when_unconfigured(self, monkeypatch, tmp_path):
        import health

        monkeypatch.delenv("COROS_TOKENS_BACKEND", raising=False)
        monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "nope.json")
        monkeypatch.setattr(health, "check_redis_health", lambda: True, raising=False)
        results = {}
        # Run only the COROS section semantics: call the real function but
        # stub the unrelated checks fast.
        import conversation_store

        monkeypatch.setattr(conversation_store, "check_redis_health", lambda: True)
        import config

        monkeypatch.setattr(config, "llm_client", None)
        monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
        monkeypatch.delenv("CALENDAR_ID", raising=False)
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        try:
            results = health.run_health_checks()
        except Exception:
            pass
        assert "coros" not in results

    def test_false_when_configured_but_dead(self, monkeypatch, tmp_path):
        import conversation_store
        import health

        monkeypatch.setenv("COROS_TOKENS_BACKEND", "file")
        monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "nope.json")
        monkeypatch.setattr(conversation_store, "check_redis_health", lambda: True)
        import config

        monkeypatch.setattr(config, "llm_client", None)
        monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
        monkeypatch.delenv("CALENDAR_ID", raising=False)
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        try:
            results = health.run_health_checks()
        except Exception:
            results = {}
        assert results.get("coros") is False
