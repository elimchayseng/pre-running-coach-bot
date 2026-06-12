"""Tests for coros.scheduler — mirror of tests/test_calendar_health.py with
the COROS-specific addition: the nightly due-check (_is_due + last-run
marker) layered on the interval loop."""

from __future__ import annotations

from datetime import datetime

from coros import auth, scheduler


def _blob():
    return {
        "client_info": {"client_id": "cid"},
        "tokens": {"access_token": "at", "refresh_token": "rt", "expires_at": 9999999999},
    }


# ---------- classify_auth ----------


class TestClassifyAuth:
    def test_storage_unavailable_is_infra(self, monkeypatch):
        def _boom():
            raise auth.TokenStorageUnavailable("redis down")

        monkeypatch.setattr(auth, "_read_blob", _boom)
        status, detail = scheduler.classify_auth()
        assert status == "infra"
        assert "redis down" in detail

    def test_no_tokens_needs_auth(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_blob", lambda: None)
        assert scheduler.classify_auth()[0] == "needs_auth"

    def test_missing_refresh_token_needs_auth(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_blob", lambda: {"client_info": {}, "tokens": {}})
        assert scheduler.classify_auth()[0] == "needs_auth"

    def test_dead_refresh_token_needs_auth(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_blob", lambda: _blob())

        def _fail():
            raise auth.CorosAuthError("token refresh failed: 400 invalid_grant")

        monkeypatch.setattr(auth, "get_access_token", _fail)
        assert scheduler.classify_auth()[0] == "needs_auth"

    def test_storage_outage_during_refresh_is_infra(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_blob", lambda: _blob())

        def _fail():
            raise auth.CorosAuthError("COROS token storage unavailable (redis backend)")

        monkeypatch.setattr(auth, "get_access_token", _fail)
        assert scheduler.classify_auth()[0] == "infra"

    def test_happy_path_ok(self, monkeypatch):
        monkeypatch.setattr(auth, "_read_blob", lambda: _blob())
        monkeypatch.setattr(auth, "get_access_token", lambda: "at")
        assert scheduler.classify_auth() == ("ok", "")


# ---------- due-check ----------


class TestIsDue:
    def test_not_due_before_pull_hour(self):
        now = datetime(2026, 6, 11, 21, 59)
        assert scheduler._is_due(now, None, pull_hour=22) is False

    def test_due_at_pull_hour_when_not_run_today(self):
        now = datetime(2026, 6, 11, 22, 0)
        assert scheduler._is_due(now, "2026-06-10", pull_hour=22) is True

    def test_not_due_when_already_ran_today(self):
        now = datetime(2026, 6, 11, 23, 30)
        assert scheduler._is_due(now, "2026-06-11", pull_hour=22) is False

    def test_missing_marker_fails_open(self):
        now = datetime(2026, 6, 11, 22, 30)
        assert scheduler._is_due(now, None, pull_hour=22) is True

    def test_pull_hour_env_clamped(self, monkeypatch):
        assert scheduler._pull_hour({"COROS_PULL_HOUR_LOCAL": "99"}) == 23
        assert scheduler._pull_hour({"COROS_PULL_HOUR_LOCAL": "-1"}) == 0
        assert scheduler._pull_hour({"COROS_PULL_HOUR_LOCAL": "junk"}) == 22
        assert scheduler._pull_hour({}) == 22

    def test_marker_roundtrip_via_redis(self, fake_redis):
        scheduler._record_last_run_date("2026-06-11")
        assert scheduler._read_last_run_date() == "2026-06-11"

    def test_marker_read_fails_open(self, monkeypatch):
        import conversation_store

        def _boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr(conversation_store, "_get_redis", _boom)
        assert scheduler._read_last_run_date() is None


# ---------- run() ----------


class TestRun:
    def test_infra_returns_exit_infra_no_alert(self, monkeypatch):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("infra", "redis down"))
        sent = []
        monkeypatch.setattr(scheduler, "_send_alert", lambda now: sent.append(1))
        assert scheduler.run() == scheduler.EXIT_INFRA
        assert sent == []

    def test_needs_auth_alerts_and_returns_code(self, monkeypatch):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("needs_auth", "dead"))
        sent = []
        monkeypatch.setattr(scheduler, "_send_alert", lambda now: sent.append(1) or True)
        assert scheduler.run() == scheduler.EXIT_NEEDS_AUTH
        assert sent == [1]

    def test_ok_pulls_and_clears_alert(self, monkeypatch):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        cleared = []
        monkeypatch.setattr(scheduler, "_clear_alert_state", lambda: cleared.append(1))

        from coros import ingest

        monkeypatch.setattr(
            ingest, "run_nightly_pull", lambda state, dry_run=False: {"dates": ["d"], "fields_parsed": 5, "errors": []}
        )
        assert scheduler.run() == scheduler.EXIT_OK
        assert cleared == [1]

    def test_check_only_skips_pull(self, monkeypatch, fake_redis):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))

        from coros import ingest

        def _boom(*a, **k):
            raise AssertionError("pull should not run with do_pull=False")

        monkeypatch.setattr(ingest, "run_nightly_pull", _boom)
        assert scheduler.run(do_pull=False) == scheduler.EXIT_OK

    def test_auth_dies_mid_pull_alerts(self, monkeypatch):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        sent = []
        monkeypatch.setattr(scheduler, "_send_alert", lambda now: sent.append(1) or True)

        from coros import ingest

        def _die(state, dry_run=False):
            raise auth.CorosAuthError("revoked")

        monkeypatch.setattr(ingest, "run_nightly_pull", _die)
        assert scheduler.run() == scheduler.EXIT_NEEDS_AUTH
        assert sent == [1]

    def test_successful_pull_triggers_readiness_checkin(self, monkeypatch, fake_redis):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        from coros import ingest

        monkeypatch.setattr(
            ingest,
            "run_nightly_pull",
            lambda state, dry_run=False: {"dates": ["d"], "fields_parsed": 5, "errors": [], "ok": True},
        )
        called = []
        monkeypatch.setattr(scheduler, "_run_readiness_checkin", lambda state: called.append(1))
        assert scheduler.run() == scheduler.EXIT_OK
        assert called == [1]

    def test_dry_run_skips_readiness_checkin(self, monkeypatch, fake_redis):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        from coros import ingest

        monkeypatch.setattr(
            ingest,
            "run_nightly_pull",
            lambda state, dry_run=False: {"dates": ["d"], "fields_parsed": 5, "errors": [], "ok": True},
        )
        called = []
        monkeypatch.setattr(scheduler, "_run_readiness_checkin", lambda state: called.append(1))
        assert scheduler.run(dry_run=True) == scheduler.EXIT_OK
        assert called == []

    def test_readiness_checkin_failure_does_not_fail_pass(self, monkeypatch, fake_redis):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        from coros import ingest

        monkeypatch.setattr(
            ingest,
            "run_nightly_pull",
            lambda state, dry_run=False: {"dates": ["d"], "fields_parsed": 5, "errors": [], "ok": True},
        )

        def _explode(state):
            raise RuntimeError("llm down")

        from coros import review as coros_review

        monkeypatch.setattr(coros_review, "run_readiness_review", _explode)
        assert scheduler.run() == scheduler.EXIT_OK

    def test_zero_data_pull_is_infra_and_skips_checkin(self, monkeypatch, fake_redis):
        """A pull that parses nothing usable must FAIL the pass (no success
        marker -> retries tonight) instead of silently returning OK."""
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        from coros import ingest

        monkeypatch.setattr(
            ingest,
            "run_nightly_pull",
            lambda state, dry_run=False: {
                "dates": ["2026-06-11"],  # raw-only row exists
                "fields_parsed": 0,
                "errors": ["0 fields parsed from a non-empty bundle"],
                "ok": False,
            },
        )
        called = []
        monkeypatch.setattr(scheduler, "_run_readiness_checkin", lambda state: called.append(1))
        monkeypatch.setattr(scheduler, "_maybe_send_staleness_alert", lambda state, now: None)
        assert scheduler.run() == scheduler.EXIT_INFRA
        assert called == []

    def test_staleness_alert_only_when_data_stale(self, monkeypatch, fake_redis):
        sent = []
        import strava.notify as notify

        monkeypatch.setattr(notify, "send_telegram_text", lambda text, mirror=True: sent.append(text) or True)

        class _FreshState:
            def get_daily_health(self, days):
                return [{"date": "2026-06-11"}]

        class _StaleState:
            def get_daily_health(self, days):
                return []

        scheduler._maybe_send_staleness_alert(_FreshState(), now=1000.0)
        assert sent == []  # fresh data -> no alert
        scheduler._maybe_send_staleness_alert(_StaleState(), now=1000.0)
        assert len(sent) == 1 and "COROS pull has been failing" in sent[0]

    def test_pull_crash_is_infra_not_auth(self, monkeypatch):
        monkeypatch.setattr(scheduler, "classify_auth", lambda: ("ok", ""))
        sent = []
        monkeypatch.setattr(scheduler, "_send_alert", lambda now: sent.append(1))

        from coros import ingest

        def _die(state, dry_run=False):
            raise RuntimeError("mcp exploded")

        monkeypatch.setattr(ingest, "run_nightly_pull", _die)
        assert scheduler.run() == scheduler.EXIT_INFRA
        assert sent == []


