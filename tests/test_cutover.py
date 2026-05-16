"""Tests for the Phase 1A.2 cutover (scripts/cutover_to_unified_sessions.py).

Builds a realistic pre-cutover DB (the old v1 schema: a `plan` markdown blob
and a completed-only `sessions` table), runs the cutover, and asserts the
result is the unified v4 shape.
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scripts.cutover_to_unified_sessions import cutover
from state_manager import StateManager

# The pre-1A.1 schema (v1). Includes the old `sessions` indexes whose names
# collide globally with the new unified-table index names.
_OLD_SCHEMA = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE INDEX idx_sessions_date ON sessions(date DESC);
CREATE UNIQUE INDEX idx_sessions_strava_id
    ON sessions(json_extract(data, '$.details.strava_id'))
    WHERE json_extract(data, '$.details.strava_id') IS NOT NULL;
CREATE TABLE plan (id INTEGER PRIMARY KEY CHECK (id = 1), content TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE plan_changelog (id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE athlete (id INTEGER PRIMARY KEY CHECK (id = 1), yaml_text TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE journal (id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE gcal_sync_state (event_id TEXT PRIMARY KEY, hash TEXT, last_synced_at TEXT,
    completed INTEGER NOT NULL DEFAULT 0, last_completed_at TEXT, off_plan INTEGER NOT NULL DEFAULT 0);
"""

PLAN = """\
# Training Plan

## Active Goals

- A race: Test Half — 2026-05-16.

### This Week

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Fri | 2026-05-08 | Easy 8mi STRICT | 8:30-9:00 | base |
| Sat | 2026-05-09 | 5mi w/ 3x1000m | 6:00 reps | quality |

#### 2026-05-09
Sharpening session.

## Reference
- Easy: 8:30-9:00
"""


@pytest.fixture
def pre_cutover_db(tmp_path: Path, monkeypatch) -> Path:
    """A v1-schema DB with a plan blob and two logged sessions."""
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    db = tmp_path / "coach.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.execute("INSERT INTO plan (id, content) VALUES (1, ?)", (PLAN,))
    conn.execute("INSERT INTO plan_changelog (id, content) VALUES (1, 'seed\n')")
    # One session overlapping a planned date (forces a merge), one off-plan.
    for sdate, stype in (("2026-05-09", "workout"), ("2026-04-01", "easy")):
        conn.execute(
            "INSERT INTO sessions (date, type, data) VALUES (?, ?, ?)",
            (sdate, stype, json.dumps({"date": sdate, "type": stype, "miles": 5})),
        )
    conn.commit()
    conn.close()
    return db


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _indexes(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_%'",
                (table,),
            )
        }
    finally:
        conn.close()


class TestCutover:
    def test_renames_old_tables_to_archive(self, pre_cutover_db):
        cutover(pre_cutover_db)
        tables = _tables(pre_cutover_db)
        assert "sessions_v1_archive" in tables
        assert "plan_archive" in tables
        assert "sessions" in tables  # the unified table
        assert "plan" not in tables  # blob renamed away

    def test_unified_sessions_has_status_column(self, pre_cutover_db):
        cutover(pre_cutover_db)
        conn = sqlite3.connect(str(pre_cutover_db))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        finally:
            conn.close()
        assert {"status", "prescribed_workout", "detail_md", "data"} <= cols

    def test_index_names_normalized_despite_collision(self, pre_cutover_db):
        """The old `sessions` indexes share global names with the new ones —
        the cutover must drop the stale ones so the new indexes land."""
        cutover(pre_cutover_db)
        idx = _indexes(pre_cutover_db, "sessions")
        assert idx == {"idx_sessions_strava_id", "idx_sessions_date_status"}

    def test_strava_unique_index_enforced_after_cutover(self, pre_cutover_db):
        cutover(pre_cutover_db)
        sm = StateManager()
        sm.db_path = pre_cutover_db
        sm._schema_applied = False
        entry = {"date": "2026-06-01", "type": "easy", "details": {"strava_id": 7777}}
        sm.append_session(entry)
        with pytest.raises(sqlite3.IntegrityError):
            sm.append_session({**entry, "miles": 9})

    def test_data_migrated_planned_and_completed(self, pre_cutover_db):
        cutover(pre_cutover_db)
        conn = sqlite3.connect(str(pre_cutover_db))
        try:
            counts = dict(conn.execute("SELECT status, count(*) FROM sessions GROUP BY status"))
        finally:
            conn.close()
        # 2 plan rows; 2026-05-09 merges to completed, 2026-05-08 stays planned.
        assert counts.get("planned") == 1
        assert counts.get("completed") == 2  # merged 05-09 + standalone 04-01

    def test_schema_version_bumped(self, pre_cutover_db):
        cutover(pre_cutover_db)
        conn = sqlite3.connect(str(pre_cutover_db))
        try:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 4
        finally:
            conn.close()

    def test_idempotent_rerun(self, pre_cutover_db):
        cutover(pre_cutover_db)
        summary = cutover(pre_cutover_db)
        assert summary["already_cut_over"] is True

    def test_statemanager_reads_cutover_db(self, pre_cutover_db):
        cutover(pre_cutover_db)
        sm = StateManager()
        sm.db_path = pre_cutover_db
        sm._schema_applied = False
        w = sm.get_todays_workout(date(2026, 5, 8))
        assert w["found"] is True
        assert w["workout"] == "Easy 8mi STRICT"
        assert "Active Goals" in sm.get_plan_meta()

    def test_fresh_db_is_a_noop(self, tmp_path, monkeypatch):
        """A fresh StateManager DB is already v4 — cutover must no-op."""
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        db = tmp_path / "fresh.db"
        sm = StateManager()
        sm.db_path = db
        sm._schema_applied = False
        sm.get_plan_meta()  # triggers schema creation
        summary = cutover(db)
        assert summary["already_cut_over"] is True
