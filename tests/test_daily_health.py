"""Tests for StateManager daily-health storage (schema v7) + coros.ingest.

Covers the upsert COALESCE contract (backfill re-pulls never erase the
recovery snapshot captured on the original night), window reads, the weekly
load-trend aggregation, readiness-block rendering, and v7 landing additively
on a pre-existing v6 database.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from state_manager import StateManager

TODAY = date(2026, 6, 11)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    return d


@pytest.fixture
def state(state_dir: Path) -> StateManager:
    return StateManager(state_dir)


def _row(d: str, **overrides) -> dict:
    base = {
        "date": d,
        "sleep_score": 80,
        "sleep_duration_min": 450,
        "hrv_avg": 85,
        "hrv_baseline": 82,
        "resting_hr": 50,
        "stress_avg": 30,
        "load_short_term": 130.0,
        "load_long_term": 105.0,
        "load_ratio": 1.24,
        "load_comment": "Optimized",
    }
    base.update(overrides)
    return base


# ---------- upsert ----------


class TestUpsertDailyHealth:
    def test_insert_then_read(self, state):
        state.upsert_daily_health([_row("2026-06-10"), _row("2026-06-11", recovery_pct=92)])
        rows = state.get_daily_health(days=7, today=TODAY)
        assert [r["date"] for r in rows] == ["2026-06-10", "2026-06-11"]
        assert rows[1]["recovery_pct"] == 92

    def test_idempotent_reupsert(self, state):
        state.upsert_daily_health([_row("2026-06-10")])
        state.upsert_daily_health([_row("2026-06-10")])
        rows = state.get_daily_health(days=7, today=TODAY)
        assert len(rows) == 1

    def test_null_field_does_not_clobber_existing(self, state):
        # Night 1: today's row carries the recovery snapshot + raw bundle.
        state.upsert_daily_health([_row("2026-06-10", recovery_pct=88, recovery_level="Good", raw='{"t": "night1"}')])
        # Night 2: backfill re-pull of the same date — no recovery, no raw.
        state.upsert_daily_health([_row("2026-06-10", sleep_score=81)])
        row = state.get_daily_health(days=7, today=TODAY)[0]
        assert row["recovery_pct"] == 88  # preserved
        assert row["recovery_level"] == "Good"
        assert json.loads(row["raw"]) == {"t": "night1"}
        assert row["sleep_score"] == 81  # new non-null value wins

    def test_empty_list_is_noop(self, state):
        state.upsert_daily_health([])
        assert state.get_daily_health(days=7, today=TODAY) == []

    def test_mirror_hook_fires(self, state, monkeypatch):
        captured = []
        monkeypatch.setattr(state, "_notify_mirror_health", lambda rows: captured.append(rows))
        state.upsert_daily_health([_row("2026-06-11")])
        assert len(captured) == 1


# ---------- reads ----------


class TestGetDailyHealth:
    def test_window_is_inclusive_last_n_days(self, state):
        state.upsert_daily_health([_row("2026-06-04"), _row("2026-06-05"), _row("2026-06-11")])
        rows = state.get_daily_health(days=7, today=TODAY)
        # 7-day window = [06-05, 06-11]; 06-04 falls outside.
        assert [r["date"] for r in rows] == ["2026-06-05", "2026-06-11"]

    def test_empty_db(self, state):
        assert state.get_daily_health(days=7, today=TODAY) == []


class TestGetLoadTrend:
    def test_weekly_aggregation(self, state):
        # Week of 06-01 (Mon) and week of 06-08.
        state.upsert_daily_health(
            [
                _row("2026-06-02", load_ratio=1.0, load_long_term=100.0),
                _row("2026-06-03", load_ratio=1.2, load_long_term=101.0),
                _row("2026-06-08", load_ratio=1.52, load_long_term=107.0, load_comment="Excessive"),
                _row("2026-06-09", load_ratio=1.42, load_long_term=107.0),
            ]
        )
        trend = state.get_load_trend(weeks=4, today=TODAY)
        assert len(trend) == 2
        wk1, wk2 = trend
        assert wk1["start"] == "2026-06-01"
        assert wk1["avg_load_ratio"] == 1.1
        assert wk1["last_long_term"] == 101.0
        assert wk1["flagged_days"] == 0
        assert wk2["avg_load_ratio"] == 1.47
        assert wk2["flagged_days"] == 1

    def test_empty_db(self, state):
        assert state.get_load_trend(weeks=4, today=TODAY) == []


# ---------- rendering ----------


class TestRenderReadinessBlock:
    def test_empty_db_renders_empty_string(self, state):
        assert state.render_readiness_block(days=7, today=TODAY) == ""

    def test_table_and_header(self, state):
        state.upsert_daily_health(
            [
                _row("2026-06-10", hrv_range_low=68, hrv_range_high=96),
                _row(
                    "2026-06-11",
                    recovery_pct=92,
                    recovery_level="Heavy training allowed",
                    sleep_nap_min=122,
                    hrv_evaluation="Above normal",
                    hrv_range_low=68,
                    hrv_range_high=96,
                ),
            ]
        )
        block = state.render_readiness_block(days=7, today=TODAY)
        assert "HRV baseline 82ms (normal 68–96ms)" in block
        assert "Recovery 92% — Heavy training allowed (as of 2026-06-11)" in block
        assert "| 2026-06-11 | 7h30 (score 80) +122m nap | 85ms (Above normal) | 50 | 30 | 1.24 | Optimized |" in block

    def test_missing_fields_render_dashes(self, state):
        state.upsert_daily_health([{"date": "2026-06-11", "steps": 100}])
        block = state.render_readiness_block(days=7, today=TODAY)
        assert "| 2026-06-11 | — | — | — | — | — | — |" in block

    def test_load_trend_block(self, state):
        state.upsert_daily_health([_row("2026-06-09", load_ratio=1.42)])
        block = state._render_load_trend_block(weeks=4, today=TODAY)
        assert "avg load ratio 1.42" in block
        assert "chronic load 105.0" in block


# ---------- load_full_context integration ----------


class TestFullContextIntegration:
    def _seed_minimum(self, state):
        """load_full_context needs athlete + plan rows to exist."""
        with state._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO athlete (id, yaml_text) VALUES (1, 'name: Test')")
            conn.commit()

    def test_blocks_absent_without_health_data(self, state):
        self._seed_minimum(state)
        blob = state.load_full_context()
        assert "=== READINESS" not in blob
        assert "=== TRAINING LOAD TREND" not in blob

    def test_blocks_present_with_health_data(self, state):
        from temporal_context import today_local

        self._seed_minimum(state)
        # Seed with today in USER_TIMEZONE: load_full_context's windows are
        # anchored to today_local(), not date.today() — across a midnight
        # boundary (e.g. UTC CI with a US timezone in the env) the system
        # clock's date can fall one day AFTER the window and be excluded.
        state.upsert_daily_health([_row(today_local().isoformat())])
        blob = state.load_full_context()
        assert "=== READINESS (COROS, last 7 days) ===" in blob
        assert "=== TRAINING LOAD TREND (last 4 weeks) ===" in blob
        assert "| Date | Sleep | HRV | RHR | Stress | Load ratio | Load status |" in blob
        # Journal stays last.
        assert blob.index("READINESS") < blob.index("JOURNAL")


# ---------- migration ----------


class TestV7Migration:
    def test_v7_lands_on_existing_v6_db(self, state_dir):
        """A DB created before v7 gains daily_health on next _ensure_schema."""
        import sqlite3

        import state_manager as sm

        # Build a v6-era DB: full current schema minus daily_health.
        db = state_dir / "coach.db"
        ddl = sm.SCHEMA_PATH.read_text()
        head = ddl.split("-- Daily wearable health metrics")[0]
        conn = sqlite3.connect(db)
        conn.executescript(head)  # current schema.sql minus daily_health = v6 shape
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (6)")
        conn.commit()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_health'").fetchone()
        conn.close()

        state = StateManager(state_dir)
        state.upsert_daily_health([_row("2026-06-11")])  # forces connect + migrate
        assert len(state.get_daily_health(days=7, today=TODAY)) == 1


# ---------- ingest ----------


class TestRunNightlyPull:
    @pytest.fixture
    def fixtures_bundle(self):
        fixtures = Path(__file__).parent / "fixtures" / "coros"
        return {
            name: (fixtures / f"{name}.txt").read_text()
            for name in (
                "queryDailyHealthData",
                "querySleepData",
                "queryHrvAssessment",
                "queryRestingHeartRate",
                "queryTrainingLoadAssessment",
                "queryRecoveryStatus",
            )
        }

    def test_pull_persists_rows(self, state, monkeypatch, fixtures_bundle):
        from coros import ingest

        monkeypatch.setattr(ingest.client, "fetch_daily_bundle", lambda days: fixtures_bundle)
        monkeypatch.setattr(ingest, "today_local", lambda: TODAY)
        result = ingest.run_nightly_pull(state)
        assert "2026-06-11" in result["dates"]
        assert result["fields_parsed"] > 50
        assert result["errors"] == []
        assert state.get_daily_health(days=7, today=TODAY)

    def test_dry_run_writes_nothing(self, state, monkeypatch, fixtures_bundle):
        from coros import ingest

        monkeypatch.setattr(ingest.client, "fetch_daily_bundle", lambda days: fixtures_bundle)
        monkeypatch.setattr(ingest, "today_local", lambda: TODAY)
        result = ingest.run_nightly_pull(state, dry_run=True)
        assert result["dates"]
        assert state.get_daily_health(days=7, today=TODAY) == []

    def test_total_format_change_flags_error_and_keeps_raw(self, state, monkeypatch):
        """The worst case — NOTHING parses — must surface an error (so the
        scheduler fails the pass and retries) and still persist the raw
        bundle on today's row for recovery."""
        from coros import ingest

        garbled = {t: '"Totally New Format"' for t in ingest.client.BUNDLE_TOOLS}
        monkeypatch.setattr(ingest.client, "fetch_daily_bundle", lambda days: garbled)
        monkeypatch.setattr(ingest, "today_local", lambda: TODAY)
        result = ingest.run_nightly_pull(state)
        assert result["ok"] is False
        assert any("0 fields parsed" in e for e in result["errors"])
        assert result["dates"] == [TODAY.isoformat()]  # raw-only row
        row = state.get_daily_health(days=1, today=TODAY)[0]
        assert json.loads(row["raw"]) == garbled

    def test_partial_tool_failure_reported(self, state, monkeypatch, fixtures_bundle):
        from coros import ingest

        partial = {k: v for k, v in fixtures_bundle.items() if k != "querySleepData"}
        monkeypatch.setattr(ingest.client, "fetch_daily_bundle", lambda days: partial)
        monkeypatch.setattr(ingest, "today_local", lambda: TODAY)
        result = ingest.run_nightly_pull(state)
        assert any("querySleepData" in e for e in result["errors"])
        assert result["dates"]  # other tools still landed


