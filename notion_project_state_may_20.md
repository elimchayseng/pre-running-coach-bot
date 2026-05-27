# PRE × Notion — Project State (2026-05-20)

Snapshot for picking the work up cold. Pair with `notion-integration-prd.md`
(the original plan) and the merged PRs listed below.

## Status in one line

**Phase 1 complete.** SQLite stays source of truth; every write reflects into
four Notion databases within ~5s via a fire-and-forget daemon-thread mirror.
Three contained follow-ups are filed as issues (#26 / #33 / #34); broader
Phase 2/3/4 work is mapped but not started. An **initial Phase 2 plan** is now
documented below ("Phase 2 — initial plan (not started)") with the
requirements decisions still open — implementation has not begun.

## What shipped

| Phase | Scope | PR | Merged |
|---|---|---|---|
| 1A.1 | Dormant `sessions_v2` + `plan_meta` schema + migration | #24 | ✅ |
| 1A.2 | Cutover — unified `sessions` table goes live | #25 | ✅ |
| 1B.1 | Notion `client.py` + 4 DBs bootstrapped under `PRE Training` | #27 | ✅ |
| 1B.2 | Sessions mirror (rows → PRE Sessions) | #28 | ✅ |
| 1B.3 | Journal + Plan Changes mirrors | #29 | ✅ |
| 1B.3b | Plan-change page bodies (before / after diffs) | #30 | ✅ |
| 1B.4 | Reviews mirror — schema v5 adds the `reviews` table | #32 | ✅ |
| **2** | **Reflection bidirectional sync via Notion Worker — schema v6 adds `sessions.reflection`; new `notion_worker/` TypeScript Worker + `PUT /sessions/<id>/reflection` bridge endpoint** | — | ✅ |

Tests: 440 passing. Lint + format clean. CI green on every PR.

## What the runtime actually does now

A Telegram update or a Strava webhook lands → bot writes to SQLite under
`_chat_lock` → every session-writing method fires a daemon thread that upserts
into Notion. None of the mirror calls are on the request path; a Notion
outage logs a warning and is dropped.

**Four mirror DBs under the `PRE Training` Notion parent page:**

- **PRE Sessions** — `sid:{sessions.id}`. One row per workout. Body = coaching
  detail + notes/laps/splits once logged.
- **PRE Journal** — `jid:{title}` where title is the `## ` header. Body = the
  entry text. `append_journal` now timestamps at second precision so two rapid
  entries can't collide on the same source_key.
- **PRE Plan Changes** — `cid:{timestamp}`. Body = `## Before` / `## After`
  fenced markdown of the affected row(s) — for completed sessions, headings
  flip to `## Prescribed` / `## Actuals`.
- **PRE Reviews** — `rid:{reviews.id}`. Body = `## Critique` + `## Proposed
  change` (summary as quote, `new_plan_md` fenced, italic reason).
  `Session` relation linked by looking up `sid:{session_id}` in Sessions.

**Schema v5** in SQLite:

- `sessions` (unified plan-as-rows; the v3→v4 cutover)
- `plan_meta`, `plan_changelog`, `journal` (singletons — see "open
  refactors" below)
- `athlete`, `gcal_sync_state`
- `reviews` (new in v5; FK to sessions.id)

**Schema migration on prod:** `gunicorn.conf.py:on_starting` runs the cutover
script before workers serve traffic. v4→v5 is purely additive (`CREATE TABLE
IF NOT EXISTS`) and lands the next time `_ensure_schema` re-runs schema.sql on
a unified DB.

## Open issues (filed; ready to pick up)

| # | Title | Why it matters |
|---|---|---|
| **#26** | Auto-sync plan edits to Google Calendar | Plan edits update SQLite + Notion but the user's GCal event stays stale until something explicitly calls `sync_plan_to_calendar`. Small contained fix — debounced hook in the plan-edit tools. |
| **#33** | Auto-resolve reviews when a proposal is applied or rejected | Reviews land as Pending and never flip. Either a heuristic in `update_plan` matches the Redis pending proposal back to a `reviews.id`, or a new `resolve_review(review_id, status)` tool. |
| **#34** | Custom Notion views (calendar / board / smart filters) | Deferred from 1B.1 because `/v1/views` `board.group_by` and `calendar.date_property_id` payloads were uncertain. Now that all four DBs are populated with real data, easy to live-iterate. |

## Deferred items not (yet) ticketed

- **`Triggered by` relation on Plan Changes.** The changelog blob doesn't track
  which session caused each change. Needs every `_append_changelog` caller to
  plumb a session id.
- **Sleep / Stress / Tags parsing on Journal entries.** Properties are emitted
  empty; would require body-content parsing. Speculative; the runner can fill
  them in Notion if they want.
- **Backfilling Plan Changes bodies historically.** Only writes from 1B.3b
  onward have bodies; older pages stay bodyless (the changelog blob never
  recorded the before/after data).
- **Row-ifying `journal` and `plan_changelog`.** Both are SQLite singleton
  blobs that `notion/entries.py` parses into entries. Works fine, but a real
  schema migration would let `jid` / `cid` be true row ids and remove the
  parsers.
- **Operational gaps surfaced in 1B.2's adversarial review:** mirror writes
  swallow auth/rate-limit errors into a warning log with no metric. Health
  check (`users.me`) catches a fully-broken token, but nothing surfaces a
  partial 429 storm.

## Phase 2 — shipped (2026-05-26)

The reflection bidirectional sync is live. The Worker design (formerly
"Design B" in the section below) won over the Flask `/notion/webhook`
design. Durable architecture record:
[`docs/notion-workers-architecture.md`](docs/notion-workers-architecture.md).

What landed:

- **Schema v6** — `sessions.reflection TEXT DEFAULT NULL`, additive
  ALTER TABLE in `state_manager._ensure_schema`.
- **`StateManager.set_session_reflection(session_id, text)`** — single
  writer for the column; calls `_notify_mirror` so the page body
  refreshes (the mirror still omits the property).
- **`PUT /sessions/<int:session_id>/reflection`** in `app.py` —
  Bearer-token bridge gated by `WORKER_BRIDGE_SECRET`.
- **`notion/schema.py`** — `"Reflection": {"rich_text": {}}` added to
  `SESSIONS_PROPERTIES`. Re-run `scripts/notion_bootstrap.py` to add it
  to the live workspace (idempotent).
- **`notion/mirror.py`** — `_session_properties` docstring names the
  omission contract; regression test
  `tests/test_notion_mirror.py::test_omits_reflection_property` is the
  trip-wire.
- **`notion_worker/`** — new TypeScript Worker. One `worker.webhook`
  handler. Deploy via `ntn workers deploy`; subscribe Notion's
  `page.content_updated` event on PRE Sessions to the resulting URL.
- **`tools/state.py` / `companion.py`** — `get_sessions` description
  and coach norms teach the agent to treat reflections as primary
  context. `_session_data` injects `reflection` into every returned
  entry when present, so `load_full_context` carries it through
  automatically.

The open-requirements decisions in the section below (echo prevention,
conflict UX, validation UX) are resolved by the Workers design: the
mirror never writes Reflection, so there is no echo to prevent and no
conflict to resolve.

The Phase 2 sketch below is preserved as the pre-decision record.

## Phase 2 — initial plan (not started)

PRD §2 outcome: *Before — structured journal data (sleep, stress, illness) is
chat-hostile and gets lost. After — typed properties capturable directly in
Notion, syncing back to SQLite.* Phase 1 made Notion a read surface; Phase 2
turns the Journal DB into an editing path that flows back into SQLite. The
other three mirror DBs stay one-way for now.

This section is **planning state, not shipped state** — sibling to the open
issues table above. Nothing here has been built; the doc captures the
sketch so future sessions can pick the requirements refinement up cold.

### Two designs in tension

| | Design A — Flask webhook handler | Design B — Notion Worker |
|---|---|---|
| Source | `notion-integration-prd.md` §2 | "Out of scope" table below |
| Shape | `database-content-updated` webhook → new `POST /notion/webhook` Flask endpoint → handler fetches the page, validates, writes to SQLite | Lift `notion/mirror.py` into a Notion Worker (deployed via Notion CLI), subscribe to Notion Webhook Triggers, run mirror logic inside Notion's compute |
| New files | `notion/webhook.py`, `notion/verify.py` | Worker bundle + Notion CLI deploy config |
| Trade | Small, fast, low-risk; reuses Flask alongside `/webhook` and `/strava/webhook` | Bigger architectural commitment; moves the mirror off Railway and uses first-party trigger plumbing |

**Default: Design A** unless we commit to B. The case for revisiting B: if
Phase 3 (race-day briefings) and Phase 4 (coaching log) both also need
Notion → PRE flows, per-flow Flask handlers stack up and the Worker design
amortizes across all three.

### Concrete build steps (Design A)

1. **New Flask endpoint** — `POST /notion/webhook` in `app.py`, mirroring the
   shape of `/strava/webhook` (`app.py:129`). Returns 200 fast.
2. **Signature verification** — new `notion/verify.py`. Notion sends a
   shared-secret header; reuse the `TELEGRAM_WEBHOOK_SECRET` env-var pattern
   (`app.py:168`).
3. **Webhook handler** — new `notion/webhook.py`:
   1. Verify signature → drop if invalid.
   2. Read changed page IDs from the event payload.
   3. `GET /v1/pages/{id}` via `NotionClient` (need a new `retrieve_page`
      method — `notion/client.py` has `update_page` and `query_data_source`
      but no plain retrieve yet).
   4. Echo-prevention check (see decision 1 below).
   5. Validate property values (sleep 0–24, stress 1–5 per PRD §2).
   6. Write to SQLite via `state_manager.append_journal()`
      (`state_manager.py:808`) — or a new sibling method if we need to
      preserve the Notion timestamp instead of `datetime.now()`.
   7. Optionally `notify_telegram("Logged journal entry from Notion: ...")`.
4. **Subscribe Notion to the webhook** — one-time out-of-band:
   register a `database-content-updated` subscription on the Journal DB
   pointing at the Railway URL. Document in `README.md` / `.env.example`.
5. **Tests** — mirror the patterns in `tests/test_notion_views.py`: handler
   unit tests with a fake Notion client, signature-verify rejection,
   echo-prevention regression, validation reject-and-log, last-write-wins
   conflict.

### Open requirements decisions

1. **Echo prevention.** PRD §2 specifies hidden `source` + `sqlite_id`
   properties on the Journal DB so PRE-originated writes can be filtered
   at the handler. But journal is still a singleton TEXT blob
   (`state_manager.py:808`; row-ification is deferred — see "Deferred
   items"). Three options:
   - (a) Row-ify journal first (clean, but pulls a deferred schema
     refactor into Phase 2 scope).
   - (b) Set a hidden `source=pre` on every Phase 1 mirror write
     (`notion/mirror.py:_journal_properties` line 111); handler checks
     `source=pre` AND that properties match SQLite → no-op. The
     `sqlite_id` placeholder property is already on the schema
     (`notion/schema.py:90`) — would also need a new `source` property.
   - (c) Timestamp window: mirror writes within the last N seconds are
     PRE-originated. Fragile under clock skew / slow webhook delivery.

   **Default: (b).** Minimal new scope; defer the row-ify refactor. Migrate
   to (a) only if conflict pain shows up.

2. **Conflict resolution.** PRD §2: "last-write-wins, but log both
   versions." Default: log only. Decision: do we also want to surface
   conflicts to Telegram?

3. **Validation UX.** PRD §2: "Reject silently and log; don't crash on bad
   data." Default: silent log. Decision: do we also write a comment back on
   the rejected Notion page explaining why? Better UX but means a Notion
   write from the handler.

4. **New-row vs edit-row handler path.** `database-content-updated` fires
   on both. PRD flow assumes one handler can do both ("if `sqlite_id` is
   set, update; otherwise insert and write back the id"). Concrete only
   once decision 1 is settled.

5. **Hidden-property migration.** If we go with (b) above, the Journal DB
   schema gains a new hidden `source` property — `notion/schema.py` change
   plus a backfill on existing pages. Trivial but flag it.

6. **Revisit Design B.** If we go with A, document the decision durably so
   we don't keep relitigating. Revisit if Phases 3/4 both need Notion→PRE
   flows.

### Verification plan

How we'll know Phase 2 actually works end-to-end:

- **Functional:** add a row in PRE Journal in Notion with `Sleep hours=6,
  Stress=4, Tags=[travel]`. Within ~5s, see a matching `journal` blob entry
  in SQLite (`./venv/bin/python scripts/state_dump.py` or direct `sqlite3`
  query).
- **Echo prevention:** post a journal entry via Telegram → confirm the
  resulting Phase 1 mirror write does NOT cause the Phase 2 handler to
  write the same entry back to SQLite. Watch logs for "echo dropped".
- **Validation:** set `Sleep hours=99` in Notion → no SQLite write, log
  line records rejection.
- **Edit flow:** edit an existing Notion Journal row → SQLite row updates
  (last-write-wins).
- **Cross-surface query:** "show me every workout that followed a sleep
  night under 6 hours" — answerable as a Notion filter joining PRE
  Sessions × PRE Journal by date (the PRD-cited success metric).
- **Tests:** `make check` green; new `tests/test_notion_webhook.py` covers
  handler unit tests, signature verify, echo prevention, validation, and
  conflict-resolution log.

### Decisions to settle before implementation begins

In rough order:

1. Design A vs Design B (Flask webhook vs Notion Worker). Default: A.
2. Echo prevention approach (row-ify first / `source=pre` flag / timestamp
   window). Default: `source=pre` flag.
3. Validation UX (silent log vs comment-back-to-Notion). Default: silent
   log.
4. Conflict UX (log only vs surface to Telegram). Default: log only.

## Out of scope — later phases mapped in the original plan

| Phase | What |
|---|---|
| **2** | Bidirectional sync. Lift `notion/mirror.py` into a Notion Worker, deploy via Notion CLI, subscribe to Webhook Triggers (Notion → Worker on row edit; Strava → Worker on activity). Optionally swap planned-sessions push for Database Sync pulling `/notion/sessions`. |
| **3** | Race-day briefing pages. LLM authors markdown via the Markdown API; GPX / route maps via the File Upload API. |
| **4** | Coaching log of state-changing turns. Register PRE via the External Agents API so the user can `@PRE` from any Notion page. |
| —     | Multi-athlete / workspace-scoped OAuth (currently single integration / single workspace). |
| —     | More elaborate diffs than before/after row snippets (semantic diffs, week-over-week training-load comparisons). |

## Files to read first when picking up cold

- `notion-integration-prd.md` — the original full plan, including the
  rationale for the 3.5 platform features used (Markdown API, `/v1/views`,
  data-sources model, Workers, etc.).
- `notion/mirror.py` — the entire fire-and-forget mirror. `enabled()` /
  `journal_enabled()` / `plan_changes_enabled()` / `reviews_enabled()` show
  the per-DB config gates. `_upsert_lock` serializes all upserts globally to
  fix the query-then-insert race caught in 1B.2 review.
- `notion/client.py` — thin `requests` wrapper pinned to `Notion-Version:
  2026-03-11`. `search()` paginates (fixed during 1B.1 QA).
- `state_manager.py` — `_notify_mirror_*` helpers are the entry points every
  writer calls after commit. `_append_changelog(conn, note, body=None)`
  returns the dict (with body) that lands in PRE Plan Changes.
- `scripts/cutover_to_unified_sessions.py` — the once-per-DB cutover. Runs
  from `gunicorn.conf.py:on_starting`.
- `scripts/notion_seed.py` — idempotent backfill of all four mirror DBs.
- `state/schema.sql` — current canonical schema, v5.

## Operational notes

- **Env vars** live in `.env` (gitignored). Required for the mirror:
  `NOTION_TOKEN`, `NOTION_API_VERSION` (`2026-03-11`),
  `NOTION_PARENT_PAGE_ID`, and one or more of `NOTION_SESSIONS_DS_ID` /
  `NOTION_JOURNAL_DS_ID` / `NOTION_PLAN_CHANGES_DS_ID` /
  `NOTION_REVIEWS_DS_ID`. Unset → mirror short-circuits silently.
- **`scripts/state_pull.sh`** pulls a consistent prod DB copy via `railway
  ssh` + base64. Was rewritten during 1A.1 QA after the original `cat
  coach.db` corrupted the binary through the PTY. If it ever reports
  "Unauthorized," `railway login` again.
- **Cutover is reversible.** `scripts/cutover_to_unified_sessions.py`
  archives (`sessions_v1_archive`, `plan_archive`) rather than dropping. A
  rollback restores the archive tables and reverts the code.
- **Notion side is reversible too.** The mirror is one-way. Editing or
  trashing a Notion page changes nothing in SQLite. The seed reconstructs
  the Notion side from SQLite at any time.

## Quick recovery commands

```bash
# Pull a fresh prod DB copy
./scripts/state_pull.sh -o /tmp/prod.db

# Run the cutover on a copy (idempotent)
./venv/bin/python scripts/cutover_to_unified_sessions.py --db /tmp/prod.db

# Re-mirror everything from SQLite to Notion (idempotent)
./venv/bin/python scripts/notion_seed.py --db /tmp/prod.db

# Health check (includes Notion users/me probe)
./venv/bin/python -c "from health import run_health_checks; print(run_health_checks())"
```

## Where the docs / archive lives

- This file: `notion_project_state_may_20.md` (state snapshot).
- Original plan: `notion-integration-prd.md`.
- Schema reference: `state/schema.sql`.
- README / ARCHITECTURE etc. predate this work and don't yet cover the mirror
  — `/document-release` after merging #32 will sync them.
