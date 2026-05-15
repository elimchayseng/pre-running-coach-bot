"""Tests for the Phase 1A plan-as-rows migration (scripts/migrate_plan_to_sessions.py)."""

import json
from pathlib import Path

import pytest

from scripts.migrate_plan_to_sessions import _build_plan_meta, _infer_planned_type, migrate
from state_manager import StateManager

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
| Sun | 2026-05-10 | Cycling 60-75min | HR <140 | cross |

### Workout Notes

Intro prose that should survive into plan_meta.

#### 2026-05-09
Sharpening session. Finish feeling fresh.

## Reference

### Target paces
- Easy: 8:30-9:00
"""


class TestInferPlannedType:
    @pytest.mark.parametrize(
        "workout,expected",
        [
            ("", "rest"),
            ("—", "rest"),
            ("Rest + gentle yoga PM 30-40min", "rest"),
            ("Walk 10-15min + 5min mobility PM", "rest"),
            ("Easy 8mi STRICT", "easy"),
            ("Easy 4mi + restorative yoga PM", "easy"),  # yoga mention, still a run
            ("AM fly SFO→Newark / PM 3mi shakeout + strides", "easy"),  # strides ≠ ride
            ("Cycling 60-75min, NO climbing", "cross"),  # 75min ≠ 75mi
            ("Optional 20min spin OR rest", "cross"),
            ("5mi w/ 3x1000m + strength primer PM", "workout"),
            ("**BROOKLYN HALF**", "race"),
        ],
    )
    def test_classification(self, workout, expected):
        assert _infer_planned_type(workout) == expected


class TestBuildPlanMeta:
    def test_strips_table_and_detail_blocks(self):
        meta = _build_plan_meta(PLAN)
        assert "| Fri |" not in meta
        assert "#### 2026-05-09" not in meta
        assert "Sharpening session" not in meta

    def test_keeps_prose(self):
        meta = _build_plan_meta(PLAN)
        assert "Active Goals" in meta
        assert "Intro prose that should survive" in meta
        assert "Target paces" in meta


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    db_path = tmp_path / "coach.db"
    sm = StateManager()
    sm.db_path = db_path
    sm.state_dir = db_path.parent
    sm._schema_applied = False
    with sm._conn() as c:
        c.execute("INSERT INTO plan (id, content) VALUES (1, ?)", (PLAN,))
        # One completed session that overlaps a planned date (forces a merge),
        # one that does not (forces a standalone insert).
        for sdate, stype in (("2026-05-09", "workout"), ("2026-04-01", "easy")):
            c.execute(
                "INSERT INTO sessions (date, type, data) VALUES (?, ?, ?)",
                (sdate, stype, json.dumps({"date": sdate, "type": stype, "miles": 5})),
            )
    return db_path


def _rows(db_path: Path, where: str = "1=1") -> list:
    sm = StateManager()
    sm.db_path = db_path
    sm._schema_applied = False
    with sm._conn() as c:
        return c.execute(
            f"SELECT date, status, type, prescribed_workout, data FROM sessions_v2 WHERE {where} ORDER BY date"
        ).fetchall()


class TestMigrate:
    def test_populates_planned_and_completed(self, db):
        summary = migrate(db)
        assert summary["planned_inserted"] == 3
        assert summary["completed_merged"] == 1  # 2026-05-09 overlaps a plan row
        assert summary["completed_inserted"] == 1  # 2026-04-01 standalone

    def test_merge_keeps_prescription_and_actuals(self, db):
        migrate(db)
        merged = [r for r in _rows(db) if r["date"] == "2026-05-09"]
        assert len(merged) == 1
        row = merged[0]
        assert row["status"] == "completed"
        assert row["prescribed_workout"] == "5mi w/ 3x1000m"  # prescription kept
        assert row["type"] == "workout"  # planned type kept, not the actual's
        assert json.loads(row["data"])["miles"] == 5  # actuals filled

    def test_merge_picks_type_matching_actual(self, tmp_path, monkeypatch):
        """A strength actual must not land on a run prescription when a
        run actual is available on the same date."""
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        db_path = tmp_path / "coach.db"
        sm = StateManager()
        sm.db_path = db_path
        sm.state_dir = db_path.parent
        sm._schema_applied = False
        with sm._conn() as c:
            c.execute("INSERT INTO plan (id, content) VALUES (1, ?)", (PLAN,))
            # 2026-05-08 is a run prescription ("Easy 8mi"). Insert the
            # strength session first so id-order would mis-pick it.
            for stype, marker in (("strength", "lift"), ("easy", "run")):
                c.execute(
                    "INSERT INTO sessions (date, type, data) VALUES (?, ?, ?)",
                    ("2026-05-08", stype, json.dumps({"date": "2026-05-08", "marker": marker})),
                )
        migrate(db_path)
        merged = [r for r in _rows(db_path) if r["date"] == "2026-05-08" and r["status"] == "completed"]
        run_row = [r for r in merged if r["prescribed_workout"]][0]
        assert json.loads(run_row["data"])["marker"] == "run"  # run actual chosen

    def test_plan_meta_written(self, db):
        migrate(db)
        sm = StateManager()
        sm.db_path = db
        sm._schema_applied = False
        with sm._conn() as c:
            content = c.execute("SELECT content FROM plan_meta WHERE id=1").fetchone()["content"]
        assert "Active Goals" in content
        assert "| Fri |" not in content

    def test_idempotent_noop(self, db):
        migrate(db)
        before = len(_rows(db))
        summary = migrate(db)
        assert summary["no_op"] is True
        assert len(_rows(db)) == before

    def test_force_rebuilds(self, db):
        migrate(db)
        summary = migrate(db, force=True)
        assert summary["no_op"] is False
        assert summary["planned_inserted"] == 3
        assert len(_rows(db)) == 4  # 3 planned (1 merged) + 1 standalone completed

    def test_old_tables_untouched(self, db):
        migrate(db)
        sm = StateManager()
        sm.db_path = db
        sm._schema_applied = False
        with sm._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
            assert c.execute("SELECT content FROM plan WHERE id=1").fetchone()["content"] == PLAN
