"""State management for the running coach.

Persists all bot/agent state in a single SQLite database (default
``state/coach.db`` locally, ``$DATABASE_PATH`` in prod, where Railway's
attached volume keeps writes durable across deploys and restarts).

Tables (see ``state/schema.sql``):
  athlete         — singleton row holding ``yaml_text`` (ruamel-preserved YAML)
  sessions        — unified plan-as-rows: one row per workout in a lifecycle
                    state (planned → completed/missed/off-plan). ``prescribed_*``
                    hold the prescription; ``data`` JSON holds the actuals once
                    logged. Partial UNIQUE index on ``details.strava_id``
                    enforces webhook idempotency.
  plan_meta       — singleton row holding plan prose (phases, goals, pace
                    zones, adjustment triggers)
  plan_changelog  — singleton row holding the append-only changelog
  journal         — singleton row holding append-only timestamped notes
  gcal_sync_state — per-event sync metadata (replaces .gcal_sync_state.json)

Every coach turn calls ``load_full_context()``.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ruamel.yaml import YAML

import plan_markdown
from notion.markdown import render_change_body

# Round-trip YAML — preserves comments, key order, quotes. update_athlete()
# depends on this; PyYAML would silently drop them.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

SCHEMA_PATH = Path(__file__).resolve().parent / "state" / "schema.sql"

# Current schema version. v4 was the Phase 1A cutover (unified `sessions`
# table, `plan_meta`); v5 adds the `reviews` table for Phase 1B.4; v6 adds
# the `sessions.reflection` column for the Notion-Workers bidirectional
# sync (athlete-owned post-run notes; see docs/notion-workers-architecture.md);
# v7 adds the `daily_health` table for the nightly COROS wearable pull
# (see docs/coros-mcp.md). scripts/cutover_to_unified_sessions.py handles
# v3→v4; v4→v5 / v5→v6 / v6→v7 are additive and land the next time
# _ensure_schema runs (v6 uses an ALTER TABLE … ADD COLUMN guarded by a
# PRAGMA table_info check; v5/v7 are plain CREATE TABLE IF NOT EXISTS).
CURRENT_SCHEMA_VERSION = 7

_JOURNAL_HEADER = "# Journal\n\nAppend-only freeform notes. Newest entries at the bottom.\n"

# Double-checked locking so two threads can't try to apply the schema
# simultaneously on the very first connect.
_schema_lock = threading.Lock()

# Run-shaped workout types — used to match a logged activity to a planned row.
# Covers both planned-row vocab ("long") and logged-session vocab ("long_run").
_RUN_LIKE = {"run", "easy", "workout", "long", "long_run", "race", "strides", "return_test", "tempo"}
_REST_PATTERNS = ("off", "rest", "no run", "no running")


# ---------- multi-session-per-day slot helpers ----------
#
# When a date has N>1 planned sessions, each row gets a string ordinal slot
# ("1", "2", ...) assigned at parse time. Single-session days stay slot=NULL.
# The bucket/label helpers below are the single source of truth used by
# Google Calendar event payload, Strava activity matching, and Notion title
# formatting — so the three surfaces stay in lockstep.


def slot_time_bucket(slot: Optional[str], total_slots: int) -> Optional[tuple[float, float]]:
    """Return (start_hour, end_hour) in local time for a multi-session slot.

    Returns None when the date is single-session (caller emits an all-day
    event / matches loosely on type). Hours are floats so 06:00–07:30 is
    (6.0, 7.5); split into HH:MM at the boundary with ``divmod(h*60, 60)``.

    Canonical layout:
        2 slots: 06:00–07:30, 17:30–19:00
        3 slots: 06:00–07:30, 12:00–13:00, 17:30–19:00
        4 slots: 06:00–07:30, 10:00–11:00, 14:00–15:00, 18:00–19:00
        5+ slots: linearly distributed across 06:30–19:30 centers, 1h each
    """
    if total_slots <= 1 or slot is None:
        return None
    try:
        idx = int(slot)
    except (ValueError, TypeError):
        return None
    if idx < 1 or idx > total_slots:
        return None
    if total_slots == 2:
        return [(6.0, 7.5), (17.5, 19.0)][idx - 1]
    if total_slots == 3:
        return [(6.0, 7.5), (12.0, 13.0), (17.5, 19.0)][idx - 1]
    if total_slots == 4:
        return [(6.0, 7.5), (10.0, 11.0), (14.0, 15.0), (18.0, 19.0)][idx - 1]
    # 5+ slots: spread 1-hour windows evenly. First center at 6.5h, last at
    # 19.5h — matches the AM/PM endpoints used for 2/3/4 above.
    first_center, last_center = 6.5, 19.5
    center = first_center + (last_center - first_center) * (idx - 1) / (total_slots - 1)
    return (center - 0.5, center + 0.5)


def slot_bucket_center(slot: Optional[str], total_slots: int) -> Optional[float]:
    """Midpoint hour of the slot's bucket, used by the Strava matcher."""
    bucket = slot_time_bucket(slot, total_slots)
    return None if bucket is None else (bucket[0] + bucket[1]) / 2


def _hour_from_start_local(start_local: Optional[str]) -> Optional[float]:
    """Pull a local-clock-time hour float from an ISO ``start_local`` field.

    Accepts "2026-05-12T06:32:00Z" or "2026-05-12T06:32:00" (Strava emits
    the Z suffix on ``start_date_local`` despite the value being a local
    clock-time). Returns None for missing / malformed input so the matcher
    falls back to type-only logic.
    """
    if not start_local or "T" not in start_local:
        return None
    time_part = start_local.split("T", 1)[1].rstrip("Z")
    parts = time_part.split(":")
    if len(parts) < 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h + m / 60.0


def slot_display_label(slot: Optional[str], total_slots: int) -> str:
    """User-facing label: '' for single, 'AM'/'PM' for two-a-day, 'k/N' for 3+."""
    if total_slots <= 1 or slot is None:
        return ""
    try:
        idx = int(slot)
    except (ValueError, TypeError):
        return ""
    if total_slots == 2:
        return "AM" if idx == 1 else "PM"
    return f"{idx}/{total_slots}"