# ---------- gating ----------


def _env(**over):
    base = {"RAILWAY_ENVIRONMENT": "production", "TELEGRAM_BOT_TOKEN": "t"}
    base.update(over)
    return base


class TestSchedulerEnabled:
    def test_on_when_all_signals_present(self):
        assert scheduler.scheduler_enabled(_env()) is True

    def test_on_with_alternate_railway_signal(self):
        env = _env()
        del env["RAILWAY_ENVIRONMENT"]
        env["RAILWAY_PROJECT_ID"] = "p"
        assert scheduler.scheduler_enabled(env) is True

    def test_off_under_testing(self):
        assert scheduler.scheduler_enabled(_env(TESTING="1")) is False

    def test_off_when_disable_flag_set(self):
        assert scheduler.scheduler_enabled(_env(DISABLE_COROS_SCHEDULER="1")) is False

    def test_off_when_not_on_railway(self):
        env = _env()
        del env["RAILWAY_ENVIRONMENT"]
        assert scheduler.scheduler_enabled(env) is False

    def test_off_when_token_absent(self):
        env = _env()
        del env["TELEGRAM_BOT_TOKEN"]
        assert scheduler.scheduler_enabled(env) is False

    def test_does_not_require_calendar_id(self):
        # COROS is independent of the calendar integration.
        assert scheduler.scheduler_enabled(_env()) is True


