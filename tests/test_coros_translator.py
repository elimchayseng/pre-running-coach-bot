"""Tests for coros.translator against captured real COROS MCP outputs.

Fixtures in tests/fixtures/coros/ were captured live by scripts/coros_spike.py
on 2026-06-11 — they are the JSON-string-wrapped text the server actually
returns, escapes and all.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from coros import translator

FIXTURES = Path(__file__).parent / "fixtures" / "coros"


def _fx(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


def _bundle() -> dict[str, str]:
    return {
        "queryDailyHealthData": _fx("queryDailyHealthData"),
        "querySleepData": _fx("querySleepData"),
        "queryHrvAssessment": _fx("queryHrvAssessment"),
        "queryRestingHeartRate": _fx("queryRestingHeartRate"),
        "queryTrainingLoadAssessment": _fx("queryTrainingLoadAssessment"),
        "queryRecoveryStatus": _fx("queryRecoveryStatus"),
    }


# ---------- unwrap + primitives ----------


class TestPrimitives:
    def test_unwrap_json_string_envelope(self):
        assert translator._unwrap('"a\\nb"') == "a\nb"

    def test_unwrap_plain_text_passthrough(self):
        assert translator._unwrap("plain — text") == "plain — text"

    def test_unwrap_malformed_quote_passthrough(self):
        assert translator._unwrap('"unterminated') == '"unterminated'

    def test_duration_variants(self):
        assert translator._duration_min("7h 32min") == 452
        assert translator._duration_min("49 min") == 49
        assert translator._duration_min("3h 34min") == 214
        assert translator._duration_min("0 min") == 0
        assert translator._duration_min("garbage") is None

    def test_int_with_thousands_separator(self):
        assert translator._to_int("12,377") == 12377


# ---------- per-tool parsers (fixture-driven) ----------


class TestParseDailyHealth:
    def test_parses_all_seven_days(self):
        out = translator.parse_daily_health(_fx("queryDailyHealthData"))
        assert len(out) == 7
        assert out["2026-06-05"]["steps"] == 12377
        assert out["2026-06-05"]["stress_avg"] == 31
        assert out["2026-06-05"]["sleep_score"] == 77
        assert out["2026-06-05"]["sleep_total_min"] == 8 * 60 + 27
        assert out["2026-06-05"]["sleep_deep_min"] == 65
        assert out["2026-06-07"]["exercise_min"] == 214  # "3h 34min"

    def test_today_has_no_sleep_yet(self):
        out = translator.parse_daily_health(_fx("queryDailyHealthData"))
        assert "sleep_score" not in out["2026-06-11"]
        assert out["2026-06-11"]["stress_avg"] == 60

    def test_mangled_text_returns_empty_not_raise(self):
        assert translator.parse_daily_health("Completely Different Format") == {}
        assert translator.parse_daily_health("") == {}


class TestParseSleep:
    def test_parses_main_sleep_and_naps(self):
        out = translator.parse_sleep(_fx("querySleepData"))
        assert out["2026-06-10"]["sleep_score"] == 96
        assert out["2026-06-10"]["sleep_duration_min"] == 6 * 60 + 24
        assert out["2026-06-10"]["sleep_nap_min"] == 122
        assert out["2026-06-09"]["sleep_nap_min"] == 0
        assert out["2026-06-09"]["sleep_awake_min"] == 4

    def test_dedicated_score_differs_from_daily_health(self):
        # Known COROS quirk: 2026-06-09 scores 79 here but 83 in
        # queryDailyHealthData. The merge must prefer this tool's value.
        out = translator.parse_sleep(_fx("querySleepData"))
        assert out["2026-06-09"]["sleep_score"] == 79


class TestParseHrv:
    def test_baseline_range_and_days(self):
        out = translator.parse_hrv(_fx("queryHrvAssessment"))
        assert out["baseline"] == 82
        assert (out["range_low"], out["range_high"]) == (68, 96)
        assert out["days"]["2026-06-10"] == {"hrv_avg": 102, "evaluation": "Above normal"}
        assert out["days"]["2026-06-08"]["evaluation"] == "Normal"
        # No entry for today (2026-06-11) — COROS lags a day.
        assert "2026-06-11" not in out["days"]


class TestParseRestingHr:
    def test_no_data_days_omitted(self):
        out = translator.parse_resting_hr(_fx("queryRestingHeartRate"))
        assert out["2026-06-10"] == 49
        assert "2026-06-11" not in out  # "No data"
        assert len(out) == 6


class TestParseTrainingLoad:
    def test_per_day_loads(self):
        out = translator.parse_training_load(_fx("queryTrainingLoadAssessment"))
        assert out["2026-06-11"] == {
            "load_comment": "Optimized",
            "load_short_term": 128.0,
            "load_long_term": 105.0,
            "load_ratio": 1.21,
        }
        assert out["2026-06-07"]["load_comment"] == "Excessive"


class TestParseRecovery:
    def test_point_in_time(self):
        out = translator.parse_recovery(_fx("queryRecoveryStatus"))
        assert out == {"recovery_pct": 92, "recovery_level": "Heavy training allowed"}


# ---------- merge ----------


class TestMergeDailyRows:
    def test_one_row_per_date_sorted(self):
        rows = translator.merge_daily_rows(_bundle(), today=date(2026, 6, 11))
        dates = [r["date"] for r in rows]
        assert dates == sorted(dates)
        assert "2026-06-11" in dates
        assert "2026-06-04" in dates  # sleep fixture reaches one day further back

    def test_sleep_tool_beats_daily_health(self):
        rows = translator.merge_daily_rows(_bundle(), today=date(2026, 6, 11))
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-06-09"]["sleep_score"] == 79  # querySleepData wins
        # Stage minutes come from daily health (sleep tool only has ratios).
        assert by_date["2026-06-09"]["sleep_deep_min"] == 70

    def test_recovery_and_raw_only_on_today(self):
        rows = translator.merge_daily_rows(_bundle(), today=date(2026, 6, 11))
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-06-11"]["recovery_pct"] == 92
        assert by_date["2026-06-11"]["recovery_level"] == "Heavy training allowed"
        assert "recovery_pct" not in by_date["2026-06-10"]
        assert "raw" in by_date["2026-06-11"]
        assert "raw" not in by_date["2026-06-10"]

    def test_raw_round_trips_the_bundle(self):
        bundle = _bundle()
        rows = translator.merge_daily_rows(bundle, today=date(2026, 6, 11))
        today_row = next(r for r in rows if r["date"] == "2026-06-11")
        assert json.loads(today_row["raw"]) == bundle

    def test_hrv_baseline_denormalized_onto_hrv_days(self):
        rows = translator.merge_daily_rows(_bundle(), today=date(2026, 6, 11))
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-06-10"]["hrv_baseline"] == 82
        assert by_date["2026-06-10"]["hrv_range_high"] == 96

    def test_empty_bundle_yields_no_rows(self):
        assert translator.merge_daily_rows({}, today=date(2026, 6, 11)) == []

    def test_partial_bundle_still_produces_rows(self):
        bundle = {"queryTrainingLoadAssessment": _fx("queryTrainingLoadAssessment")}
        rows = translator.merge_daily_rows(bundle, today=date(2026, 6, 11))
        assert len(rows) == 7
        assert all("sleep_score" not in r for r in rows)

    def test_raw_survives_total_parse_failure(self):
        """Format-change insurance: even when NOTHING parses, today's row
        must carry the raw bundle."""
        bundle = {t: '"Mystery New Format"' for t in ("queryDailyHealthData", "querySleepData")}
        rows = translator.merge_daily_rows(bundle, today=date(2026, 6, 11))
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-06-11"
        assert json.loads(rows[0]["raw"]) == bundle

    def test_metric_keys_match_state_manager_columns(self):
        """Drift detector: translator METRIC_KEYS and StateManager._HEALTH_COLS
        must stay identical — a key added to one but not the other silently
        drops data (upsert) or breaks the format-change counter (ingest)."""
        from state_manager import StateManager

        assert tuple(translator.METRIC_KEYS) == tuple(StateManager._HEALTH_COLS)
