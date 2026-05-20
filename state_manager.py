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
# table, `plan_meta`); v5 adds the `reviews` table for Phase 1B.4.
# scripts/cutover_to_unified_sessions.py handles v3→v4; v4→v5 is purely
# additive (CREATE TABLE IF NOT EXISTS) and lands the next time _ensure_schema
# re-runs schema.sql against a unified DB.
CURRENT_SCHEMA_VERSION = 5

_JOURNAL_HEADER = "# Journal\n\nAppend-only freeform notes. Newest entries at the bottom.\n"

# Double-checked locking so two threads can't try to apply the schema
# simultaneously on the very first connect.
_schema_lock = threading.Lock()

# Run-shaped workout types — used to match a logged activity to a planned row.
# Covers both planned-row vocab ("long") and logged-session vocab ("long_run").
_RUN_LIKE = {"run", "easy", "workout", "long", "long_run", "race", "strides", "return_test", "tempo"}
_REST_PATTERNS = ("off", "rest", "no run", "no running")


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
        """
        rows = plan_markdown.parse_plan_rows(new_plan_md)
        details = plan_markdown.parse_workout_details(new_plan_md)
        meta = plan_markdown.build_plan_meta(new_plan_md)
        with self._conn() as conn:
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
                    "VALUES (?, NULL, 'planned', ?, ?, ?, ?, ?)",
                    (
                        r["date"],
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

        Updates the planned row for ``target_date`` (only the fields passed
        are touched). If the day has no planned row, a new one is inserted.
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
                # date, else insert a fresh planned row.
                row = conn.execute(
                    "SELECT id, status FROM sessions WHERE date = ? ORDER BY slot LIMIT 1", (iso,)
                ).fetchone()
            before_row = (
                _row_dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (row["id"],)).fetchone())
                if row is not None
                else None
            )
            if row is None:
                cur = conn.execute(
                    "INSERT INTO sessions "
                    "(date, slot, status, type, prescribed_workout, prescribed_pace, "
                    " prescribed_notes, detail_md) "
                    "VALUES (?, NULL, 'planned', ?, ?, ?, ?, ?)",
                    (
                        iso,
                        plan_markdown.infer_workout_type(workout or ""),
                        workout,
                        pace_target,
                        notes,
                        (detail_body or "").strip() or None,
                    ),
                )
                affected_id = cur.lastrowid
            else:
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
        with self._conn() as conn:
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
                    "VALUES (?, NULL, 'planned', ?, ?, ?, ?)",
                    (
                        r["date"],
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
        """Return the prescription row for a date (planned preferred), or None."""
        rows = self._rows(
            "date = ? AND prescribed_workout IS NOT NULL",
            (target.isoformat(),),
        )
        if not rows:
            return None
        planned = [r for r in rows if r["status"] == "planned"]
        return planned[0] if planned else rows[0]

    def get_todays_workout(self, target_date: Optional[date] = None) -> dict:
        """Return the prescribed workout for a date.

        Keys: date, day_name, workout, pace_target, notes, detail_md, status,
        is_rest_day, found. ``found`` is False when no prescription row exists.
        """
        if target_date is None:
            target_date = date.today()
        result = {
            "date": target_date.isoformat(),
            "day_name": target_date.strftime("%A"),
            "workout": "",
            "pace_target": "",
            "notes": "",
            "detail_md": "",
            "status": None,
            "is_rest_day": False,
            "found": False,
        }
        row = self.get_workout_row(target_date)
        if row is None:
            return result
        result.update(
            {
                "workout": row["prescribed_workout"] or "",
                "pace_target": row["prescribed_pace"] or "",
                "notes": row["prescribed_notes"] or "",
                "detail_md": row["detail_md"] or "",
                "status": row["status"],
                "found": True,
                "is_rest_day": _is_rest_day(row["prescribed_workout"] or ""),
            }
        )
        return result

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

    def _session_data(self, where: str, params: tuple) -> list[dict]:
        """Return the ``data`` JSON of logged (completed/off-plan) sessions."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT data FROM sessions WHERE {where} AND data IS NOT NULL ORDER BY date, slot, id",
                params,
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except (json.JSONDecodeError, TypeError):
                continue
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
        with self._conn() as conn:
            planned = conn.execute(
                "SELECT id, type FROM sessions WHERE date = ? AND status = 'planned' ORDER BY slot, id",
                (sdate,),
            ).fetchall()
            match = _pick_planned_match(stype, planned)
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
        and a Notion problem must never break a SQLite write.
        """
        if not rows:
            return
        try:
            from notion.mirror import mirror_sessions

            mirror_sessions(rows)
        except Exception:  # noqa: BLE001 — mirror failures never propagate
            pass

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


def _is_rest_day(workout: str) -> bool:
    w = workout.strip().lower()
    if not w or w in {"-", "—"}:
        return True
    return any(w.startswith(p) for p in _REST_PATTERNS)


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


def _pick_planned_match(activity_type: str, planned: list) -> Optional[Any]:
    """Choose which planned row a logged activity completes.

    A single planned row on the date is claimed regardless of type. With
    multiple, prefer an exact type match, then a same-bucket (run-like vs
    other) match; no match → None (the activity is off-plan).
    """
    if not planned:
        return None
    if len(planned) == 1:
        return planned[0]
    at = (activity_type or "").strip().lower()
    for row in planned:
        if (row["type"] or "").strip().lower() == at:
            return row
    activity_run = at in _RUN_LIKE
    for row in planned:
        if ((row["type"] or "").strip().lower() in _RUN_LIKE) == activity_run:
            return row
    return None
