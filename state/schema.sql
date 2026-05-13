-- SQLite schema for the running coach's persistent state.
--
-- One DB file at $DATABASE_PATH (default state/coach.db locally, mounted on a
-- Railway volume in prod). StateManager applies this schema on first connect
-- when schema_version is empty. All statements use IF NOT EXISTS so the script
-- is safe to re-run.
--
-- Document blobs (plan, journal, athlete, plan_changelog) are stored as
-- singleton-row TEXT so the system prompt sees the same bytes as today.
-- sessions.data holds the full JSON entry to preserve variable top-level
-- fields. gcal_sync_state is normalized for SQL-joined reconcile queries.

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- Migration tracking. StateManager checks MAX(version) on connect; if empty,
-- runs this file end-to-end and inserts version 1.
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Append-only session log. Replaces state/log.jsonl.
--
-- `data` holds the full JSON entry so additional top-level fields (notes,
-- weather, pace, etc.) round-trip exactly. `date` and `type` are indexed
-- copies for query speed. The partial UNIQUE index on the JSON-extracted
-- strava_id closes the TOCTOU window in the webhook handler — duplicate
-- inserts raise IntegrityError instead of silently double-logging.
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT    NOT NULL,
    type       TEXT    NOT NULL,
    data       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_strava_id
    ON sessions(json_extract(data, '$.details.strava_id'))
    WHERE json_extract(data, '$.details.strava_id') IS NOT NULL;

-- Singleton document blobs. CHECK (id = 1) enforces the singleton at the
-- schema level; INSERT OR REPLACE on writes keeps it that way.

CREATE TABLE IF NOT EXISTS plan (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plan_changelog (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    content    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- athlete.yaml_text is the canonical form so ruamel can preserve comments,
-- key order, and quotes on round-trip. No JSON mirror — nothing queries
-- inside athlete today; add later if needed.
CREATE TABLE IF NOT EXISTS athlete (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    yaml_text  TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    content    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-event gcal sync state. Replaces state/.gcal_sync_state.json. Each row
-- corresponds to one calendar event. Booleans are INTEGER 0/1 per SQLite
-- convention. Reconcile joins this against `sessions` to detect drift.
CREATE TABLE IF NOT EXISTS gcal_sync_state (
    event_id          TEXT    PRIMARY KEY,
    hash              TEXT,
    last_synced_at    TEXT,
    completed         INTEGER NOT NULL DEFAULT 0,
    last_completed_at TEXT,
    off_plan          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gcal_sync_completed ON gcal_sync_state(completed);