class StateManager:
    """Reads and writes coach state, backed by SQLite."""

    def __init__(self, state_dir: Path | str | None = None) -> None:
        """Resolve the DB path.

        Precedence:
          1. ``$DATABASE_PATH`` env var (used in prod to point at the volume).
          2. Explicit ``state_dir`` argument → ``<state_dir>/coach.db``.
          3. Default ``state/coach.db`` under the repo root.
        """
        env_db = os.getenv("DATABASE_PATH")
        if env_db:
            self.db_path = Path(env_db)
        elif state_dir is not None:
            self.db_path = Path(state_dir) / "coach.db"
        else:
            self.db_path = Path("state") / "coach.db"
        self.state_dir = self.db_path.parent
        self._schema_applied = False

    # ---------- Connection plumbing ----------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, ensure schema is applied, commit/rollback + close."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema(conn)
        try:
            with conn:  # commits on clean exit, rolls back on exception
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_applied:
            return
        with _schema_lock:
            if self._schema_applied:
                return
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            sessions_exists = (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
                is not None
            )
            unified = sessions_exists and any(r[1] == "status" for r in conn.execute("PRAGMA table_info(sessions)"))
            if not sessions_exists:
                # Fresh DB — apply the full v4 schema.
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                unified = True
            elif unified:
                # Already unified; re-running schema.sql is idempotent.
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            # else: pre-cutover DB — `sessions` is the old completed-only
            # table. schema.sql's unified-shape DDL (the date+status index)
            # would fail, so we don't apply it. scripts/cutover_to_unified_
            # sessions.py owns that migration.
            if unified:
                # v5 → v6: additive ALTER TABLE for the athlete-owned reflection
                # column. SQLite's CREATE TABLE IF NOT EXISTS in schema.sql is a
                # no-op when the table already exists, so a column added after
                # the table was first created has to come in via ALTER TABLE.
                cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
                if "reflection" not in cols:
                    conn.execute("ALTER TABLE sessions ADD COLUMN reflection TEXT DEFAULT NULL")
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (CURRENT_SCHEMA_VERSION if unified else 3,),
            )
            conn.commit()
            self._schema_applied = True

    # ---------- Athlete ----------

    def load_athlete(self) -> dict:
        """Parse athlete YAML into a dict. Returns {} if no athlete row."""
        text = self._load_athlete_yaml()
        if not text:
            return {}
        data = _yaml.load(text)
        return data or {}

    def _load_athlete_yaml(self) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT yaml_text FROM athlete WHERE id = 1").fetchone()
        return row["yaml_text"] if row else ""

    def update_athlete(self, updates: dict) -> None:
        """Patch fields in athlete YAML. Preserves comments, key order, quotes."""
        with self._conn() as conn:
            row = conn.execute("SELECT yaml_text FROM athlete WHERE id = 1").fetchone()
            if row is None:
                raise FileNotFoundError("athlete row not found; run scripts/migrate_state_to_sqlite.py first")
            data = _yaml.load(row["yaml_text"])
            if data is None:
                data = {}
            _deep_merge(data, updates)
            buf = io.StringIO()
            _yaml.dump(data, buf)
            conn.execute(
                "UPDATE athlete SET yaml_text = ?, updated_at = datetime('now') WHERE id = 1",
                (buf.getvalue(),),
            )

    # ---------- Plan: meta + rows ----------

    def get_plan_meta(self) -> str:
        """Return the plan prose (phases, goals, pace zones, triggers)."""
        with self._conn() as conn:
            row = conn.execute("SELECT content FROM plan_meta WHERE id = 1").fetchone()
        return row["content"] if row else ""

    def render_current_week_markdown(self, today: Optional[date] = None) -> str:
        """Render the current week's prescriptions as the locked markdown table."""
        ref = today or date.today()
        monday = ref - timedelta(days=ref.weekday())
        sunday = monday + timedelta(days=6)
        rows = self.get_prescription_rows(monday, sunday)
        if not rows:
            return "_No workouts prescribed for this week yet._"
        return plan_markdown.render_week_table(rows)

    def render_plan(self, today: Optional[date] = None) -> str:
        """Compose the plan view: prose + this week's locked table.

        Used by the ``/plan`` command, the post-activity review prompt, and
        the system-prompt training-plan block.
        """
        meta = self.get_plan_meta().rstrip()
        week = self.render_current_week_markdown(today)
        parts = [meta] if meta else []
        parts.extend(["", "## This week", "", week])
        return "\n".join(parts).strip() + "\n"

    def update_plan_meta(self, content: str, change_note: str) -> None:
        """Replace plan_meta and log to the changelog."""
        with self._conn() as conn:
            prev_row = conn.execute("SELECT content FROM plan_meta WHERE id = 1").fetchone()
            before = prev_row["content"] if prev_row else ""
            conn.execute(
                "INSERT INTO plan_meta (id, content, updated_at) VALUES (1, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
                (content,),
            )
            body = render_change_body(
                before, content, before_heading="Before (plan_meta)", after_heading="After (plan_meta)"
            )
            change_entry = self._append_changelog(conn, change_note, body=body)
        self._notify_mirror_change(change_entry)

    def update_plan(self, new_plan_md: str, change_note: str) -> None:
        """Escape hatch: replace the whole plan from a full markdown document.

        Parses the locked weekly table into ``planned`` rows, the per-day
        ``#### YYYY-MM-DD`` blocks into ``detail_md``, and the remaining prose
        into ``plan_meta``. Planned rows are replaced wholesale; completed /
        missed / off-plan rows are never touched. Used to apply a post-activity
        review proposal verbatim.

        Raises ``ValueError`` if a new (date, slot) assignment would collide
        with an existing completed / missed / off-plan row — a slot's history
        is anchored once written and the parser is not allowed to reuse the
        ordinal for a different prescription (issue #46 W5).
        """
        rows = plan_markdown.parse_plan_rows(new_plan_md)
        details = plan_markdown.parse_workout_details(new_plan_md)
        meta = plan_markdown.build_plan_meta(new_plan_md)
        with self._conn() as conn:
            _validate_new_slots_against_history(conn, rows)
            before_rows = [
                _row_dict(r)
                for r in conn.execute(
                    "SELECT * FROM sessions WHERE status = 'planned' ORDER BY date, slot, id"
                ).fetchall()
            ]
            conn.execute("DELETE FROM sessions WHERE status = 'planned'")
            for r in rows:
                conn.execute(
                    "INSERT INTO sessions "
                    "(date, slot, status, type, prescribed_workout, prescribed_pace, "
                    " prescribed_notes, detail_md) "
                    "VALUES (?, ?, 'planned', ?, ?, ?, ?, ?)",
                    (
                        r["date"],
                        r.get("slot"),
                        plan_markdown.infer_workout_type(r["workout"]),
                        r["workout"],
                        r["pace_target"],
                        r["notes"],
                        details.get(r["date"]),
                    ),
                )
            conn.execute(
                "INSERT INTO plan_meta (id, content, updated_at) VALUES (1, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
                (meta,),
            )
            after_rows = [
                _row_dict(r)
                for r in conn.execute(
                    "SELECT * FROM sessions WHERE status = 'planned' ORDER BY date, slot, id"
                ).fetchall()
            ]
            body = render_change_body(_format_session_rows(before_rows), _format_session_rows(after_rows))
            change_entry = self._append_changelog(conn, change_note, body=body)
        self._notify_mirror(self._rows("status = 'planned'", ()))
        self._notify_mirror_change(change_entry)

    def update_workout(
        self,
        target_date: date,
        change_note: str,
        workout: Optional[str] = None,
        pace_target: Optional[str] = None,
        notes: Optional[str] = None,
        detail_body: Optional[str] = None,
    ) -> None:
        """Patch a single day's prescription row.

        Updates the existing row for ``target_date`` (only the fields passed
        are touched). Raises ``ValueError`` if no row exists for the date so
        the LLM can self-route to ``replace_week_table`` (to add the
        containing week) or ``update_plan`` (for structural changes) instead
        of silently inserting an orphan day.
        """
        has_edit = any(v is not None for v in (workout, pace_target, notes, detail_body))
        if not has_edit:
            raise ValueError("must pass at least one of workout, pace_target, notes, detail_body")
        iso = target_date.isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, status FROM sessions WHERE date = ? AND status = 'planned' ORDER BY slot LIMIT 1",
                (iso,),
            ).fetchone()
            if row is None:
                # No planned row — fall back to any prescription row on the
                # date (a completed/missed/off-plan row is still a valid
                # patch target for detail_body etc.).
                row = conn.execute(
                    "SELECT id, status FROM sessions WHERE date = ? ORDER BY slot LIMIT 1", (iso,)
                ).fetchone()
            if row is None:
                raise ValueError(
                    f"no row found in locked table for date {iso} — to add the "
                    "week containing this date, call replace_week_table; for a "
                    "structural plan change, call update_plan"
                )
            before_row = _row_dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (row["id"],)).fetchone())
            sets, params = [], []
            if workout is not None:
                sets.append("prescribed_workout = ?")
                params.append(workout)
                sets.append("type = ?")
                params.append(plan_markdown.infer_workout_type(workout))
            if pace_target is not None:
                sets.append("prescribed_pace = ?")
                params.append(pace_target)
            if notes is not None:
                sets.append("prescribed_notes = ?")
                params.append(notes)
            if detail_body is not None:
                sets.append("detail_md = ?")
                params.append(detail_body.strip() or None)
            sets.append("updated_at = datetime('now')")
            params.append(row["id"])
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
            affected_id = row["id"]
            after_row = _row_dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (affected_id,)).fetchone())
            body = render_change_body(_format_session_row_short(before_row), _format_session_row_short(after_row))
            change_entry = self._append_changelog(conn, change_note, body=body)
        self._notify_mirror(self._rows("id = ?", (affected_id,)))
        self._notify_mirror_change(change_entry)

    def replace_week_table(self, rows: list[dict], change_note: str) -> None:
        """Replace a week's planned rows.

        Each row dict needs keys: day, date, workout, pace_target, notes.
        Planned rows inside the [min, max] date span that aren't in ``rows``
        are dropped; days that already have a completed/off-plan row are left
        alone (history is never overwritten with a fresh prescription).
        """
        if not rows:
            raise ValueError("rows must be non-empty")
        required = ("day", "date", "workout", "pace_target", "notes")
        for r in rows:
            missing = [k for k in required if k not in r]
            if missing:
                raise ValueError(f"row missing required keys {missing}: {r}")
        dates = sorted(r["date"] for r in rows)
        # Caller may pass duplicate-date rows for two-a-days; stamp ordinal
        # slots so the INSERT respects UNIQUE(date, slot). A caller that
        # already populated `slot` (e.g. a future tool) is honored.
        if not all("slot" in r for r in rows):
            plan_markdown.assign_slot_ordinals(rows)
        with self._conn() as conn:
            _validate_new_slots_against_history(conn, rows)
            before_rows = [
                _row_dict(r)
                for r in conn.execute(
                    "SELECT * FROM sessions WHERE status = 'planned' AND date BETWEEN ? AND ? ORDER BY date, slot, id",
                    (dates[0], dates[-1]),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM sessions WHERE status = 'planned' AND date BETWEEN ? AND ?",
                (dates[0], dates[-1]),
            )
            for r in rows:
                done = conn.execute(
                    "SELECT 1 FROM sessions WHERE date = ? AND status IN ('completed','off-plan') LIMIT 1",
                    (r["date"],),
                ).fetchone()
                if done:
                    continue  # don't shadow a logged day with a fresh prescription
                conn.execute(
                    "INSERT INTO sessions "
                    "(date, slot, status, type, prescribed_workout, prescribed_pace, prescribed_notes) "
                    "VALUES (?, ?, 'planned', ?, ?, ?, ?)",
                    (
                        r["date"],
                        r.get("slot"),
                        plan_markdown.infer_workout_type(r["workout"]),
                        r["workout"],
                        r["pace_target"],
                        r["notes"],
                    ),
                )
            after_rows = [
                _row_dict(r)
                for r in conn.execute(
                    "SELECT * FROM sessions WHERE status = 'planned' AND date BETWEEN ? AND ? ORDER BY date, slot, id",
                    (dates[0], dates[-1]),
                ).fetchall()
            ]
            body = render_change_body(_format_session_rows(before_rows), _format_session_rows(after_rows))
            change_entry = self._append_changelog(conn, change_note, body=body)
        self._notify_mirror(self._rows("status = 'planned' AND date BETWEEN ? AND ?", (dates[0], dates[-1])))
        self._notify_mirror_change(change_entry)

    def _append_changelog(self, conn: sqlite3.Connection, note: str, body: Optional[str] = None) -> dict:
        """Append a timestamped note to the changelog blob.

        Returns ``{"timestamp", "note", "action", "body"}`` so the caller can
        mirror the new entry to Notion after the transaction commits. ``body``
        (if non-empty) is the fenced before/after markdown that lands on the
        Plan Changes page.
        """
        ts = datetime.now().isoformat(timespec="seconds")
        line = f"- {ts}: {note}\n"
        conn.execute(
            "INSERT INTO plan_changelog (id, content, updated_at) VALUES (1, ?, datetime('now')) "
            "ON CONFLICT(id) DO UPDATE SET "
            "content = plan_changelog.content || excluded.content, updated_at = excluded.updated_at",
            (line,),
        )
        action = "completed" if " completed:" in note or note.startswith("completed:") else "planned-edit"
        return {"timestamp": ts, "note": note, "action": action, "body": body}

    # ---------- Plan-row reads ----------

    def get_prescription_rows(self, start: date, end: date) -> list[dict]:
        """Return rows carrying a prescription in [start, end], ordered by date."""
        return self._rows(
            "date BETWEEN ? AND ? AND prescribed_workout IS NOT NULL",
            (start.isoformat(), end.isoformat()),
        )

    def get_rows_in_range(self, start: date, end: date) -> list[dict]:
        """Return all session rows in [start, end], ordered by date, slot, id."""
        return self._rows("date BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))

    def get_workout_row(self, target: date) -> Optional[dict]:
        """Return the first prescription row for a date (planned preferred), or None.

        On multi-session days this is slot 1 only — callers that need to render
        every session should use :meth:`get_workout_rows`. Kept for backwards
        compatibility with status checks, single-session UIs, and tests.
        """
        rows = self.get_workout_rows(target)
        if not rows:
            return None
        planned = [r for r in rows if r["status"] == "planned"]
        return planned[0] if planned else rows[0]

    def get_workout_rows(self, target: date) -> list[dict]:
        """Return every prescription row for a date, ordered by slot.

        One row per session — empty list when the date has no prescription.
        Use this anywhere a multi-session day must be surfaced in full (chat
        bot summaries, LLM tools, post-activity reviews).
        """
        return self._rows(
            "date = ? AND prescribed_workout IS NOT NULL",
            (target.isoformat(),),
        )

    def get_todays_workout(self, target_date: Optional[date] = None) -> dict:
        """Return the prescribed workout for a date.

        Single-row view: returns the first slot. ``slot`` and ``total_slots``
        are populated so callers can detect multi-session days and route to
        :meth:`get_todays_workouts` when needed.

        Keys: date, day_name, workout, pace_target, notes, detail_md, status,
        is_rest_day, found, slot, total_slots.
        """
        if target_date is None:
            target_date = date.today()
        rows = self.get_workout_rows(target_date)
        result = _empty_workout(target_date)
        if not rows:
            return result
        row = next((r for r in rows if r["status"] == "planned"), rows[0])
        result.update(_workout_dict_from_row(row, total_slots=len(rows)))
        return result

    def get_todays_workouts(self, target_date: Optional[date] = None) -> list[dict]:
        """Return every prescribed workout for a date (one dict per slot).

        Each entry carries the same shape as :meth:`get_todays_workout` plus
        ``slot`` and ``total_slots``. Empty list when no prescription exists.
        """
        if target_date is None:
            target_date = date.today()
        rows = self.get_workout_rows(target_date)
        n = len(rows)
        return [_workout_dict_from_row(r, total_slots=n, date_override=target_date) for r in rows]

    def _rows(self, where: str, params: tuple) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(f"SELECT * FROM sessions WHERE {where} ORDER BY date, slot, id", params).fetchall()
        return [dict(r) for r in rows]

    # ---------- Session (actuals) reads ----------

    def get_recent_sessions(self, days: int = 14, today: Optional[date] = None) -> list[dict]:
        ref = today or date.today()
        cutoff = ref - timedelta(days=days)
        return self._session_data("date >= ? AND date <= ?", (cutoff.isoformat(), ref.isoformat()))

    def get_sessions_in_range(self, start: date, end: date) -> list[dict]:
        return self._session_data("date >= ? AND date <= ?", (start.isoformat(), end.isoformat()))

    def sessions_on_date(self, target: date) -> list[dict]:
        return self._session_data("date = ?", (target.isoformat(),))

    def get_session_rows_on_date(self, target: date) -> list[dict]:
        """Return every full session row (any status) for a date, ordered by slot.

        Unlike ``sessions_on_date`` which returns only the ``data`` JSON of
        logged entries, this returns the full row dicts (id, slot, status,
        prescribed_*, data, detail_md, ...). Callers that need to mirror
        per-slot calendar / Notion state — particularly mark_complete on
        multi-session days — should use this so they can identify which slot
        each entry belongs to.
        """
        return self._rows("date = ?", (target.isoformat(),))

    def _session_data(self, where: str, params: tuple) -> list[dict]:
        """Return the ``data`` JSON of logged (completed/off-plan) sessions.

        When the row carries an athlete-owned ``reflection`` (post-run note
        synced back from Notion via the Worker bridge), it's injected into
        the returned dict under the ``reflection`` key so the coach prompt
        and the ``get_sessions`` tool see it alongside the actuals without
        a second query.
        """
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT data, reflection FROM sessions WHERE {where} AND data IS NOT NULL ORDER BY date, slot, id",
                params,
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                entry = json.loads(r["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            if r["reflection"]:
                entry["reflection"] = r["reflection"]
            out.append(entry)
        return out

    def existing_strava_ids(self) -> set[int]:
        """Return all ``details.strava_id`` values from logged sessions."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT CAST(json_extract(data, '$.details.strava_id') AS INTEGER) AS sid "
                "FROM sessions WHERE json_extract(data, '$.details.strava_id') IS NOT NULL"
            ).fetchall()
        return {r["sid"] for r in rows if r["sid"] is not None}

    # ---------- Session writes (reconciliation) ----------

    def append_session(self, session: dict) -> dict:
        """Log a completed session, reconciling it against the plan.

        Delegates to :meth:`reconcile_strava_activity` — a logged activity
        either completes a matching planned row or lands as an off-plan row.
        """
        return self.reconcile_strava_activity(session)

    def reconcile_strava_activity(self, session: dict) -> dict:
        """Match a logged activity to the plan and record it.

        - A planned row on the activity's date whose type matches (or the
          single planned row if there's only one) flips to ``completed`` with
          the actuals filled in; a changelog row is written.
        - Otherwise a new ``off-plan`` row is inserted (no changelog entry).

        Raises ``sqlite3.IntegrityError`` if ``details.strava_id`` is set and a
        row already carries it — webhook callers treat that as already-logged.
        Returns a small dict: {matched, status, row_id}.
        """
        if "date" not in session:
            raise ValueError("session entry must include a 'date' field")
        sdate = str(session["date"])[:10]
        stype = session.get("type") or ""
        payload = json.dumps(session, ensure_ascii=False)
        change_entry: Optional[dict] = None
        # Activity hour drives slot routing on multi-session days. Missing
        # start_local (legacy entries) falls through to type-only matching.
        activity_hour = _hour_from_start_local(session.get("start_local"))
        with self._conn() as conn:
            planned = conn.execute(
                "SELECT id, type, slot FROM sessions WHERE date = ? AND status = 'planned' ORDER BY slot, id",
                (sdate,),
            ).fetchall()
            total_slots = sum(1 for r in planned if r["slot"] is not None)
            match = _pick_planned_match(stype, planned, activity_hour=activity_hour, total_slots=total_slots or None)
            if match is not None:
                before_row = _row_dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (match["id"],)).fetchone())
                conn.execute(
                    "UPDATE sessions SET status = 'completed', data = ?, type = ?, "
                    "completed_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                    (payload, stype or None, match["id"]),
                )
                body = render_change_body(
                    _format_session_row_short(before_row),
                    _format_actuals(payload),
                    before_heading="Prescribed",
                    after_heading="Actuals",
                )
                change_entry = self._append_changelog(conn, f"{sdate} completed: {stype or 'session'}", body=body)
                result = {"matched": True, "status": "completed", "row_id": match["id"]}
            else:
                cur = conn.execute(
                    "INSERT INTO sessions (date, slot, status, type, data, completed_at) "
                    "VALUES (?, NULL, 'off-plan', ?, ?, datetime('now'))",
                    (sdate, stype or None, payload),
                )
                result = {"matched": False, "status": "off-plan", "row_id": cur.lastrowid}
        self._notify_mirror(self._rows("id = ?", (result["row_id"],)))
        self._notify_mirror_change(change_entry)  # None when the activity went off-plan
        return result

    def set_session_reflection(self, session_id: int, text: Optional[str]) -> bool:
        """Write the athlete-owned reflection text for one session row.

        Returns True when a row was updated. Called by the Railway bridge
        endpoint (``PUT /sessions/<id>/reflection``) on behalf of the Notion
        Worker. ``text`` of None / "" clears the column. The Notion mirror
        never writes the Reflection property back — see
        ``notion/mirror._session_properties`` for the contract — so a
        ``_notify_mirror`` here cannot cause an echo loop.
        """
        normalized: Optional[str] = (text or "").strip() or None
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sessions SET reflection = ?, updated_at = datetime('now') WHERE id = ?",
                (normalized, session_id),
            )
            updated = cur.rowcount > 0
        if updated:
            self._notify_mirror(self._rows("id = ?", (session_id,)))
        return updated

    def update_session_by_strava_id(self, activity_id: int, new_entry: dict) -> bool:
        """Replace the actuals of the row carrying ``details.strava_id``.

        Returns True if a row was updated. Used by the Strava webhook on an
        ``aspect_type=update`` event (e.g. a Run retagged as a Workout).
        """
        if "date" not in new_entry:
            raise ValueError("new_entry must include a 'date' field")
        payload = json.dumps(new_entry, ensure_ascii=False)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sessions SET data = ?, date = ?, type = ?, updated_at = datetime('now') "
                "WHERE json_extract(data, '$.details.strava_id') = ?",
                (payload, str(new_entry["date"])[:10], new_entry.get("type") or None, activity_id),
            )
            updated = cur.rowcount > 0
        if updated:
            self._notify_mirror(self._rows("json_extract(data, '$.details.strava_id') = ?", (activity_id,)))
        return updated

    def _notify_mirror(self, rows: list[dict]) -> None:
        """Best-effort: reflect written session rows into the Notion mirror.

        Lazy-imported and fully exception-swallowing — the mirror is optional
        and a Notion problem must never break a SQLite write. Each row is
        annotated in-place with ``total_slots_on_date`` (the count of
        prescription rows sharing its date) so the mirror can format
        multi-session titles correctly.
        """
        if not rows:
            return
        self._stamp_total_slots_on_date(rows)
        try:
            from notion.mirror import mirror_sessions

            mirror_sessions(rows)
        except Exception:  # noqa: BLE001 — mirror failures never propagate
            pass

    def _stamp_total_slots_on_date(self, rows: list[dict]) -> None:
        """Annotate each row with the prescription-row count for its date."""
        dates = {r["date"] for r in rows if r.get("date")}
        if not dates:
            return
        placeholders = ",".join("?" * len(dates))
        with self._conn() as conn:
            result = conn.execute(
                f"SELECT date, COUNT(*) AS n FROM sessions "
                f"WHERE date IN ({placeholders}) AND prescribed_workout IS NOT NULL "
                f"GROUP BY date",
                tuple(dates),
            ).fetchall()
        counts = {r["date"]: r["n"] for r in result}
        for row in rows:
            row["total_slots_on_date"] = counts.get(row["date"], 0)

    def _notify_mirror_change(self, entry: Optional[dict]) -> None:
        """Best-effort mirror of one changelog entry to Notion Plan Changes."""
        if not entry:
            return
        try:
            from notion.mirror import mirror_plan_change

            mirror_plan_change(entry)
        except Exception:  # noqa: BLE001
            pass

    def _notify_mirror_journal(self, entry: Optional[dict]) -> None:
        """Best-effort mirror of one journal entry to Notion Journal."""
        if not entry:
            return
        try:
            from notion.mirror import mirror_journal_entry

            mirror_journal_entry(entry)
        except Exception:  # noqa: BLE001
            pass

    def _notify_mirror_review(self, entry: Optional[dict]) -> None:
        """Best-effort mirror of one review to Notion Reviews."""
        if not entry:
            return
        try:
            from notion.mirror import mirror_review

            mirror_review(entry)
        except Exception:  # noqa: BLE001
            pass

    def _notify_mirror_reviews(self, entries: list[dict]) -> None:
        """Best-effort batched mirror of N reviews in ONE daemon thread.

        Used by the nightly expire sweep so a 50-row batch fires one Notion
        worker, not 50. ``mirror_reviews`` upserts each row sequentially
        inside the single thread.
        """
        if not entries:
            return
        try:
            from notion.mirror import mirror_reviews

            mirror_reviews(entries)
        except Exception:  # noqa: BLE001
            pass

    # ---------- Reviews ----------

    def save_review(
        self,
        session_id: Optional[int],
        strava_id: Optional[int],
        review_date: date,
        critique: str,
        proposed_change: Optional[dict] = None,
    ) -> dict:
        """Persist a post-activity review and fire the Notion mirror.

        Returns the inserted row as a dict with ``proposed_change`` parsed
        back into a dict (it's stored as JSON in SQLite). ``status`` starts
        NULL (= Pending in the Notion view).
        """
        proposed_json = json.dumps(proposed_change, ensure_ascii=False) if proposed_change else None
        iso = review_date.isoformat() if isinstance(review_date, date) else str(review_date)[:10]
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO reviews (session_id, strava_id, date, critique, proposed_change) VALUES (?, ?, ?, ?, ?)",
                (session_id, strava_id, iso, critique, proposed_json),
            )
            row = _parse_review_row(
                dict(conn.execute("SELECT * FROM reviews WHERE id = ?", (cur.lastrowid,)).fetchone())
            )
        self._notify_mirror_review(row)
        return row

    def get_reviews_in_range(self, start: date, end: date) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE date BETWEEN ? AND ? ORDER BY date, id",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [_parse_review_row(dict(r)) for r in rows]

    def get_all_reviews(self) -> list[dict]:
        """Return every review row, in id order. Used by the Notion seed."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM reviews ORDER BY id").fetchall()
        return [_parse_review_row(dict(r)) for r in rows]

    def find_pending_review_for_activity(
        self,
        strava_id: Optional[int] = None,
        session_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Return the most recent Pending review for an activity, or None.

        Pending = ``status IS NULL``. Match priority: ``strava_id`` first
        (the post-activity review is keyed off Strava), then ``session_id``
        as a fallback when the activity wasn't logged via Strava.
        """
        if strava_id is None and session_id is None:
            return None
        with self._conn() as conn:
            row = None
            if strava_id is not None:
                row = conn.execute(
                    "SELECT * FROM reviews WHERE strava_id = ? AND status IS NULL ORDER BY id DESC LIMIT 1",
                    (strava_id,),
                ).fetchone()
            if row is None and session_id is not None:
                row = conn.execute(
                    "SELECT * FROM reviews WHERE session_id = ? AND status IS NULL ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
        return _parse_review_row(dict(row)) if row else None

    def resolve_pending_review(self, review_id: int, status: str) -> Optional[dict]:
        """Flip a Pending reviews row to a terminal status and mirror it.

        ``status`` must be one of approved / rejected / expired / no-op
        (the schema CHECK constraint enforces this). Sets ``resolved_at`` to
        now. Returns the updated row dict (with ``proposed_change`` parsed)
        or ``None`` if the row doesn't exist or was already resolved (the
        UPDATE is conditioned on ``status IS NULL`` so callers can race
        each other harmlessly). Mirrors the flip to Notion best-effort —
        a Notion failure logs and is swallowed; SQLite is the source of
        truth.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE reviews SET status = ?, resolved_at = datetime('now') WHERE id = ? AND status IS NULL",
                (status, review_id),
            )
            if cur.rowcount == 0:
                return None
            row = _parse_review_row(dict(conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()))
        self._notify_mirror_review(row)
        return row

    def expire_old_pending_reviews(self, days: int = 14, today: Optional[date] = None) -> list[dict]:
        """Flip Pending reviews older than ``days`` to ``expired``.

        Pending = ``status IS NULL``. Cutoff is computed against the review's
        ``date`` field (the activity date). Returns the list of rows that
        were just expired (dicts with ``proposed_change`` parsed); the whole
        batch is mirrored to Notion in a single daemon thread (one mirror
        call, N upserts inside) so a sweep over many stale rows doesn't
        spawn N threads.

        ``today`` defaults to the current UTC date — the cron that calls this
        runs on UTC and the schema's ``date`` field is the activity day, also
        recorded against UTC. Passing a local date here would make the cutoff
        drift by up to a day either way.

        Intended to run nightly via cron — wire as a Railway scheduled job
        (e.g. ``0 9 * * *`` UTC, daily at 9 AM) calling
        ``StateManager().expire_old_pending_reviews()``. This module
        deliberately does NOT register the schedule itself; scheduling is
        owned by the deploy config.
        """
        ref = today or datetime.now(timezone.utc).date()
        cutoff = (ref - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            stale = conn.execute(
                "SELECT id FROM reviews WHERE status IS NULL AND date < ?",
                (cutoff,),
            ).fetchall()
            stale_ids = [r["id"] for r in stale]
            if not stale_ids:
                return []
            conn.execute(
                f"UPDATE reviews SET status = 'expired', resolved_at = datetime('now') "
                f"WHERE id IN ({','.join('?' * len(stale_ids))})",
                stale_ids,
            )
            rows = conn.execute(
                f"SELECT * FROM reviews WHERE id IN ({','.join('?' * len(stale_ids))}) ORDER BY id",
                stale_ids,
            ).fetchall()
        parsed = [_parse_review_row(dict(r)) for r in rows]
        self._notify_mirror_reviews(parsed)
        return parsed

    # ---------- Daily health (COROS) ----------

    # Metric columns shared by upsert and reads. `raw` and `fetched_at` are
    # handled separately in the upsert (raw COALESCEs like metrics;
    # fetched_at always takes the new value).
    _HEALTH_COLS = (
        "sleep_score",
        "sleep_duration_min",
        "sleep_nap_min",
        "sleep_deep_min",
        "sleep_light_min",
        "sleep_rem_min",
        "sleep_awake_min",
        "hrv_avg",
        "hrv_baseline",
        "hrv_range_low",
        "hrv_range_high",
        "hrv_evaluation",
        "resting_hr",
        "stress_avg",
        "steps",
        "exercise_min",
        "recovery_pct",
        "recovery_level",
        "load_short_term",
        "load_long_term",
        "load_ratio",
        "load_comment",
    )

    def upsert_daily_health(self, rows: list[dict]) -> None:
        """Insert-or-update one row per date in `daily_health`.

        Per-column COALESCE(excluded, existing) semantics: a re-pull that
        carries NULL for a field (e.g. backfill rows have no recovery
        snapshot; today's resting HR is 'No data' until tomorrow) never
        erases a previously stored value. Fires the best-effort Notion
        mirror after commit.
        """
        if not rows:
            return
        cols = list(self._HEALTH_COLS)
        col_sql = ", ".join(cols)
        placeholders = ", ".join("?" * (len(cols) + 2))  # + date, raw
        updates = ", ".join(f"{c} = COALESCE(excluded.{c}, daily_health.{c})" for c in cols)
        sql = (
            f"INSERT INTO daily_health (date, {col_sql}, raw, fetched_at) "
            f"VALUES ({placeholders}, datetime('now')) "
            f"ON CONFLICT(date) DO UPDATE SET {updates}, "
            "raw = COALESCE(excluded.raw, daily_health.raw), "
            "fetched_at = excluded.fetched_at"
        )
        with self._conn() as conn:
            conn.executemany(
                sql,
                [
                    (
                        row["date"],
                        *(row.get(c) for c in cols),
                        row.get("raw"),
                    )
                    for row in rows
                ],
            )
            conn.commit()
        self._notify_mirror_health(rows)

    def get_daily_health(self, days: int = 7, today: Optional[date] = None) -> list[dict]:
        """Return daily_health rows for [today-days+1, today], ascending by date."""
        ref = today or date.today()
        start = (ref - timedelta(days=days - 1)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_health WHERE date >= ? AND date <= ? ORDER BY date",
                (start, ref.isoformat()),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_load_trend(self, weeks: int = 4, today: Optional[date] = None) -> list[dict]:
        """Per-ISO-week training-load aggregates over the trailing window.

        Returns [{week, start, avg_load_ratio, last_long_term, flagged_days}]
        ascending; weeks with no data are omitted. `flagged_days` counts days
        whose COROS load comment was anything other than 'Optimized'.
        """
        ref = today or date.today()
        rows = self.get_daily_health(days=weeks * 7, today=ref)
        buckets: dict[tuple[int, int], dict] = {}
        for row in rows:
            d = date.fromisoformat(row["date"])
            key = d.isocalendar()[:2]
            b = buckets.setdefault(
                key, {"start": (d - timedelta(days=d.weekday())).isoformat(), "ratios": [], "long_terms": [], "flagged": 0}
            )
            if row.get("load_ratio") is not None:
                b["ratios"].append(row["load_ratio"])
            if row.get("load_long_term") is not None:
                b["long_terms"].append((row["date"], row["load_long_term"]))
            comment = row.get("load_comment")
            if comment and comment.lower() != "optimized":
                b["flagged"] += 1
        out = []
        for (year, week), b in sorted(buckets.items()):
            if not b["ratios"] and not b["long_terms"]:
                continue
            out.append(
                {
                    "week": f"{year}-W{week:02d}",
                    "start": b["start"],
                    "avg_load_ratio": round(sum(b["ratios"]) / len(b["ratios"]), 2) if b["ratios"] else None,
                    "last_long_term": max(b["long_terms"])[1] if b["long_terms"] else None,
                    "flagged_days": b["flagged"],
                }
            )
        return out

    def render_readiness_block(self, days: int = 7, today: Optional[date] = None) -> str:
        """Compact markdown readiness table for the system prompt.

        Returns "" when there's no data so the context blob degrades
        silently on a pre-COROS database.
        """
        rows = self.get_daily_health(days=days, today=today)
        if not rows:
            return ""

        def _v(row: dict, key: str, suffix: str = "") -> str:
            val = row.get(key)
            return f"{val}{suffix}" if val is not None else "—"

        header_bits = []
        # Today's row often lags (no HRV entry yet) — take the baseline from
        # the most recent row that has one.
        baseline_row = next((r for r in reversed(rows) if r.get("hrv_baseline") is not None), None)
        if baseline_row:
            rng = ""
            if baseline_row.get("hrv_range_low") is not None and baseline_row.get("hrv_range_high") is not None:
                rng = f" (normal {baseline_row['hrv_range_low']}–{baseline_row['hrv_range_high']}ms)"
            header_bits.append(f"HRV baseline {baseline_row['hrv_baseline']}ms{rng}")
        recovery_row = next((r for r in reversed(rows) if r.get("recovery_pct") is not None), None)
        if recovery_row:
            header_bits.append(
                f"Recovery {recovery_row['recovery_pct']}% — {recovery_row.get('recovery_level') or '?'}"
                f" (as of {recovery_row['date']})"
            )
        lines = []
        if header_bits:
            lines.append(" | ".join(header_bits))
        lines.append("| Date | Sleep | HRV | RHR | Stress | Load ratio | Load status |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in rows:
            sleep = "—"
            if row.get("sleep_duration_min") is not None:
                h, m = divmod(row["sleep_duration_min"], 60)
                score = f" (score {row['sleep_score']})" if row.get("sleep_score") is not None else ""
                nap = f" +{row['sleep_nap_min']}m nap" if row.get("sleep_nap_min") else ""
                sleep = f"{h}h{m:02d}{score}{nap}"
            hrv = _v(row, "hrv_avg", "ms")
            if row.get("hrv_evaluation") and row.get("hrv_avg") is not None:
                hrv += f" ({row['hrv_evaluation']})"
            lines.append(
                f"| {row['date']} | {sleep} | {hrv} | {_v(row, 'resting_hr')} | "
                f"{_v(row, 'stress_avg')} | {_v(row, 'load_ratio')} | {_v(row, 'load_comment')} |"
            )
        return "\n".join(lines)

    def _render_load_trend_block(self, weeks: int = 4, today: Optional[date] = None) -> str:
        """Weekly chronic-load trend lines for the system prompt; "" if no data."""
        trend = self.get_load_trend(weeks=weeks, today=today)
        if not trend:
            return ""
        lines = []
        for w in trend:
            ratio = w["avg_load_ratio"] if w["avg_load_ratio"] is not None else "—"
            chronic = w["last_long_term"] if w["last_long_term"] is not None else "—"
            flagged = f", {w['flagged_days']} non-optimized day(s)" if w["flagged_days"] else ""
            lines.append(f"- {w['week']} (w/o {w['start']}): avg load ratio {ratio}, chronic load {chronic}{flagged}")
        return "\n".join(lines)

    def _notify_mirror_health(self, rows: list[dict]) -> None:
        """Best-effort batched mirror of daily health rows to Notion."""
        if not rows:
            return
        try:
            from notion.mirror import mirror_health_rows

            mirror_health_rows(rows)
        except Exception:  # noqa: BLE001
            pass

    # ---------- Journal ----------

    def load_journal(self, max_entries: Optional[int] = None) -> str:
        """Return the journal text, optionally truncated to the last N entries."""
        with self._conn() as conn:
            row = conn.execute("SELECT content FROM journal WHERE id = 1").fetchone()
        text = row["content"] if row else ""
        if max_entries is None or not text:
            return text
        sections = text.split("\n---\n")
        if len(sections) <= 1 or max_entries < 0:
            return text
        head, *entries = sections
        kept = entries[-max_entries:] if max_entries > 0 else []
        return "\n---\n".join([head, *kept])

    def append_journal(self, entry: str, when: Optional[datetime] = None) -> None:
        """Append a timestamped entry to the journal.

        Second precision (not just minutes): the entry timestamp is the
        Notion mirror's source_key for the row, and multiple entries in the
        same minute would otherwise collapse into one mirrored page.
        """
        ts = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        body = entry.rstrip()
        block = f"\n---\n\n## {ts}\n\n{body}\n"
        with self._conn() as conn:
            row = conn.execute("SELECT content FROM journal WHERE id = 1").fetchone()
            if row is None or not row["content"]:
                conn.execute(
                    "INSERT INTO journal (id, content, updated_at) VALUES (1, ?, datetime('now')) "
                    "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
                    "updated_at = excluded.updated_at",
                    (_JOURNAL_HEADER + block,),
                )
            else:
                conn.execute(
                    "UPDATE journal SET content = content || ?, updated_at = datetime('now') WHERE id = 1",
                    (block,),
                )
        self._notify_mirror_journal({"title": ts, "date": ts[:10], "body": body})

    # ---------- Gcal sync state ----------

    def load_gcal_sync_state(self) -> dict[str, dict]:
        """Return per-event sync state as ``{event_id: {hash, completed, ...}}``."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_id, hash, last_synced_at, completed, last_completed_at, off_plan FROM gcal_sync_state"
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            entry: dict[str, Any] = {}
            if r["hash"] is not None:
                entry["hash"] = r["hash"]
            if r["last_synced_at"] is not None:
                entry["last_synced_at"] = r["last_synced_at"]
            if r["completed"]:
                entry["completed"] = True
            if r["last_completed_at"] is not None:
                entry["last_completed_at"] = r["last_completed_at"]
            if r["off_plan"]:
                entry["off_plan"] = True
            out[r["event_id"]] = entry
        return out

    def save_gcal_sync_state(self, state: dict[str, dict]) -> None:
        """Replace the gcal sync state wholesale, in one transaction."""
        with self._conn() as conn:
            conn.execute("DELETE FROM gcal_sync_state")
            conn.executemany(
                "INSERT INTO gcal_sync_state "
                "(event_id, hash, last_synced_at, completed, last_completed_at, off_plan) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        event_id,
                        entry.get("hash"),
                        entry.get("last_synced_at"),
                        1 if entry.get("completed") else 0,
                        entry.get("last_completed_at"),
                        1 if entry.get("off_plan") else 0,
                    )
                    for event_id, entry in state.items()
                ],
            )

    # ---------- Composite read for system prompt ----------

    def load_full_context(self, recent_days: int = 21, journal_entries: int = 5) -> str:
        """Format all state into a single markdown blob for the system prompt."""
        athlete_yaml = self._load_athlete_yaml()
        plan_text = self.render_plan()
        recent = self.get_recent_sessions(days=recent_days)
        journal = self.load_journal(max_entries=journal_entries)

        parts = [
            "=== ATHLETE PROFILE ===",
            "```yaml",
            athlete_yaml.rstrip(),
            "```",
            "",
            "=== TRAINING PLAN ===",
            plan_text.rstrip(),
            "",
            f"=== RECENT SESSIONS (last {recent_days} days) ===",
            *(json.dumps(e, ensure_ascii=False) for e in recent),
        ]

        # Wearable readiness + load trend (COROS nightly pull). Both render
        # "" on a pre-COROS database, keeping the blob unchanged until the
        # first pull lands. Daily readiness drives today's-session decisions;
        # the weekly trend gives plan-construction turns the chronic-load
        # arc without re-deriving it from raw rows.
        readiness = self.render_readiness_block(days=7)
        if readiness:
            parts += ["", "=== READINESS (COROS, last 7 days) ===", readiness]
        load_trend = self._render_load_trend_block(weeks=4)
        if load_trend:
            parts += ["", "=== TRAINING LOAD TREND (last 4 weeks) ===", load_trend]

        parts += [
            "",
            f"=== JOURNAL (last {journal_entries} entries) ===",
            journal.rstrip(),
        ]
        return "\n".join(parts)


# ---------- module helpers ----------


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursive dict merge; src overrides dst, lists replace not extend."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _validate_new_slots_against_history(conn: sqlite3.Connection, new_rows: list[dict]) -> None:
    """Raise ValueError if a new (date, slot) collides with non-planned history.

    Once a slot has logged actuals — status in {completed, missed, off-plan} —
    that ordinal is anchored to its historical prescription. The plan parser
    is not allowed to reassign the same ordinal to a different workout: the
    Notion page, GCal event, and changelog all reference the (date, slot)
    key, so silently overwriting would scramble cross-surface state. The
    parser-side equivalent of issue #46 W5: surface a clear error instead.

    NULL-slot rows are exempt (single-session legacy shape; UNIQUE(date,slot)
    treats them as distinct in SQLite anyway).
    """
    by_date: dict[str, set] = {}
    for r in new_rows:
        slot = r.get("slot")
        if slot is None:
            continue
        by_date.setdefault(r["date"], set()).add(slot)
    if not by_date:
        return
    dates = list(by_date.keys())
    placeholders = ",".join("?" * len(dates))
    existing = conn.execute(
        f"SELECT date, slot, status FROM sessions "
        f"WHERE date IN ({placeholders}) AND status != 'planned' AND slot IS NOT NULL",
        tuple(dates),
    ).fetchall()
    for row in existing:
        claimed = by_date.get(row["date"], set())
        if row["slot"] in claimed:
            raise ValueError(
                f"cannot reassign slot {row['slot']!r} on {row['date']}: an existing "
                f"{row['status']} session already owns that ordinal. Edit it via "
                f"update_workout or restructure with the historical slot preserved."
            )


def _is_rest_day(workout: str) -> bool:
    w = workout.strip().lower()
    if not w or w in {"-", "—"}:
        return True
    return any(w.startswith(p) for p in _REST_PATTERNS)


def _empty_workout(target_date: date) -> dict:
    """Default get_todays_workout response when no prescription row exists."""
    return {
        "date": target_date.isoformat(),
        "day_name": target_date.strftime("%A"),
        "workout": "",
        "pace_target": "",
        "notes": "",
        "detail_md": "",
        "status": None,
        "is_rest_day": False,
        "found": False,
        "slot": None,
        "total_slots": 0,
        "slot_label": "",
    }


def _workout_dict_from_row(row: dict, total_slots: int, date_override: Optional[date] = None) -> dict:
    """Project one sessions row into the get_todays_workout view shape."""
    iso = row["date"]
    d = date_override or date.fromisoformat(iso)
    return {
        "date": iso,
        "day_name": d.strftime("%A"),
        "workout": row.get("prescribed_workout") or "",
        "pace_target": row.get("prescribed_pace") or "",
        "notes": row.get("prescribed_notes") or "",
        "detail_md": row.get("detail_md") or "",
        "status": row.get("status"),
        "is_rest_day": _is_rest_day(row.get("prescribed_workout") or ""),
        "found": True,
        "slot": row.get("slot"),
        "total_slots": total_slots,
        "slot_label": slot_display_label(row.get("slot"), total_slots),
    }


# ---------- change-body formatters (for the Plan Changes Notion mirror) ----------
#
# Each writer that calls _append_changelog snapshots the affected row(s)
# before and after its writes and renders a body via these helpers; the
# mirror writes it as the Plan Changes page markdown.


def _format_session_row_short(row: Optional[dict]) -> str:
    """One-line summary of a session row for change-diff bodies."""
    if row is None:
        return "(none)"
    status = row.get("status") or "?"
    rtype = row.get("type") or "?"
    line = f"{row['date']} [{status}/{rtype}] {row.get('prescribed_workout') or ''}".rstrip()
    pace = row.get("prescribed_pace")
    if pace:
        line += f" | pace={pace}"
    notes = row.get("prescribed_notes")
    if notes:
        line += f" | notes={notes}"
    return line


def _format_session_rows(rows: Optional[list]) -> str:
    if not rows:
        return "(no rows)"
    return "\n".join(_format_session_row_short(r) for r in rows)


def _format_actuals(data_value: Any) -> str:
    """Format a completed session's actuals JSON as readable lines."""
    if not data_value:
        return "(no actuals)"
    data: Any = data_value
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return data[:500]
    if not isinstance(data, dict):
        return str(data)[:500]
    bits: list[str] = []
    for k in ("date", "type", "miles", "pace_avg", "hr_avg", "rpe", "notes"):
        v = data.get(k)
        if v is not None:
            bits.append(f"{k}: {v}")
    details = data.get("details") or {}
    for k in ("strava_id", "elevation_gain_ft", "moving_time", "duration"):
        v = details.get(k)
        if v is not None:
            bits.append(f"{k}: {v}")
    return "\n".join(bits) if bits else "(no actuals)"


def _row_dict(row: Any) -> Optional[dict]:
    """sqlite3.Row → dict; None passthrough."""
    return None if row is None else dict(row)


def _parse_review_row(row: dict) -> dict:
    """Parse ``proposed_change`` JSON back to a dict so callers and the
    Notion mirror see structured data, not a string."""
    raw = row.get("proposed_change")
    if isinstance(raw, str) and raw:
        try:
            row["proposed_change"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass  # leave the raw string so debugging is still possible
    return row


def _pick_planned_match(
    activity_type: str,
    planned: list,
    activity_hour: Optional[float] = None,
    total_slots: Optional[int] = None,
) -> Optional[Any]:
    """Choose which planned row a logged activity completes.

    Single planned row → matched regardless of type (legacy behavior). With
    multiple planned rows, prefer the slot whose time bucket is closest to
    ``activity_hour``; type breaks ties (exact match, then run-like bucket).

    Falls back to today's type-only logic when no hour is available
    (e.g. older log entries without ``start_local``).
    """
    if not planned:
        return None
    if len(planned) == 1:
        return planned[0]
    at = (activity_type or "").strip().lower()

    # Bucket-proximity: only when we know the activity's local hour AND the
    # planned rows actually carry slot ordinals (multi-session days do).
    if activity_hour is not None:
        slotted = [r for r in planned if r["slot"] is not None]
        if slotted:
            n = total_slots if total_slots is not None else len(slotted)
            scored = []
            for row in slotted:
                center = slot_bucket_center(row["slot"], n)
                if center is None:
                    continue
                distance = abs(activity_hour - center)
                # type-priority within ties: 0 = exact, 1 = run-like bucket, 2 = mismatch
                row_type = (row["type"] or "").strip().lower()
                if row_type == at:
                    type_rank = 0
                elif (row_type in _RUN_LIKE) == (at in _RUN_LIKE):
                    type_rank = 1
                else:
                    type_rank = 2
                scored.append((distance, type_rank, row))
            if scored:
                scored.sort(key=lambda x: (x[0], x[1]))
                return scored[0][2]

    # Legacy fallback: type-only matching.
    for row in planned:
        if (row["type"] or "").strip().lower() == at:
            return row
    activity_run = at in _RUN_LIKE
    for row in planned:
        if ((row["type"] or "").strip().lower() in _RUN_LIKE) == activity_run:
            return row
    return None
