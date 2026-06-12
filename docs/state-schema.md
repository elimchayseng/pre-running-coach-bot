# State schema reference

Canonical reference for the bot's persistent state. Read this before:
- Editing state by hand (via `sqlite3 state/coach.db` or `scripts/apply_state_updates.py`)
- Modifying tools that write state (`tools/state.py`)
- Designing new tools that read state

State lives in a single SQLite database (`$DATABASE_PATH`, default `state/coach.db`). The agent writes via `StateManager`. Schema source of truth: [`state/schema.sql`](../state/schema.sql).

---

## Table index (schema v8)

| Table | Format | Writer | Reader(s) |
|---|---|---|---|
| `athlete` | YAML text in `yaml_text` column (round-trip via ruamel) | `tools.state.update_athlete` | `StateManager.load_athlete`, all tools |
| `sessions` | **Unified plan-as-rows.** One row per workout in a lifecycle state — `planned` / `completed` / `missed` / `off-plan`. `prescribed_workout`, `prescribed_pace`, `prescribed_notes`, `detail_md` carry what the plan asked for; `data` JSON carries the actuals once the workout is logged. `reflection` (v6) holds an athlete-authored post-run note synced back from Notion. `UNIQUE(date, slot)` keeps one prescription per slot; partial UNIQUE index on `details.strava_id` enforces webhook idempotency. | `StateManager.update_plan` / `update_workout` / `replace_week_table` / `reconcile_strava_activity` / `update_session_by_strava_id` | `get_workout_row`, `get_todays_workout`, `get_prescription_rows`, `get_recent_sessions`, `get_sessions_in_range`, `existing_strava_ids`; `google_calendar.sync`; `notion.mirror` |
| `plan_meta` | Singleton row holding plan prose (phases, goal, pace zones, adjustment triggers) — the non-row remainder of the legacy plan.md. | `StateManager.update_plan_meta` (also written by `update_plan`) | `get_plan_meta`, `render_plan` (system prompt) |
| `plan_changelog` | Singleton row holding append-only changelog (one line per write). | `StateManager._append_changelog` (called by every plan writer) | Read by humans / agent on demand; not parsed |
| `journal` | Singleton row holding markdown text; timestamped sections separated by `\n---\n`. | `StateManager.append_journal` | `StateManager.load_journal` (last N entries); `notion.entries.parse_journal_entries` for the mirror |
| `reviews` | One row per LLM review. `kind` (v8) discriminates `activity` (post-activity, FK to `sessions(id)` via `session_id`/`strava_id`) from `readiness` (nightly COROS check-in, no session link). `date`, `critique`, `proposed_change` (JSON), `status` (NULL = Pending; `approved`/`rejected`/`expired`/`no-op` on resolution), `resolved_at`. Partial unique index `idx_reviews_readiness_per_date` enforces one `readiness` row per date. | `StateManager.save_review` (via `strava.review.run_post_activity_review` and `coros.review.run_readiness_review`) | `get_reviews_in_range`, `get_all_reviews`; `notion.mirror.mirror_review` |
| `daily_health` | One row per day (PK = local ISO date) of COROS wearable metrics — sleep stages, HRV vs baseline, resting HR, stress, steps, recovery, training load. Parsed columns plus the `raw` MCP payload as format-change insurance. Upserts COALESCE so a backfill re-pull never erases a previously captured value. | `StateManager.upsert_daily_health` (via `coros.ingest.run_nightly_pull`) | `get_daily_health`, `get_load_trend`, `latest_metric_date`, `has_daily_health_rows`, `render_readiness_block`; `notion.mirror.mirror_health` |
| `gcal_sync_state` | One row per gcal `event_id` with `hash`, `last_synced_at`, `completed`, `last_completed_at`, `off_plan`. | `google_calendar.sync.sync_plan` / `mark_complete` (via `StateManager.save_gcal_sync_state`) | `google_calendar.sync.reconcile_completion`, `get_last_sync_summary` |

Schema source of truth: [`state/schema.sql`](../state/schema.sql). Schema version is tracked in `schema_version`.

**Migrations.** v3 → v4 is the Phase 1A cutover (plan blob → unified `sessions` rows) — runs once via `scripts/cutover_to_unified_sessions.py`, invoked from `gunicorn.conf.py:on_starting` before workers serve traffic. Later migrations are additive and land the next time `_ensure_schema` runs: v4 → v5 adds `reviews` and v7 adds `daily_health` (both `CREATE TABLE IF NOT EXISTS` in `schema.sql`); v6 (`sessions.reflection`) and v8 (`reviews.kind` + the readiness partial unique index) come in as guarded `ALTER TABLE` / `CREATE INDEX` statements in `_ensure_schema` (a `PRAGMA table_info` check, with the `ALTER` wrapped to tolerate a cross-process "duplicate column" race). The legacy `state/*.{md,yaml,jsonl,json}` files in the repo are pre-cutover migration seeds for first-time setup; once the DB is populated they aren't read at runtime.

