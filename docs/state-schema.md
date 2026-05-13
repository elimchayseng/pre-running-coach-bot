# State schema reference

Canonical reference for the bot's persistent state. Read this before:
- Editing state by hand (via `sqlite3 state/coach.db` or `scripts/apply_state_updates.py`)
- Modifying tools that write state (`tools/state.py`)
- Designing new tools that read state

State lives in a single SQLite database (`$DATABASE_PATH`, default `state/coach.db`). The agent writes via `StateManager`. Schema source of truth: [`state/schema.sql`](../state/schema.sql).

---

## Table index

| Table | Replaces (legacy file) | Format | Writer | Reader(s) |
|---|---|---|---|---|
| `athlete` | `athlete.yaml` | YAML text in `yaml_text` column (round-trip via ruamel) | `tools.state.update_athlete` | `StateManager.load_athlete`, all tools |
| `plan` | `plan.md` | Markdown text in `content` column (freeform + locked weekly table) | `tools.state.update_plan` | `StateManager.load_plan`, `get_todays_workout` parser |
| `sessions` | `log.jsonl` | One row per session; full original JSON entry in `data` column. `date` + `type` indexed; partial UNIQUE index on `details.strava_id`. | `StateManager.append_session` (via `tools.state.log_session`, `strava.handler`, `strava_backfill`) | `StateManager.get_recent_sessions`, `get_sessions_in_range`, `existing_strava_ids`; `tools.fitness.get_fitness_summary` |
| `journal` | `journal.md` | Markdown text in `content` column, timestamped sections separated by `\n---\n` | `StateManager.append_journal` (via `tools.state.append_journal`) | `StateManager.load_journal` (last N entries) |
| `plan_changelog` | `plan_changelog.md` | Markdown text in `content` column, one line per `update_plan` call | `StateManager.update_plan` (automatic) | Read by humans / agent on demand; not parsed |
| `gcal_sync_state` | `.gcal_sync_state.json` | Normalized: one row per gcal `event_id` with `hash`, `last_synced_at`, `completed`, `last_completed_at`, `off_plan` | `google_calendar.sync.sync_plan` / `mark_complete` (via `StateManager.save_gcal_sync_state`) | `google_calendar.sync.reconcile_completion`, `get_last_sync_summary` |

Note the legacy `state/*.{md,yaml,jsonl,json}` files are kept committed as **migration seeds only** — `scripts/migrate_state_to_sqlite.py` reads them once to populate `coach.db`. They are not the runtime source of truth.

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

## `plan.md`

Freeform markdown. Two sections are special:

### Locked "This Week" table (CRITICAL — `get_todays_workout` parses this)

Heading flexible; the table itself must match this format exactly:

```markdown
| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Fri | 2026-05-08 | Rest + gentle yoga PM 30-40min | — | Hip/hamstring focus |
| Sat | 2026-05-09 | Easy 8mi STRICT | 8:30-9:00, HR ≤155 | Cut from 9-10mi… |
```

Parser (`state_manager.py:_find_workout_row`) requires:
- Pipe-delimited rows with 5 cells: Day, Date, Workout, Pace target, Notes
- Date column matches `YYYY-MM-DD` (preferred) OR `M/D`
- Empty cells use `—` or empty string; never delete the cell separator

Other weeks in the doc are freeform — the parser only looks for today's row in any pipe-delimited block.

### "Recent Plan Adjustments" section

By convention, every `update_plan` call should append a dated line. Format:

```markdown
- 2026-05-08: <one-line reason for the change>
```

Not strictly enforced; the `plan_changelog.md` file is the immutable backup.

### Sections by convention (not enforced)

- `## Active Goals` — bulleted summary of target races
- `## Phase N — <name>` — per-phase narrative
- `## Adjustment Triggers (How the Coach Adapts)` — rule set the agent applies when adapting the plan. Currently lives in plan.md so it's loaded into every system prompt.
- `## Reference` — paces, HR zones, race-week strength protocol, fixed calendar conflicts

---

## `log.jsonl`

Append-only. One JSON object per line. The shape standardizes top-level queryable fields; type-specific extras live in `details`.

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

## `journal.md`

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

- Header line is exactly `## YYYY-MM-DD HH:MM` (24-hour, local timezone).
- Entries separated by `\n---\n`.
- Body is free text. The agent passes body only (no date/header in the text it submits) — `append_journal` prepends the timestamp.
- `StateManager.load_journal(max_entries=N)` returns the last N entries (preamble + last N sections).

---

## `plan_changelog.md`

Append-only audit log. One line per `update_plan` call. Format:

```markdown
- 2026-05-08T20:42:13: <change_reason from the tool call>
```

Automatic — written by `StateManager.update_plan`. The agent doesn't touch this file directly.

---

## Schema evolution guidelines

When the agent (or a human) wants to add fields:

1. **Add to optional first.** Required fields are validated at parse time and may break readers.
2. **Use `details.*` for type-specific richness** rather than top-level fields — keeps the queryable surface small.
3. **Never change the locked plan-table format** without updating both `state_manager.py:_find_workout_row` and the tool description in `tools/state.py`.
4. **Lists in `athlete.yaml` REPLACE on merge** (per `update_athlete` semantics). To add to a list, include the full new list in the tool call.

When state shapes drift in production:
- `python scripts/state_dump.py log` to inspect recent sessions (or `--all` for everything).
- `sqlite3 state/coach.db 'SELECT id, date, type, json_valid(data) FROM sessions WHERE NOT json_valid(data)'` to find any malformed JSON in `sessions.data` (should always be 0 — the writers go through `json.dumps`).
- `python -c "from state_manager import StateManager; print(StateManager().load_athlete())"` to verify athlete YAML parses.
- `python scripts/strava_setup.py status` for the live token + API health.
