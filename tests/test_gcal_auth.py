"""Tests for google_calendar.auth — primarily the Redis vs file backend split.

Mirror of tests/test_strava_auth.py with method/key renames for Gcal. The
HTTP paths (exchange_code_for_tokens, _refresh) are exercised manually
during the smoke test rather than mocked here; they're thin wrappers over
requests.post.
"""

from __future__ import annotations

import json
import time

import pytest

from google_calendar import auth


@pytest.fixture
def file_backend(monkeypatch, tmp_path):
    """Force file backend with a temp token path."""
    monkeypatch.setenv("GCAL_TOKENS_BACKEND", "file")
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    yield tmp_path / "tokens.json"


@pytest.fixture
def redis_backend(monkeypatch, fake_redis):
    """Force redis backend; fake_redis fixture (from conftest) wires the client."""
    monkeypatch.setenv("GCAL_TOKENS_BACKEND", "redis")
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
        token_path = tmp_path / "tokens.json"
        monkeypatch.setattr(auth, "TOKEN_FILE", token_path)
        auth._write_tokens({"refresh_token": "x", "access_token": "y", "expires_at": 1})
        assert not token_path.exists()


# ---------- get_access_token ----------


class TestGetAccessToken:
    def test_raises_when_no_tokens(self, file_backend):
        with pytest.raises(auth.GcalAuthError):
            auth.get_access_token()

    def test_returns_cached_when_not_expired(self, file_backend):
        future = int(time.time()) + 3600
        auth._write_tokens({"refresh_token": "r", "access_token": "cached", "expires_at": future})
        assert auth.get_access_token() == "cached"

    def test_redis_backend_error_includes_redis_in_message(self, redis_backend):
        with pytest.raises(auth.GcalAuthError) as exc:
            auth.get_access_token()
        assert "Redis" in str(exc.value) or "redis" in str(exc.value)


class TestStorageUnreachableSurfaces:
    """When Redis itself is unreachable, the error message must NOT tell the
    user to re-auth — that's misleading. It should say infrastructure issue.
    """

    def test_redis_unreachable_raises_token_storage_unavailable(self, monkeypatch):
        monkeypatch.setenv("GCAL_TOKENS_BACKEND", "redis")

        import conversation_store

        def _boom():
            raise ConnectionError("connection refused")

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        with pytest.raises(auth.TokenStorageUnavailable):
            auth._read_tokens_redis()

    def test_get_access_token_translates_unavailable_to_infra_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GCAL_TOKENS_BACKEND", "redis")
        import conversation_store

        def _boom():
            raise ConnectionError("connection refused")

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        with pytest.raises(auth.GcalAuthError) as exc:
            auth.get_access_token()
        msg = str(exc.value)
        assert "scripts/google_calendar_setup.py auth" not in msg
        assert "infrastructure" in msg.lower() or "unreachable" in msg.lower() or "unavailable" in msg.lower()


