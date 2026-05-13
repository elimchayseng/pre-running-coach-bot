# State + Log Persistence — Followup Plan

> **Resolved by `feat/sqlite-state-persistence`.** All state moved to SQLite at `$DATABASE_PATH` (Railway volume in prod); `reconcile_completion` lives in `google_calendar/sync.py` and piggybacks on `sync_plan`. Schema: [`state/schema.sql`](../state/schema.sql). Deploy steps: README "Deployment". This doc is kept as historical context.

Handoff doc for the next working session. Captures the durability + reconciliation problems surfaced during QA of the workout-completion feature (PR #10, merged).

## What just shipped

PR #10 added `mark_complete(state, log_date)` which mirrors each logged session onto Google Calendar — ✅ summary prefix, graphite color, aggregated actuals block. It's hooked into `strava/handler.py` and `tools/state.py:_log_session` so every `append_session` call also fires `mark_complete`. Conversation history mirroring was added to `strava/notify.py` so Strava-side Telegram messages land in Redis chat history.

mark_complete is **event-driven only**. It runs at the moment a session is appended to `log.jsonl`. There is no periodic reconciliation.

## The problem

Two intertwined issues surfaced during local QA:

### 1. Log durability on Railway is broken

- `Procfile: web: gunicorn app:app` → bot runs on Railway.
- `state/` was committed at `ab874db` ("Track state/ in-repo for now (deploy parity with local)"), so the deployed image ships with whatever was in git at deploy time.
- Railway's filesystem is **ephemeral**. Per the README: `STRAVA_TOKENS_BACKEND=redis` exists specifically because of this. Same constraint applies to `log.jsonl`, `plan.md`, `athlete.yaml`, `journal.md`.
- When Strava webhooks fire on prod, the dyno appends to `/app/state/log.jsonl`. On the next deploy/restart, that write is lost.
- Local `state/log.jsonl` is just a snapshot of whatever was in git. It doesn't reflect prod webhook activity unless something pulls it back down.

**Concrete symptom we hit during QA**: PRE confirmed Strava uploads for 5/11 and 5/12 via Telegram, but local `state/log.jsonl` had no entries past 5/7. The 5/11 entry was processed in prod, then lost. The 5/12 entry suffered the same fate or hasn't been re-deployed yet (mark_complete code wasn't on prod when the 5/12 webhook fired).

### 2. No reconciliation path

Even if log durability is fixed, there's no way to recover when `mark_complete` doesn't fire:

- Old webhooks processed before this feature shipped — `log.jsonl` entries exist, gcal not marked.
- A `mark_complete` call fails (network blip on the gcal patch); nothing retries.
- Manual log edits via `scripts/apply_state_updates.py` don't trigger `mark_complete`.
- State files pulled down from prod to local don't trigger anything.

User's mental model (from the QA conversation): *"if what's prescribed = what is found in logs = true, the event should be updated."* That's a reconciliation pass — currently absent.

## Two problems to solve

### A. State durability (broader than just log.jsonl)

Decide where each mutable state file actually lives:

| File | Mutation rate | Current location | Durable? |
|---|---|---|---|
| `log.jsonl` | High (every Strava activity, every chat log) | `state/` in git, ephemeral on dyno | ❌ |
| `plan.md` | Medium (plan edits, post-activity proposals) | `state/` in git, ephemeral on dyno | ❌ |
| `athlete.yaml` | Low (PRs, zones, injuries) | `state/` in git, ephemeral on dyno | ❌ |
| `journal.md` | Low (sporadic notes) | `state/` in git, ephemeral on dyno | ❌ |
| `.gcal_sync_state.json` | Per-sync | `state/` in git, ephemeral on dyno | ❌ |
| `pending_proposal_store` | Per-activity | Redis (24h TTL) | ✅ |
| `conversation_store` | Per-chat-turn | Redis (2h TTL) | ✅ |

#### Durability options

1. **Move log.jsonl (and ideally all of `state/`) to Redis.** Same pattern as `STRAVA_TOKENS_BACKEND=redis`. Writes survive dyno restarts. `StateManager` would gain a backend abstraction with file + redis implementations selected by env var.
   - Pros: Minimal new infra; Redis already required.
   - Cons: Redis isn't great for long append-only logs (memory cost; no querying). Backup/inspection harder than `cat log.jsonl`. The "edit state via PR" workflow that `apply_state_updates.py` enables breaks.

2. **Auto-commit `state/` from the dyno back to GitHub.** After each write, push to a `state-snapshot` branch. Local can pull anytime to see prod state.
   - Pros: Keeps the "state as files in git" model; visible diff history.
   - Cons: Deploy token on Railway; rate-limit risk with frequent commits; merge complexity if two writes race; commits as a side effect of HTTP requests feels wrong.

3. **Managed storage** (S3, R2, Supabase, Postgres, etc.).
   - Pros: Right tool for the job for append-only structured data.
   - Cons: New dependency; another secret to manage; refactor of `StateManager`.

