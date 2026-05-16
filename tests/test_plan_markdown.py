"""Tests for plan_markdown — the plan-blob parsers and the week renderer."""

import pytest

from plan_markdown import (
    build_plan_meta,
    infer_workout_type,
    parse_plan_rows,
    parse_workout_details,
    render_week_table,
)

PLAN = """\
# Training Plan

## Active Goals

- A race: Test Half — 2026-05-16.

## Phase 1

### This Week (2026-05-08 → 2026-05-10)

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Fri | 2026-05-08 | Easy 8mi STRICT | 8:30-9:00 | base |
| Sat | 2026-05-09 | 5mi w/ 3x1000m | 6:00 reps | quality |
| Sun | 2026-05-10 | — | — | skipped (empty) |

### Workout Notes

Intro prose that should survive into plan_meta.

#### 2026-05-09
Sharpening session. Finish feeling fresh.

## Reference

### Target paces
- Easy: 8:30-9:00
"""


class TestParsePlanRows:
    def test_parses_locked_rows(self):
        rows = parse_plan_rows(PLAN)
        assert [r["date"] for r in rows] == ["2026-05-08", "2026-05-09"]
        assert rows[0]["workout"] == "Easy 8mi STRICT"

    def test_skips_empty_workout(self):
        rows = parse_plan_rows(PLAN)
        assert all(r["date"] != "2026-05-10" for r in rows)

    def test_skips_non_iso_date(self):
        text = "| Day | Date | Workout | Pace target | Notes |\n| Sat | 5/9 | Easy 8mi | x | y |\n"
        assert parse_plan_rows(text) == []


class TestParseWorkoutDetails:
    def test_extracts_block_until_next_heading(self):
        details = parse_workout_details(PLAN)
        assert "2026-05-09" in details
        assert "Sharpening session" in details["2026-05-09"]
        assert "Target paces" not in details["2026-05-09"]

    def test_empty_body_omitted(self):
        text = "#### 2026-05-12\n\n#### 2026-05-13\nHas content.\n"
        details = parse_workout_details(text)
        assert "2026-05-12" not in details
        assert details["2026-05-13"] == "Has content."

    def test_h3_anchor_not_parsed(self):
        assert parse_workout_details("### 2026-05-12\nbody\n") == {}


class TestBuildPlanMeta:
    def test_strips_table_and_detail_blocks(self):
        meta = build_plan_meta(PLAN)
        assert "| Fri |" not in meta
        assert "#### 2026-05-09" not in meta
        assert "Sharpening session" not in meta

    def test_keeps_prose(self):
        meta = build_plan_meta(PLAN)
        assert "Active Goals" in meta
        assert "Intro prose that should survive" in meta
        assert "Target paces" in meta


class TestInferWorkoutType:
    @pytest.mark.parametrize(
        "workout,expected",
        [
            ("", "rest"),
            ("—", "rest"),
            ("Rest + gentle yoga PM 30-40min", "rest"),
            ("Walk 10-15min + 5min mobility PM", "rest"),
            ("Easy 8mi STRICT", "easy"),
            ("Easy 4mi + restorative yoga PM", "easy"),
            ("AM fly SFO→Newark / PM 3mi shakeout + strides", "easy"),
            ("Cycling 60-75min, NO climbing", "cross"),
            ("Optional 20min spin OR rest", "cross"),
            ("5mi w/ 3x1000m + strength primer PM", "workout"),
            ("**BROOKLYN HALF**", "race"),
        ],
    )
    def test_classification(self, workout, expected):
        assert infer_workout_type(workout) == expected


class TestRenderWeekTable:
    def test_renders_locked_table(self):
        rows = [
            {"date": "2026-05-08", "workout": "Easy 8mi", "pace_target": "8:30", "notes": "base"},
            {"date": "2026-05-09", "workout": "Tempo 6mi", "pace_target": "6:30", "notes": ""},
        ]
        out = render_week_table(rows)
        assert "| Day | Date | Workout | Pace target | Notes |" in out
        assert "| Fri | 2026-05-08 | Easy 8mi | 8:30 | base |" in out
        assert "| Sat | 2026-05-09 | Tempo 6mi | 6:30 |  |" in out

    def test_accepts_prescribed_keys(self):
        """Rows straight from the sessions table use prescribed_* keys."""
        rows = [
            {
                "date": "2026-05-08",
                "prescribed_workout": "Easy 8mi",
                "prescribed_pace": "8:30",
                "prescribed_notes": "base",
            }
        ]
        out = render_week_table(rows)
        assert "Easy 8mi" in out and "8:30" in out

    def test_round_trips_through_parse(self):
        rows = [{"date": "2026-05-08", "workout": "Easy 8mi", "pace_target": "8:30", "notes": "base"}]
        reparsed = parse_plan_rows(render_week_table(rows))
        assert reparsed[0]["date"] == "2026-05-08"
        assert reparsed[0]["workout"] == "Easy 8mi"