class TestBackfillDays:
    def test_clamps_to_at_least_one_day(self, monkeypatch):
        from coros import ingest

        monkeypatch.setenv("COROS_BACKFILL_DAYS", "-3")
        assert ingest._backfill_days() == 1
        monkeypatch.setenv("COROS_BACKFILL_DAYS", "0")
        assert ingest._backfill_days() == 1

    def test_junk_falls_back_to_default(self, monkeypatch):
        from coros import ingest

        monkeypatch.setenv("COROS_BACKFILL_DAYS", "junk")
        assert ingest._backfill_days() == ingest.DEFAULT_BACKFILL_DAYS


class TestMetricFreshnessHelpers:
    def test_latest_metric_date_ignores_raw_only_rows(self, state):
        """Ingest's raw-insurance row (today's row carrying only the unparsed
        bundle) must not count as fresh data — otherwise the staleness alert
        and /health never notice a COROS format change."""
        state.upsert_daily_health([_row("2026-06-09")])
        state.upsert_daily_health([{"date": "2026-06-11", "raw": '{"t": "unparsed"}'}])
        assert state.latest_metric_date() == "2026-06-09"

    def test_latest_metric_date_none_without_metrics(self, state):
        assert state.latest_metric_date() is None
        state.upsert_daily_health([{"date": "2026-06-11", "raw": "{}"}])
        assert state.latest_metric_date() is None

    def test_has_daily_health_rows(self, state):
        assert state.has_daily_health_rows() is False
        state.upsert_daily_health([{"date": "2026-06-11", "raw": "{}"}])
        assert state.has_daily_health_rows() is True


class TestReadinessUniqueIndex:
    def test_one_readiness_review_per_date(self, state):
        """The 'once per night, DB-enforced' comment in coros/review.py is
        only true because of this partial unique index."""
        import sqlite3

        state.save_review(None, None, TODAY, "first", kind="readiness")
        with pytest.raises(sqlite3.IntegrityError):
            state.save_review(None, None, TODAY, "second", kind="readiness")

    def test_activity_reviews_not_constrained(self, state):
        state.save_review(None, None, TODAY, "a")
        state.save_review(None, None, TODAY, "b")  # duplicates fine for activity
        state.save_review(None, None, TODAY, "r", kind="readiness")  # kinds coexist