4. **Railway persistent volume.** Railway does support volumes — verify whether the current deploy uses one. If not, attaching one is the smallest possible change.
   - Pros: Smallest diff; preserves current code path.
   - Cons: Single-region persistence; backup story not great; can't read state without shelling into the dyno.

**My recommendation**: Verify Railway volume support first (cheapest fix). If volumes are off the table or insufficient, go with Redis for log.jsonl + .gcal_sync_state.json (write-heavy) and either commit-from-dyno or keep `plan.md`/`athlete.yaml`/`journal.md` in git (the workflow already includes manual sync via `apply_state_updates.py`).

### B. Reconciliation

Design a `reconcile_completion(state, days_back=14)` that walks recent plan rows, joins against `log.jsonl`, and ensures gcal events reflect the join. Reuse `_prescription_kind` / `_log_matches_prescription` / `mark_complete` partition logic from `google_calendar/sync.py`.

#### Trigger options (user preferred: piggyback on sync_plan)

1. **Piggyback on `sync_plan`** (preferred): every time the plan is synced to gcal, reconcile completion for the last N days. Cheap; fires on plan edits and any other `sync_plan` invocation.
2. **Daily scheduled run** (e.g., 11:55pm local): Railway scheduler / GitHub Actions cron. Catches missed events end-of-day.
3. **Manual `/reconcile` slash command**: useful regardless of automated triggers — QA, post-edit recovery.

`sync_plan` piggyback alone may be enough; defer the cron until we see a need.

#### Reconcile semantics — edge cases worth deciding up front

- **Logged a 6mi run when prescription said 5mi** → currently counts as matching (any run on a run day). Confirm that's desired.
- **Logged a workout but plan called for easy** → currently matches (both are run types). Confirm.
- **Two log entries on the same day, one matching + one off-plan** → both events updated; aggregation handles it.
- **No matching log but a previously-completed gcal event exists** → reconcile should NOT "uncomplete" the gcal event by default (someone may have deleted the log accidentally). Surface a warning instead.
- **Past-week reconcile finds a never-marked complete day** → mark it. This is the main self-healing case.
- **Reconcile rate-limits gcal**: if walking 14 days × 2 events/day × bursty patches risks 429s, batch the writes or rely on the existing tenacity retries in `google_calendar/client.py`.

## Files the next session will touch

Pointers so the next conversation can start cold:

- `state_manager.py` — `StateManager` class. Would need a backend abstraction if we move log.jsonl off the filesystem. Already has `sessions_on_date`, `existing_strava_ids`, `_load_log_entries`.
- `google_calendar/sync.py` — `mark_complete`, `_prescription_kind`, `_log_matches_prescription`, `sync_plan`, `_load_sync_state` / `_write_sync_state`. The reconcile function lives here.
- `strava/handler.py` — `_handle_create` / `_handle_update` already call `_mark_calendar_complete`. Reconcile would be a separate entry point.
- `tools/state.py` — `_log_session` hooks into mark_complete. Maybe add a `reconcile_completion` tool too.
- `bot.py` — slash commands. Add `/reconcile` here if we go that route.
- `config.py` + `.env.example` — new env vars for backend selection.
- `Procfile` — possibly a `worker:` line if we add a scheduled job.
- `scripts/apply_state_updates.py` — the existing "sync claude.ai conversation → local state" workflow. Should keep working regardless of backend.
- `docs/sync_prompt.md`, `docs/state-schema.md` — existing schema docs to keep in sync.
- `README.md` — env var table, "How it works" section both mention state files explicitly.

## Open questions to clarify

1. **Does Railway have a volume attached today?** If yes, the durability problem may already be partially solved and we just need reconcile.
2. **What's the deploy cadence?** If we deploy daily, ephemeral writes are unrecoverable. If we deploy weekly, the prod log may have accumulated entries we want to pull down.
3. **Is `apply_state_updates.py` still the canonical sync workflow?** If yes, the durability solution needs to preserve it. If we're moving away from "edit state via PR," that's a bigger discussion.
4. **Acceptable reconcile cadence?** Sync_plan-piggyback only, or also daily cron?

## Known limitations from the workout-completion work (carry-over)

These are documented in PR #10 but worth restating for context:

- If `log.jsonl` loses entries (manual edit, ephemeral wipe, revert), `mark_complete` returns noop without reconciling stale gcal state. → Reconcile fixes this *if* the log is durable.
- `pre_completed="1"` sentinel + local `completed: true` in `.gcal_sync_state.json` jointly protect completion state. If `.gcal_sync_state.json` is wiped (e.g., redeploy), the remote sentinel still prevents `sync_plan` from rolling back via the prune-skip in `sync_plan`. Confirm during the durability work.
- The mark_complete partition decision is logged at INFO (`mark_complete %s: %d matching, %d off_plan (prescription_kind=%s)`). Useful for prod debugging.

## Sketch of what "done" looks like

- `log.jsonl` (at minimum) survives Railway dyno restarts.
- `reconcile_completion(state, days_back=14)` exists and fires from `sync_plan`.
- A single command (slash or CLI) lets me reconcile on demand during QA.
- The `apply_state_updates.py` workflow still applies cleanly, or has a documented replacement.
- README env-var section reflects the new backend.
