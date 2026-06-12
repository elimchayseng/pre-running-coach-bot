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
        assert out["2026-06-05"]["steps"] == 11842
        assert out["2026-06-05"]["stress_avg"] == 34
        assert out["2026-06-05"]["sleep_score"] == 81
        assert out["2026-06-05"]["sleep_total_min"] == 7 * 60 + 58
        assert out["2026-06-05"]["sleep_deep_min"] == 72
        assert out["2026-06-07"]["exercise_min"] == 246  # "4h 6min"

    def test_today_has_no_sleep_yet(self):
        out = translator.parse_daily_health(_fx("queryDailyHealthData"))
        assert "sleep_score" not in out["2026-06-11"]
        assert out["2026-06-11"]["stress_avg"] == 54

    def test_mangled_text_returns_empty_not_raise(self):
        assert translator.parse_daily_health("Completely Different Format") == {}
        assert translator.parse_daily_health("") == {}


class TestParseSleep:
    def test_parses_main_sleep_and_naps(self):
        out = translator.parse_sleep(_fx("querySleepData"))
        assert out["2026-06-10"]["sleep_score"] == 91
        assert out["2026-06-10"]["sleep_duration_min"] == 6 * 60 + 48
        assert out["2026-06-10"]["sleep_nap_min"] == 94
        assert out["2026-06-09"]["sleep_nap_min"] == 0
        assert out["2026-06-09"]["sleep_awake_min"] == 6

    def test_dedicated_score_differs_from_daily_health(self):
        # Known COROS quirk: 2026-06-09 scores 74 here but 80 in
        # queryDailyHealthData. The merge must prefer this tool's value.
        out = translator.parse_sleep(_fx("querySleepData"))
        assert out["2026-06-09"]["sleep_score"] == 74


class TestParseHrv:
    def test_baseline_range_and_days(self):
        out = translator.parse_hrv(_fx("queryHrvAssessment"))
        assert out["baseline"] == 75
        assert (out["range_low"], out["range_high"]) == (61, 89)
        assert out["days"]["2026-06-10"] == {"hrv_avg": 95, "evaluation": "Above normal"}
        assert out["days"]["2026-06-08"]["evaluation"] == "Normal"
        # No entry for today (2026-06-11) — COROS lags a day.
        assert "2026-06-11" not in out["days"]


class TestParseRestingHr:
    def test_no_data_days_omitted(self):
        out = translator.parse_resting_hr(_fx("queryRestingHeartRate"))
        assert out["2026-06-10"] == 47
        assert "2026-06-11" not in out  # "No data"
        assert len(out) == 6


class TestParseTrainingLoad:
    def test_per_day_loads(self):
        out = translator.parse_training_load(_fx("queryTrainingLoadAssessment"))
        assert out["2026-06-11"] == {
            "load_comment": "Optimized",
            "load_short_term": 112.0,
            "load_long_term": 96.0,
            "load_ratio": 1.17,
        }
        assert out["2026-06-07"]["load_comment"] == "Excessive"


class TestParseRecovery:
    def test_point_in_time(self):
        out = translator.parse_recovery(_fx("queryRecoveryStatus"))
        assert out == {"recovery_pct": 87, "recovery_level": "Heavy training allowed"}


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
        assert by_date["2026-06-09"]["sleep_score"] == 74  # querySleepData wins
        # Stage minutes come from daily health (sleep tool only has ratios).
        assert by_date["2026-06-09"]["sleep_deep_min"] == 76

    def test_recovery_and_raw_only_on_today(self):
        rows = translator.merge_daily_rows(_bundle(), today=date(2026, 6, 11))
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-06-11"]["recovery_pct"] == 87
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
        assert by_date["2026-06-10"]["hrv_baseline"] == 75
        assert by_date["2026-06-10"]["hrv_range_high"] == 89

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


class TestDateValidation:
    """Parsed dates become daily_health PRIMARY KEYs. A digit-shaped but
    impossible date ('2026-06-32') would poison the table: get_load_trend's
    date.fromisoformat() then raises on EVERY chat turn until the row is
    hand-deleted."""

    def test_iso_rejects_impossible_dates(self):
        assert translator._iso("20260632") is None
        assert translator._iso("20260230") is None
        assert translator._iso("20261301") is None
        assert translator._iso("20260611") == "2026-06-11"

    def test_parse_sleep_drops_invalid_date_sections(self):
        out = translator.parse_sleep("2026-06-32\nSleep Score: 80\n2026-06-11\nSleep Score: 75")
        assert "2026-06-32" not in out
        assert out["2026-06-11"]["sleep_score"] == 75

    def test_parse_training_load_drops_invalid_date_sections(self):
        out = translator.parse_training_load("2026-02-30\nComment: Optimized\n2026-06-11\nComment: Optimized")
        assert list(out) == ["2026-06-11"]

    def test_parse_resting_hr_drops_invalid_dates(self):
        out = translator.parse_resting_hr("2026-02-30: 50 bpm\n2026-06-11: 48 bpm")
        assert out == {"2026-06-11": 48}

    def test_parse_hrv_drops_invalid_dates(self):
        out = translator.parse_hrv("2026-06-32\nHRV Avg: 80 ms\n2026-06-11\nHRV Avg: 85 ms")
        assert list(out["days"]) == ["2026-06-11"]
