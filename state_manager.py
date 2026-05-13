"""State management for the running coach.

Persists all bot/agent state in a single SQLite database (default
``state/coach.db`` locally, ``$DATABASE_PATH`` in prod, where Railway's
attached volume keeps writes durable across deploys and restarts).

Tables (see ``state/schema.sql``):
  athlete         — singleton row holding ``yaml_text`` (ruamel-preserved YAML)
  plan            — singleton row holding the current plan markdown
  plan_changelog  — singleton row holding the append-only changelog
  journal         — singleton row holding append-only timestamped notes
  sessions        — one row per logged session; ``data`` JSON preserves the
                    full original entry, partial UNIQUE index on
                    ``details.strava_id`` enforces webhook idempotency
  gcal_sync_state — per-event sync metadata (replaces .gcal_sync_state.json)

Every coach turn calls ``load_full_context()``; the output is byte-equivalent
to the file-backed format so the system prompt is unchanged.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from ruamel.yaml import YAML

# Round-trip YAML — preserves comments, key order, quotes. update_athlete()
# depends on this; PyYAML would silently drop them.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

# Schema lives next to this file so it ships with the deployed image and is
# not shadowed by the Railway volume (which mounts at /app/data, not /app/state
# — but the schema directory is in the repo root regardless).
SCHEMA_PATH = Path(__file__).resolve().parent / "state" / "schema.sql"

_JOURNAL_HEADER = "# Journal\n\nAppend-only freeform notes. Newest entries at the bottom.\n"

# Double-checked locking so two threads can't try to apply the schema
# simultaneously on the very first connect.
_schema_lock = threading.Lock()


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
        # Retained for callers that still want to reference the directory
        # (e.g., scripts writing snapshots alongside the DB).
        self.state_dir = self.db_path.parent
        self._schema_applied = False

    # ---------- Connection plumbing ----------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, ensure schema is applied, commit/rollback + close."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        # busy_timeout is connection-scoped (unlike journal_mode, which is
        # persisted in the DB header by schema.sql). Re-apply on each connect.
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
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
            if row is None:
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            # Always record version 1 (idempotent via PRIMARY KEY).
            conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
            conn.commit()
            self._schema_applied = True

    # ---------- Readers ----------

    def load_athlete(self) -> dict:
        """Parse athlete YAML into a dict. Returns {} if no athlete row."""
        text = self._load_athlete_yaml()
        if not text:
            return {}
        data = _yaml.load(text)
        return data or {}

    def _load_athlete_yaml(self) -> str:
        """Return the raw YAML text for the athlete row (or empty string)."""
        with self._conn() as conn:
            row = conn.execute("SELECT yaml_text FROM athlete WHERE id = 1").fetchone()
        return row["yaml_text"] if row else ""

    def load_plan(self) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT content FROM plan WHERE id = 1").fetchone()
        return row["content"] if row else ""

    def load_journal(self, max_entries: Optional[int] = None) -> str:
        """Return the journal text, optionally truncated to the last N entries.

        Entries are separated by horizontal-rule blocks (``\\n---\\n``); the
        preamble (everything before the first separator) is preserved.
        """
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

    def get_recent_sessions(self, days: int = 14, today: Optional[date] = None) -> list[dict]:
        ref = today or date.today()
        cutoff = ref - timedelta(days=days)
        return self._query_sessions("date >= ? AND date <= ?", (cutoff.isoformat(), ref.isoformat()))

    def get_sessions_in_range(self, start: date, end: date) -> list[dict]:
        return self._query_sessions("date >= ? AND date <= ?", (start.isoformat(), end.isoformat()))

    def sessions_on_date(self, target: date) -> list[dict]:
        return self._query_sessions("date = ?", (target.isoformat(),))

    def _query_sessions(self, where: str, params: tuple) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT data FROM sessions WHERE {where} ORDER BY date, id",
                params,
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except json.JSONDecodeError:
                continue  # mirror old behaviour: skip malformed entries
        return out

    def existing_strava_ids(self) -> set[int]:
        """Return all ``details.strava_id`` values from sessions. Used by the
        Strava backfill + webhook handler for idempotency (the partial UNIQUE
        index now also enforces this at the DB level)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT CAST(json_extract(data, '$.details.strava_id') AS INTEGER) AS sid "
                "FROM sessions WHERE json_extract(data, '$.details.strava_id') IS NOT NULL"
            ).fetchall()
        return {r["sid"] for r in rows if r["sid"] is not None}

    # ---------- Gcal sync state (replaces .gcal_sync_state.json) ----------

    def load_gcal_sync_state(self) -> dict[str, dict]:
        """Return the per-event sync state as ``{event_id: {hash, completed, ...}}``.

        Behavioural equivalence with the old JSON file: callers do
        ``.get("completed")`` / ``.get("hash")`` and don't care about exact dict
        shape, so missing fields are simply absent rather than ``None``.
        """
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
        """Replace the gcal sync state wholesale, matching the old file's
        write-the-whole-dict semantics. Done inside a single transaction so
        readers never see a partial state."""
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
        """Format all state into a single markdown blob for the system prompt.

        Byte-equivalent to the file-backed implementation: same headers, same
        athlete YAML fence, same per-line JSON dump for sessions, same journal
        truncation semantics.
        """
        athlete_yaml = self._load_athlete_yaml()
        plan_text = self.load_plan()
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

    # ---------- Writers ----------

    def append_session(self, session: dict) -> None:
        """Insert a new session row.

        Raises ``sqlite3.IntegrityError`` if ``details.strava_id`` is set and
        a row with that ID already exists. Strava webhook callers should catch
        this and treat it as already-logged; manual ``log_session`` tool calls
        without a strava_id never trigger the partial index.
        """
        if "date" not in session:
            raise ValueError("session entry must include a 'date' field")
        payload = json.dumps(session, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (date, type, data) VALUES (?, ?, ?)",
                (session["date"], session.get("type", ""), payload),
            )

    def update_session_by_strava_id(self, activity_id: int, new_entry: dict) -> bool:
        """Replace the sessions row with matching ``details.strava_id``.

        Returns True if a row was updated; False if no matching row exists
        (caller decides whether to append instead).

        Used by the Strava webhook handler when an ``aspect_type=update`` event
        fires (e.g., the user retags a Run as a Workout after upload).
        """
        if "date" not in new_entry:
            raise ValueError("new_entry must include a 'date' field")
        payload = json.dumps(new_entry, ensure_ascii=False)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sessions "
                "SET data = ?, date = ?, type = ?, updated_at = datetime('now') "
                "WHERE json_extract(data, '$.details.strava_id') = ?",
                (payload, new_entry["date"], new_entry.get("type", ""), activity_id),
            )
            return cur.rowcount > 0

    def update_plan(self, new_plan_md: str, change_note: str) -> None:
        """Replace the plan content and append ``change_note`` to the changelog."""
        ts = datetime.now().isoformat(timespec="seconds")
        changelog_entry = f"- {ts}: {change_note}\n"
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO plan (id, content, updated_at) VALUES (1, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
                (new_plan_md,),
            )
            conn.execute(
                "INSERT INTO plan_changelog (id, content, updated_at) VALUES (1, ?, datetime('now')) "
                "ON CONFLICT(id) DO UPDATE SET "
                "content = plan_changelog.content || excluded.content, "
                "updated_at = excluded.updated_at",
                (changelog_entry,),
            )

    def append_journal(self, entry: str, when: Optional[datetime] = None) -> None:
        """Append a timestamped entry to the journal."""
        ts = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
        block = f"\n---\n\n## {ts}\n\n{entry.rstrip()}\n"
        with self._conn() as conn:
            row = conn.execute("SELECT content FROM journal WHERE id = 1").fetchone()
            if row is None or not row["content"]:
                # First-ever entry: write the header preamble plus this block.
                conn.execute(
                    "INSERT INTO journal (id, content, updated_at) "
                    "VALUES (1, ?, datetime('now')) "
                    "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
                    "updated_at = excluded.updated_at",
                    (_JOURNAL_HEADER + block,),
                )
            else:
                conn.execute(
                    "UPDATE journal SET content = content || ?, updated_at = datetime('now') WHERE id = 1",
                    (block,),
                )

    def update_athlete(self, updates: dict) -> None:
        """Patch fields in athlete YAML. Preserves comments, key order, and
        quotes via ruamel round-trip on the ``yaml_text`` column."""
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

    # ---------- Today's workout ----------

    def get_todays_workout(self, target_date: Optional[date] = None) -> dict:
        """Parse the locked-format 'This Week' table from the plan.

        Locked format: ``| Day | Date | Workout | Pace target | Notes |``
        Date column may be ISO (2026-04-28) or M/D (4/28).

        Returns a dict with keys: date, day_name, workout, pace_target,
        notes, is_rest_day, found.
        """
        if target_date is None:
            target_date = date.today()
        result = {
            "date": target_date.isoformat(),
            "day_name": target_date.strftime("%A"),
            "workout": "",
            "pace_target": "",
            "notes": "",
            "is_rest_day": False,
            "found": False,
        }
        plan_text = self.load_plan()
        if not plan_text:
            return result

        row = _find_workout_row(plan_text, target_date)
        if row is None:
            return result
        result.update(row)
        result["found"] = True
        result["is_rest_day"] = _is_rest_day(result["workout"])
        return result


# ---------- module helpers ----------


def _parse_entry_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursive dict merge; src overrides dst, lists replace not extend."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


_REST_PATTERNS = ("off", "rest", "no run", "no running")


def _is_rest_day(workout: str) -> bool:
    w = workout.strip().lower()
    if not w or w in {"-", "—"}:
        return True
    return any(w.startswith(p) for p in _REST_PATTERNS)


def _find_workout_row(plan_text: str, target_date: date) -> Optional[dict]:
    iso = target_date.isoformat()
    md = f"{target_date.month}/{target_date.day}"
    for line in plan_text.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 5:
            continue
        cell = parts[1]
        if cell == iso or cell == md or iso in cell or md in cell:
            return {
                "day_name": parts[0],
                "date": iso,
                "workout": parts[2],
                "pace_target": parts[3],
                "notes": parts[4],
            }
    return None
