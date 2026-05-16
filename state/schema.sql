-- SQLite schema for the running coach's persistent state.
--
-- One DB file at $DATABASE_PATH (default state/coach.db locally, mounted on a
-- Railway volume in prod). StateManager applies this schema on first connect
-- when schema_version is empty. All statements use IF NOT EXISTS so the script
-- is safe to re-run.
--
-- Schema v4 (Phase 1A cutover): `sessions` is the unified plan-as-rows table —
-- one row per workout in a lifecycle state (planned → completed/missed/
-- off-plan). The old plan markdown blob is gone; non-checklist plan prose
-- lives in the `plan_meta` singleton. An existing pre-cutover DB is migrated
-- by scripts/cutover_to_unified_sessions.py (run as a release step); a fresh
-- DB gets this final shape directly.

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;

-- Migration tracking. StateManager records the version it observes on connect;
-- the cutover script records v4 once an existing DB has been migrated.
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Unified plan-as-rows table. Each row is one workout in a lifecycle state.
--
-- `prescribed_*` hold what the plan asked for; `data` holds the JSON actuals
-- (miles, paces, HR, Strava details) once the workout is logged. `detail_md`
-- is per-day coaching prose (race-day pacing tables etc.) synced verbatim into
-- the Google Calendar event. The partial UNIQUE index on the JSON-extracted
-- strava_id closes the webhook-handler TOCTOU window. UNIQUE(date, slot) keeps
-- one prescription per date+slot (NULL slot = the day's single session).
CREATE TABLE IF NOT EXISTS sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    date               TEXT    NOT NULL,
    slot               TEXT,
    status             TEXT    NOT NULL
                       CHECK (status IN ('planned','completed','missed','off-plan')),
    type               TEXT,
    prescribed_workout TEXT,
    prescribed_pace    TEXT,
    prescribed_notes   TEXT,
    detail_md          TEXT,
    data               TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at       TEXT,
    UNIQUE (date, slot)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_strava_id
    ON sessions (json_extract(data, '$.details.strava_id'))
    WHERE json_extract(data, '$.details.strava_id') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_date_status ON sessions (date, status);

-- Singleton: plan prose that does not belong in a checklist — phases, goal,
-- pace zones, adjustment triggers. The non-row remainder of the old plan.md.
CREATE TABLE IF NOT EXISTS plan_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    content    TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Singleton document blobs. CHECK (id = 1) enforces the singleton at the
-- schema level; INSERT OR REPLACE on writes keeps it that way.

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