class TestRefreshRetry:
    """The _refresh function should retry on transient network errors but
    propagate on Google 4xx (e.g., revoked refresh token)."""

    def test_refresh_retries_on_connection_error(self, monkeypatch):
        import requests as _requests

        from google_calendar import auth as _auth

        attempts = {"n": 0}

        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "access_token": "new_access",
                    "expires_in": 3600,
                }

        def _post(url, data, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _requests.ConnectionError("flaky")
            return _Resp()

        monkeypatch.setenv("GCAL_CLIENT_ID", "x")
        monkeypatch.setenv("GCAL_CLIENT_SECRET", "y")
        monkeypatch.setattr(_requests, "post", _post)
        result = _auth._refresh.retry_with(stop=__import__("tenacity").stop_after_attempt(3))("rt")
        assert result["access_token"] == "new_access"
        # Refresh token doesn't rotate on Google — falls back to input.
        assert result["refresh_token"] == "rt"
        assert attempts["n"] == 3

    def test_refresh_4xx_does_not_retry(self, monkeypatch):
        import requests as _requests

        from google_calendar import auth as _auth

        attempts = {"n": 0}

        class _Resp:
            status_code = 400
            text = "invalid refresh"

            def json(self):
                return {}

        def _post(*a, **kw):
            attempts["n"] += 1
            return _Resp()

        monkeypatch.setenv("GCAL_CLIENT_ID", "x")
        monkeypatch.setenv("GCAL_CLIENT_SECRET", "y")
        monkeypatch.setattr(_requests, "post", _post)
        with pytest.raises(auth.GcalAuthError):
            _auth._refresh("rt")
        assert attempts["n"] == 1


class TestSetupScriptOobFlow:
    """Cover the new --no-listener / --code flow in scripts/google_calendar_setup.py.

    The bug fixed by issue #13: the default flow binds a loopback HTTP listener
    that only works on a developer laptop. Inside `railway shell` the redirect
    lands on the laptop and the script hangs. The OOB flow uses
    `urn:ietf:wg:oauth:2.0:oob` and asks the user to paste the code.
    """

    def _import_script(self):
        import importlib.util
        from pathlib import Path

        # The script lives in scripts/ which is not on sys.path by default;
        # load it directly so we can test its helpers without invoking main().
        script = Path(__file__).resolve().parent.parent / "scripts" / "google_calendar_setup.py"
        spec = importlib.util.spec_from_file_location("_gcal_setup_under_test", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_build_auth_url_uses_oob_redirect(self):
        mod = self._import_script()
        url = mod._build_auth_url("cid123", mod.OOB_REDIRECT_URI, "state-xyz")
        assert "redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob" in url
        assert "client_id=cid123" in url
        assert "state=state-xyz" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_build_auth_url_uses_loopback_when_requested(self):
        mod = self._import_script()
        url = mod._build_auth_url("cid", mod.REDIRECT_URI, "st")
        # Loopback redirect_uri is URL-encoded but should still embed 127.0.0.1.
        assert "127.0.0.1" in url

    def test_code_flag_skips_listener_and_uses_oob_redirect(self, monkeypatch):
        """`--code <code>` must call exchange with OOB redirect_uri and never
        touch the HTTP listener."""
        mod = self._import_script()
        monkeypatch.setenv("GCAL_CLIENT_ID", "cid")
        monkeypatch.setenv("GCAL_CLIENT_SECRET", "secret")

        called = {}

        def _fake_exchange(code, redirect_uri):
            called["code"] = code
            called["redirect_uri"] = redirect_uri
            return {"refresh_token": "r", "access_token": "a", "expires_at": 1}

        monkeypatch.setattr(mod.auth, "exchange_code_for_tokens", _fake_exchange)
        # Sanity guard: if the listener path were taken, this would raise
        # because HTTPServer is not monkeypatched. Confirms we short-circuit.
        monkeypatch.setattr(
            mod.http.server,
            "HTTPServer",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("listener should not be used")),
        )

        import argparse as _argparse

        args = _argparse.Namespace(code="abc123", no_listener=False)
        rc = mod.cmd_auth(args)
        assert rc == 0
        assert called == {"code": "abc123", "redirect_uri": mod.OOB_REDIRECT_URI}

    def test_no_listener_flag_prompts_for_code(self, monkeypatch, capsys):
        mod = self._import_script()
        monkeypatch.setenv("GCAL_CLIENT_ID", "cid")
        monkeypatch.setenv("GCAL_CLIENT_SECRET", "secret")

        called = {}

        def _fake_exchange(code, redirect_uri):
            called["code"] = code
            called["redirect_uri"] = redirect_uri
            return {"refresh_token": "r", "access_token": "a", "expires_at": 1}

        monkeypatch.setattr(mod.auth, "exchange_code_for_tokens", _fake_exchange)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "pasted-code")

        import argparse as _argparse

        args = _argparse.Namespace(code=None, no_listener=True)
        rc = mod.cmd_auth(args)
        assert rc == 0
        assert called == {"code": "pasted-code", "redirect_uri": mod.OOB_REDIRECT_URI}
        # The printed URL must carry the OOB redirect_uri (URL-encoded).
        out = capsys.readouterr().out
        assert "urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob" in out

    def test_oob_rejection_emits_deprecation_hint(self, monkeypatch, capsys):
        """When Google's token endpoint returns 400 + invalid_request on an OOB
        exchange (the deprecation signature), the script must surface a clear
        hint pointing at the listener flow as the fallback before re-raising.
        """
        mod = self._import_script()
        monkeypatch.setenv("GCAL_CLIENT_ID", "cid")
        monkeypatch.setenv("GCAL_CLIENT_SECRET", "secret")

        # Mock the HTTP layer: requests.post returns a 400 with the
        # deprecation-signature error body Google emits for OOB.
        import requests as _requests

        class _Resp:
            status_code = 400
            text = '{"error": "invalid_request", "error_description": "OOB is deprecated"}'

            def json(self):
                return {"error": "invalid_request", "error_description": "OOB is deprecated"}

        monkeypatch.setattr(_requests, "post", lambda *a, **kw: _Resp())

        rc = mod._exchange_and_report("any-code", mod.OOB_REDIRECT_URI)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Google rejected the OOB redirect_uri" in err
        assert "deprecated" in err
        assert "127.0.0.1" in err
