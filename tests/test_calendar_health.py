"""Tests for calendar_health — the auth watchdog + sync sweep + alert path.

The risk areas: (1) correctly distinguishing a dead refresh token from an
unreachable token store (so we never tell the user to re-auth a working grant),
(2) the alert dedup cooldown, and (3) the run() control flow / exit codes.
"""

from __future__ import annotations

import calendar_health as ch
from google_calendar import auth

# ---------- classify_auth ----------


class TestClassifyAuth:
    def test_storage_unavailable_is_infra(self, monkeypatch):
        def boom():
            raise auth.TokenStorageUnavailable("Redis unreachable")

        monkeypatch.setattr(auth, "_read_tokens", boom)
        status, detail = ch.classify_auth()
        assert status == "infra"
        assert "Redis" in detail

    def test_no_tokens_needs_auth(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_tokens", lambda: None)
        status, _ = ch.classify_auth()
        assert status == "needs_auth"

    def test_missing_refresh_token_needs_auth(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_tokens", lambda: {"access_token": "x"})
        status, _ = ch.classify_auth()
        assert status == "needs_auth"

    def test_refresh_4xx_needs_auth(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_tokens", lambda: {"refresh_token": "rt"})

        def boom():
            raise auth.GcalAuthError("token refresh failed: 400 invalid_grant")

        monkeypatch.setattr(auth, "get_access_token", boom)
        status, detail = ch.classify_auth()
        assert status == "needs_auth"
        assert "invalid_grant" in detail

    def test_storage_outage_during_refresh_is_infra(self, monkeypatch):
        """get_access_token() re-wraps a storage outage as GcalAuthError — it
        must still be classified as infra, not a re-auth prompt."""
        monkeypatch.setattr(auth, "_read_tokens", lambda: {"refresh_token": "rt"})

        def boom():
            raise auth.GcalAuthError("Gcal token storage unavailable (redis backend): down")

        monkeypatch.setattr(auth, "get_access_token", boom)
        status, _ = ch.classify_auth()
        assert status == "infra"

    def test_happy_path_ok(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_tokens", lambda: {"refresh_token": "rt"})
        monkeypatch.setattr(auth, "get_access_token", lambda: "fresh-token")
        status, _ = ch.classify_auth()
        assert status == "ok"


# ---------- alert dedup ----------


class TestShouldAlert:
    def _fake_redis(self, monkeypatch, store):
        class _R:
            def get(self, k):
                return store.get(k)

            def set(self, k, v):
                store[k] = v

            def delete(self, k):
                store.pop(k, None)

        monkeypatch.setattr("conversation_store._get_redis", lambda: _R())
        return store

    def test_alerts_when_no_prior(self, monkeypatch):
        self._fake_redis(monkeypatch, {})
        assert ch._should_alert(now=1000.0) is True

    def test_suppressed_within_cooldown(self, monkeypatch):
        store = self._fake_redis(monkeypatch, {ch._ALERT_REDIS_KEY: "1000"})
        # 1 hour later, default 12h cooldown -> suppressed
        assert ch._should_alert(now=1000.0 + 3600) is False
        assert store  # untouched

    def test_alerts_after_cooldown(self, monkeypatch):
        self._fake_redis(monkeypatch, {ch._ALERT_REDIS_KEY: "1000"})
        later = 1000.0 + ch.ALERT_COOLDOWN_HOURS * 3600 + 1
        assert ch._should_alert(now=later) is True

    def test_fails_open_when_redis_down(self, monkeypatch):
        def boom():
            raise RuntimeError("redis down")

        monkeypatch.setattr("conversation_store._get_redis", boom)
        # Can't dedup -> alert anyway rather than stay silent.
        assert ch._should_alert(now=1000.0) is True


# ---------- run() control flow ----------


class TestRun:
    def test_infra_returns_exit_infra_no_alert(self, monkeypatch):
        monkeypatch.setattr(ch, "classify_auth", lambda: ("infra", "redis down"))
        sent = []
        monkeypatch.setattr(ch, "_send_alert", lambda now: sent.append(now))
        assert ch.run(now=1000.0) == ch.EXIT_INFRA
        assert sent == []

    def test_needs_auth_alerts_and_returns_code(self, monkeypatch):
        monkeypatch.setattr(ch, "classify_auth", lambda: ("needs_auth", "invalid_grant"))
        sent = []
        monkeypatch.setattr(ch, "_send_alert", lambda now: sent.append(now))
        assert ch.run(now=1000.0) == ch.EXIT_NEEDS_AUTH
        assert sent == [1000.0]

    def test_ok_runs_sync_and_clears_alert(self, monkeypatch):
        monkeypatch.setattr(ch, "classify_auth", lambda: ("ok", ""))
        monkeypatch.setattr(
            ch,
            "_run_sync",
            lambda dry_run: {"inserted": 1, "patched": 2, "deleted": 0, "unchanged": 9, "errors": []},
        )
        cleared = []
        monkeypatch.setattr(ch, "_clear_alert_state", lambda: cleared.append(True))
        assert ch.run(now=1000.0) == ch.EXIT_OK
        assert cleared == [True]

    def test_check_only_skips_sync(self, monkeypatch):
        monkeypatch.setattr(ch, "classify_auth", lambda: ("ok", ""))

        def fail(dry_run):
            raise AssertionError("sync must not run with do_sync=False")

        monkeypatch.setattr(ch, "_run_sync", fail)
        monkeypatch.setattr(ch, "_clear_alert_state", lambda: None)
        assert ch.run(do_sync=False, now=1000.0) == ch.EXIT_OK

    def test_auth_dies_mid_sync_alerts(self, monkeypatch):
        monkeypatch.setattr(ch, "classify_auth", lambda: ("ok", ""))

        def boom(dry_run):
            raise auth.GcalAuthError("token refresh failed: 400")

        monkeypatch.setattr(ch, "_run_sync", boom)
        sent = []
        monkeypatch.setattr(ch, "_send_alert", lambda now: sent.append(now))
        assert ch.run(now=1000.0) == ch.EXIT_NEEDS_AUTH
        assert sent == [1000.0]

    def test_sync_crash_is_infra_not_auth(self, monkeypatch):
        monkeypatch.setattr(ch, "classify_auth", lambda: ("ok", ""))

        def boom(dry_run):
            raise RuntimeError("calendar API 500")

        monkeypatch.setattr(ch, "_run_sync", boom)
        sent = []
        monkeypatch.setattr(ch, "_send_alert", lambda now: sent.append(now))
        assert ch.run(now=1000.0) == ch.EXIT_INFRA
        assert sent == []


# ---------- scheduler gating ----------


PROD_ENV = {"RAILWAY_ENVIRONMENT": "production", "CALENDAR_ID": "cal", "TELEGRAM_BOT_TOKEN": "tok"}


class TestSchedulerEnabled:
    def test_on_when_all_signals_present(self):
        assert ch.scheduler_enabled(dict(PROD_ENV)) is True

    def test_on_with_alternate_railway_signal(self):
        # Any Railway-injected runtime var counts as the prod discriminator.
        env = {"RAILWAY_SERVICE_NAME": "web", "CALENDAR_ID": "cal", "TELEGRAM_BOT_TOKEN": "tok"}
        assert ch.scheduler_enabled(env) is True

    def test_off_under_testing(self):
        assert ch.scheduler_enabled({**PROD_ENV, "TESTING": "1"}) is False

    def test_off_when_disable_flag_set(self):
        assert ch.scheduler_enabled({**PROD_ENV, ch._DISABLE_FLAG: "true"}) is False

    def test_off_when_not_on_railway(self):
        # Local dev with a prod-shaped .env but no RAILWAY_* signal must stay off.
        env = {"CALENDAR_ID": "cal", "TELEGRAM_BOT_TOKEN": "tok", "WEBHOOK_URL": "https://x"}
        assert ch.scheduler_enabled(env) is False

    def test_off_when_calendar_id_absent(self):
        env = {k: v for k, v in PROD_ENV.items() if k != "CALENDAR_ID"}
        assert ch.scheduler_enabled(env) is False

    def test_off_when_token_absent(self):
        env = {k: v for k, v in PROD_ENV.items() if k != "TELEGRAM_BOT_TOKEN"}
        assert ch.scheduler_enabled(env) is False


# ---------- scheduler loop ----------


class TestSchedulerLoop:
    def test_single_iteration_calls_run_with_sync(self, monkeypatch):
        monkeypatch.setattr(ch.time, "sleep", lambda _s: None)  # skip the 60s + interval waits
        calls = []
        monkeypatch.setattr(ch, "run", lambda do_sync=True: calls.append(do_sync) or ch.EXIT_OK)
        ch._scheduler_loop(1.0, _max_iterations=1)
        assert calls == [True]

    def test_iteration_swallows_exceptions(self, monkeypatch):
        monkeypatch.setattr(ch.time, "sleep", lambda _s: None)

        def boom(do_sync=True):
            raise RuntimeError("pass failed")

        monkeypatch.setattr(ch, "run", boom)
        # Two iterations, both raising — the loop must NOT propagate (daemon survives).
        ch._scheduler_loop(1.0, _max_iterations=2)

    def test_start_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(ch, "scheduler_enabled", lambda env: False)
        assert ch.start_scheduler_if_enabled(env={}) is None


# ---------- _run_once_safely ----------


class TestRunOnceSafely:
    def test_returns_exit_code_on_success(self, monkeypatch):
        monkeypatch.setattr(ch, "run", lambda do_sync=True: ch.EXIT_OK)
        assert ch._run_once_safely() == ch.EXIT_OK

    def test_returns_none_and_swallows_on_error(self, monkeypatch):
        def boom(do_sync=True):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(ch, "run", boom)
        assert ch._run_once_safely() is None
