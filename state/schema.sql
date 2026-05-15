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

-- Migration tracking. StateManager checks MAX(version) on connect; if the DB
-- is behind, it re-runs this file (every statement is IF NOT EXISTS, so the
-- re-run only adds tables introduced since) and records the current version.
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

-- Plan-as-rows (schema v3). DORMANT until the Phase 1A cutover — these tables
-- are created and migrated into, but nothing reads them yet. The old `plan`
-- (blob) and `sessions` (completed-only) tables remain the live source until
-- the cutover PR archives them.
--
-- sessions_v2 unifies planned + completed workouts: one row per workout in
-- some lifecycle state. After the cutover it is renamed to `sessions`.
CREATE TABLE IF NOT EXISTS sessions_v2 (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT    NOT NULL,            -- ISO 'YYYY-MM-DD'
    slot               TEXT,                        -- 'am' | 'pm' | NULL (single)
    status             TEXT    NOT NULL
                       CHECK (status IN ('planned','completed','missed','off-plan')),
    type               TEXT,                        -- easy/workout/long/race/cross/strength/rest/...
    prescribed_workout TEXT,
    prescribed_pace    TEXT,
    prescribed_notes   TEXT,
    detail_md          TEXT,                        -- per-day prose (race-day pacing tables etc.)
    data               TEXT,                        -- JSON actuals: miles, pace, hr, strava details
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at       TEXT,                        -- when status flipped to completed
    UNIQUE (date, slot)                             -- one prescription per date+slot
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_v2_strava_id
    ON sessions_v2 (json_extract(data, '$.details.strava_id'))
    WHERE json_extract(data, '$.details.strava_id') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_v2_date_status ON sessions_v2 (date, status);

-- Singleton: plan prose that does not belong in a checklist (phases, goal,
-- pace zones, adjustment triggers). The non-row remainder of the old plan.md.
CREATE TABLE IF NOT EXISTS plan_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    content    TEXT NOT NULL,
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