# ---------- tick + loop ----------


class TestTickOnceSafely:
    def test_not_due_returns_none_without_running(self, monkeypatch):
        import temporal_context

        monkeypatch.setattr(temporal_context, "now_local", lambda: datetime(2026, 6, 11, 8, 0))

        def _boom(**k):
            raise AssertionError("run() must not fire before pull hour")

        monkeypatch.setattr(scheduler, "run", _boom)
        assert scheduler._tick_once_safely() is None

    def test_due_runs_and_records_marker(self, monkeypatch):
        import temporal_context

        monkeypatch.setattr(temporal_context, "now_local", lambda: datetime(2026, 6, 11, 22, 30))
        monkeypatch.setattr(scheduler, "_read_last_run_date", lambda: "2026-06-10")
        monkeypatch.setattr(scheduler, "run", lambda do_pull: scheduler.EXIT_OK)
        recorded = []
        monkeypatch.setattr(scheduler, "_record_last_run_date", lambda d: recorded.append(d))
        assert scheduler._tick_once_safely() == scheduler.EXIT_OK
        assert recorded == ["2026-06-11"]

    def test_failed_run_does_not_record_marker(self, monkeypatch):
        import temporal_context

        monkeypatch.setattr(temporal_context, "now_local", lambda: datetime(2026, 6, 11, 22, 30))
        monkeypatch.setattr(scheduler, "_read_last_run_date", lambda: None)
        monkeypatch.setattr(scheduler, "run", lambda do_pull: scheduler.EXIT_INFRA)
        recorded = []
        monkeypatch.setattr(scheduler, "_record_last_run_date", lambda d: recorded.append(d))
        assert scheduler._tick_once_safely() == scheduler.EXIT_INFRA
        assert recorded == []  # next tick retries tonight

    def test_swallows_exceptions(self, monkeypatch):
        import temporal_context

        def _boom():
            raise RuntimeError("tz exploded")

        monkeypatch.setattr(temporal_context, "now_local", _boom)
        assert scheduler._tick_once_safely() is None


class TestSchedulerLoop:
    def test_single_iteration_ticks_once(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_SCHEDULER_INITIAL_DELAY_SECONDS", 0.0)
        ticks = []
        monkeypatch.setattr(scheduler, "_tick_once_safely", lambda: ticks.append(1))
        scheduler._scheduler_loop(0.0, _max_iterations=1)
        assert ticks == [1]

    def test_start_disabled_returns_none(self, monkeypatch):
        assert scheduler.start_scheduler_if_enabled({"TESTING": "1"}) is None