**Notion mirror.** Every write into `sessions`, `journal`, `plan_changelog`, `reviews`, or `daily_health` fires a daemon-thread upsert into the matching Notion database (when `NOTION_TOKEN` is configured). SQLite stays authoritative; the mirror is one-way and best-effort. See [README.md → Notion mirror](../README.md#notion-mirror) and `notion/mirror.py`.

---

## `athlete.yaml`

YAML document. Round-trip writes preserve comments and key order via ruamel.yaml. The agent patches fields via `update_athlete` (deep-merge; lists REPLACE).

### Top-level keys

| Key | Type | Required? | Notes |
|---|---|---|---|
| `name` | string | yes | Athlete first name. |
| `location` | string | no | Free text. |
| `target_races` | list of race objects (see below) | yes | Ordered by priority. Earliest non-passed entry is "next race" for temporal context. |
| `fall_goal` | object `{status, candidates[], decision_by}` | no | Forward-looking placeholder; informs Phase 2 planning. |
| `prs` | object | no | Distance → time string (`marathon: "2:49"`). |
| `zones` | object | yes | Pace zones — see "zones object" below. |
| `hr_zones` | object | yes | Heart-rate zones — see "hr_zones object" below. |
| `weekly_volume_ceiling_miles` | int | no | Build target. |
| `preferences` | list of strings | yes | Coaching style + behavioral notes. |
| `training_characteristics` | list of strings OR object | yes | Adaptive tendencies. Current schema is loose; agent may add subkeys (e.g. `pacing_discipline`, `unprescribed_volume_pattern`). |
| `cross_training` | object | no | Modality → free-text rules. |
| `strength_training` | object | no | Frequency, focus lifts, race-week protocol. |
| `pt_protocol_achilles` | list of strings | no | Standing PT protocol for Achilles flare-ups. Pattern repeatable for other body parts (`pt_protocol_<area>`). |
| `injury_history` | list of injury objects | yes | Append-only history; status flips to `resolved` when cleared. |
| `race_history` | list of race objects | yes | Post-race; structurally similar to target_races. |

### `target_races[]` race object

| Field | Type | Required? | Notes |
|---|---|---|---|
| `name` | string | yes | Race name. |
| `date` | ISO date `YYYY-MM-DD` | yes | Resolved by `temporal_context.get_race_date()`. |
| `start_time` | `HH:MM` | no | Local time at race location. |
| `timezone` | IANA tz | no | `America/New_York` etc. Used for race-day temporal awareness, not the bot's own clock. |
| `distance` | enum `half_marathon \| marathon \| ultra \| 10k \| 5k \| other` | yes | Coarse classification. |
| `distance_mi` | number | no | Precise mileage. |
| `distance_km` | number | no | Set for ultras when km is the canonical distance. |
| `priority` | enum `A \| B` | yes | A = goal race; B = tune-up / fun race. |
| `terrain` | string | no | `road`, `technical mountain / sky`, etc. |
| `elevation_gain_ft` | string or int | no | Approximation, e.g. `~10000` or `935`. |
| `goal_time` | string | no | Target finish time `H:MM:SS`. `null` if no time goal. |
| `goal_pace` | string | no | Target pace `M:SS` per mile. |
| `goal_context` | string | no | Free text — race plan / strategy notes. |
| `location` | string | no | Free text. |

### `zones` object

Per-effort pace targets. The agent reads these to write prescriptions. Range strings are common (`"6:15-6:25"`).

| Field | Type | Required? | Notes |
|---|---|---|---|
| `marathon_pace` | string `M:SS` | yes | MP. |
| `half_pace_target` | string `M:SS` | no | Set when actively targeting a half PR. |
| `threshold` | string `M:SS` or range | yes | LT effort. |
| `easy` | string range | yes | Easy aerobic pace band. |
| `recovery` | string range | no | Slower than easy. |
| `vo2` | string `M:SS` | no | Rarely populated. |
| `long_run` | string range | no | Long-run-specific easy band. |

### `hr_zones` object

| Field | Type | Notes |
|---|---|---|
| `resting` | string range | E.g. `"44-48"`. |
| `hrv_range` | string range | E.g. `"100-128"`. From wearable. |
| `easy_ceiling` | int | Cap for easy days. Used by translator's `easy` vs `run` classification. |
| `threshold` | string range | LT HR band. |
| `race_max_observed` | int | High-water mark from past races. |

### `injury_history[]` injury object

| Field | Type | Required? |
|---|---|---|
| `date` | string (year, year-month, or ISO date) | yes |
| `description` | string | yes |
| `status` | enum `active \| resolved` | yes |
| `diagnosed` | ISO date | no |
| `resolved` | ISO date | no — set when status flips |
| `pt_protocol_ref` | string (key in athlete.yaml) | no |
| `note` | string | no |

---

## Plan: `sessions` rows + `plan_meta`

Pre-cutover the plan was a single markdown blob; v4 split it into two tables.

### `sessions` (prescription rows)

The bot's prescription for a given day lives in the `sessions` row(s) on that date with a non-null `prescribed_workout`. The agent never re-parses a markdown table — `get_todays_workout(date)` is `SELECT * FROM sessions WHERE date = ? AND prescribed_workout IS NOT NULL ORDER BY slot`. The locked `| Day | Date | Workout | Pace target | Notes |` format still exists for one purpose: `update_plan(markdown, …)` (the escape-hatch tool) parses it via `plan_markdown.parse_plan_rows` to build new `planned` rows. For day-to-day edits, prefer `update_workout` (single row) or `replace_week_table` (a week of rows).

`plan_markdown.parse_plan_rows` requires:
- Pipe-delimited rows with at least 5 cells (Day, Date, Workout, Pace target, Notes)
- Date column matches `YYYY-MM-DD` exactly
- Rows with empty / `—` / `-` Workout are skipped

Per-day rich detail (race-day pacing tables, workout structure, execution cues) lives in `sessions.detail_md` on the same row — the calendar sync uses it verbatim as the event description. The legacy `#### YYYY-MM-DD` anchor inside a markdown plan is parsed into `detail_md` by `plan_markdown.parse_workout_details`.

### `plan_meta` (plan prose)

Singleton row. Contains the prose that doesn't belong in a checklist — phases, goals, pace zones, adjustment triggers, target paces / HR zones, race-week protocols. Loaded into every system prompt by `render_plan()`. Edit via `update_plan_meta(content, change_note)` or wholesale via `update_plan(markdown, change_note)` (which fills both `plan_meta` and `sessions`).

### `plan_changelog`

Singleton; one line per write. `_append_changelog` is called automatically by `update_plan`, `update_plan_meta`, `update_workout`, `replace_week_table`, and the matched-completion branch of `reconcile_strava_activity`. Each writer also captures a before/after snapshot and passes it to the Notion `PRE Plan Changes` mirror as the page body. The blob itself doesn't carry the snapshots — only the timestamps and notes.

---

## Session JSON shape (lives in `sessions.data`)

The JSON shape below is what every logged session carries in the `sessions.data` column. Strava webhooks and manual `log_session` calls produce it via `reconcile_strava_activity`. The shape standardizes top-level queryable fields; type-specific extras live in `details`.

### Required top-level fields

| Field | Type | Notes |
|---|---|---|
| `date` | ISO `YYYY-MM-DD` | Validated by `StateManager.append_session`. |
| `type` | enum (see below) | Session category. |

### Optional top-level fields (always semantically the same when present)

| Field | Type | Notes |
|---|---|---|
| `miles` | number | Distance; null for non-distance entries (strength, yoga). |
| `pace_avg` | string `M:SS` | Average pace per mile. |
| `hr_avg` | int | Average heart rate (bpm). |
| `rpe` | int 1-10 | Rate of perceived exertion. |
| `notes` | string | Free text. Strava entries: name + description joined. |
| `details` | object | Type-specific extras (see below). |

### `type` enum

| Value | Used for |
|---|---|
| `run` | Default running entry not otherwise classified. |
| `easy` | Recovery / aerobic run (HR ≤ easy_ceiling). |
| `long_run` | Strava `workout_type=2`, or otherwise marked. |
| `workout` | Strava `workout_type=3`, or otherwise marked. Intervals, tempo, threshold. |
| `race` | Strava `workout_type=1`, or otherwise marked. |
| `strides` | Short fast efforts (e.g., 4x400m). |
| `return_test` | Post-injury controlled return run. |
| `weekly_summary` | Aggregate week entry (seeded data uses this; new entries shouldn't). |
| `injury_event` | Onset of an injury. |
| `pt_diagnosis` | PT visit / formal diagnosis. |
| `milestone` | Resolution event ("Achilles pain-free"), PR, etc. |
| `cross_train` | Non-running aerobic — cycling, swim, etc. Use `details.modality`. |
| `strength` | Strength session. |

### `details` object — type-specific extras

Common patterns observed:

| Field | When | Notes |
|---|---|---|
| `strava_id` | Strava-sourced entries | Primary dedupe key. ALL Strava entries set this. |
| `sport` | Strava-sourced | Raw Strava sport type (`Run`, `Ride`, `Yoga`, etc.). |
| `workout_type` | Strava-sourced | Raw Strava workout_type (0/1/2/3 or null). |
| `elevation_gain_ft` | Distance entries | Total climb. |
| `moving_time` | Distance entries | Formatted (`1h 2m 5s`). |
| `elapsed_time` | Distance entries | Includes stops. |
| `suffer_score` | Strava-sourced | Strava's effort score. |
| `kudos_count` | Strava-sourced | |
| `gear_id` | Strava-sourced | Shoe ID. |
| `laps` | Strava-sourced workouts | List of lap objects (lap_index, name, distance_mi, pace, hr_avg, hr_max, cadence_avg, elevation_gain_ft). **PRIMARY signal for workout verification.** |
| `splits` | Strava-sourced | Per-mile (preferred) or per-km auto-splits. `unit: "mi"` or `"km"`. |
| `best_efforts` | Strava-sourced runs | Strava's best efforts at standard distances. |
| `pain` | `injury_event` / `return_test` | E.g., `"4/10"`. |
| `result` | `return_test` | `pass / setback / improving`. |
| `modality` | `cross_train` | `cycling`, `swimming`, `yoga`. |
| `prescribed` | Strength + cross_train | bool — was this on the plan? |
| `prescribed_intensity_violated` | Cross-train | bool — flagged unprescribed-volume cases. |
| `weather` | Optional | Object: `{temp_f, conditions}`. |
| `planned` | Race entries | What the goal was: `{goal_time, half_target, …}`. |

### `details.laps[]` lap object (workout verification critical)

| Field | Type | Notes |
|---|---|---|
| `lap_index` | int | 1-indexed lap number from Strava. |
| `name` | string | E.g. `"WU"`, `"Rep 1"`, `"Recovery"`, `"CD"`. From watch. |
| `distance_mi` | number | |
| `moving_time` | string | Formatted. |
| `elapsed_time` | string | Formatted. |
| `pace` | string `M:SS` | |
| `hr_avg` | int | |
| `hr_max` | int | |
| `cadence_avg` | int | |
| `elevation_gain_ft` | int | |

### `details.splits[]` split object

| Field | Type |
|---|---|
| `split` | int (1-indexed) |
| `distance_mi` | number |
| `elapsed_time` | string |
| `pace` | string `M:SS` |
| `hr_avg` | int |
| `elevation_diff_ft` | int (signed) |
| `unit` | enum `"mi" \| "km"` |

---

## `journal` (singleton blob)

Append-only, timestamped. Structure:

```markdown
# Journal

Append-only freeform notes. Newest entries at the bottom.

---

## 2026-05-08 14:23

<body text — paragraphs, no internal section headers>

---

## 2026-05-09 09:15

<next entry body>
```

- Header line is exactly `## YYYY-MM-DD HH:MM:SS` (24-hour, local timezone). Second precision is load-bearing — it's the Notion mirror's source_key, and two entries with the same `## ` header would collapse onto one Notion page.
- Entries separated by `\n---\n`.
- Body is free text. The agent passes body only (no date/header in the text it submits) — `append_journal` prepends the timestamp.
- `StateManager.load_journal(max_entries=N)` returns the last N entries (preamble + last N sections).
- The Notion mirror parses this blob into per-entry dicts via `notion.entries.parse_journal_entries` and upserts a Notion page per entry, keyed on `jid:{title}`.

---

## `daily_health` (COROS wearable metrics)

One row per day, primary-keyed by local ISO date (`USER_TIMEZONE`). Written by `StateManager.upsert_daily_health`, fed by `coros.ingest.run_nightly_pull` → `coros.translator.merge_daily_rows`. The upsert is per-column COALESCE: a re-pull of the same date never erases a value captured on the original night (e.g. the point-in-time recovery snapshot, which `queryRecoveryStatus` only reports for "now").

Every parsed column is nullable. A COROS output-format change degrades to NULL columns rather than an exception — and because the `raw` payload is always stored, a fully unparsed pull still lands a row for recovery. Freshness checks therefore count only rows with at least one non-NULL **parsed** metric (`latest_metric_date`), never mere row existence, so a raw-only "insurance" row can't mask a silently broken pull.

| Column | Type | Source tool | Notes |
|---|---|---|---|
| `date` | TEXT (PK) | — | Local ISO date. |
| `sleep_score` | INTEGER | querySleepData / queryDailyHealthData | Dedicated sleep tool wins on conflict. |
| `sleep_duration_min` | INTEGER | querySleepData | Main sleep, **excludes** naps. |
| `sleep_nap_min` | INTEGER | querySleepData | |
| `sleep_deep_min` / `sleep_light_min` / `sleep_rem_min` / `sleep_awake_min` | INTEGER | queryDailyHealthData | Minute-granular stages. |
| `hrv_avg` | INTEGER | queryHrvAssessment | Lags a day — no entry for "today". |
| `hrv_baseline` / `hrv_range_low` / `hrv_range_high` | INTEGER | queryHrvAssessment | Rolling baseline + normal band. |
| `hrv_evaluation` | TEXT | queryHrvAssessment | e.g. `Above normal` / `Normal`. |
| `resting_hr` | INTEGER | queryRestingHeartRate | `No data` until tomorrow for the current day. |
| `stress_avg` | INTEGER | queryStressLevel | Per-bucket breakdown is currently all `No data`. |
| `steps` | INTEGER | queryDailyHealthData | |
| `exercise_min` | INTEGER | queryDailyHealthData | |
| `recovery_pct` | INTEGER | queryRecoveryStatus | Point-in-time only; today's row only. |
| `recovery_level` | TEXT | queryRecoveryStatus | e.g. `Heavy training allowed`. |
| `load_short_term` / `load_long_term` / `load_ratio` | REAL | queryTrainingLoadAssessment | |
| `load_comment` | TEXT | queryTrainingLoadAssessment | e.g. `Optimized` / `Excessive`. |
| `raw` | TEXT | — | JSON of the full tool bundle; today's row only. Format-change insurance. |
| `fetched_at` | TEXT | — | `datetime('now')` at write. |

**Readiness in the system prompt.** `render_readiness_block(days=7)` renders a sleep / HRV / RHR / stress / load-ratio table and `_render_load_trend_block(weeks=4)` aggregates `get_load_trend` into a per-ISO-week training-load arc. Both are injected into every chat turn by `load_full_context` as `=== READINESS (COROS, last 7 days) ===` / `=== TRAINING LOAD TREND (last 4 weeks) ===`. Free-text columns (`load_comment`, `hrv_evaluation`, `recovery_level`) are charset/length-clamped before they reach the prompt — they're third-party-controlled text crossing into an LLM trust boundary.

**Date safety.** Parsed dates are validated against the real calendar (`coros.translator._real_iso`) before becoming primary keys — a digit-shaped but impossible date like `2026-06-32` is dropped rather than poisoning the table (a bad PK would crash `date.fromisoformat` in `get_load_trend` on every chat turn).

---


## Schema evolution guidelines

When the agent (or a human) wants to add fields:

1. **Add to optional first.** Required fields are validated at parse time and may break readers.
2. **Use `details.*` for type-specific richness** rather than top-level fields — keeps the queryable surface small.
3. **The locked plan-table format is still load-bearing for `update_plan`** — its `plan_markdown.parse_plan_rows` is how the escape-hatch tool turns full-markdown proposals (e.g. from `strava.review.run_post_activity_review`) into `sessions` rows. Changing the format means updating `plan_markdown.py` and every tool description that mentions it.
4. **Lists in `athlete.yaml` REPLACE on merge** (per `update_athlete` semantics). To add to a list, include the full new list in the tool call.
5. **`sessions.status` is checked at the DB level** — see `state/schema.sql`. To add a new lifecycle state (e.g. `deferred`), update the CHECK constraint *and* the Notion `Status` select options in `notion/schema.py:SESSIONS_PROPERTIES`.

When state shapes drift in production:
- `python scripts/state_dump.py log` to inspect recent sessions (or `--all` for everything).
- `sqlite3 state/coach.db 'SELECT id, date, type, json_valid(data) FROM sessions WHERE NOT json_valid(data)'` to find any malformed JSON in `sessions.data` (should always be 0 — the writers go through `json.dumps`).
- `python -c "from state_manager import StateManager; print(StateManager().load_athlete())"` to verify athlete YAML parses.
- `python scripts/strava_setup.py status` for the live Strava token + API health.
- `python scripts/coros_setup.py status` for the live COROS token + a `queryUserInfo` round-trip; `python scripts/coros_setup.py pull --dry-run` to fetch + parse a bundle without writing.
