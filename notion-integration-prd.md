# PRE × Notion — Integration PRD

A four-phase plan to make Notion the durable, reflective, editable surface for PRE. Each phase is independently shippable. Headlines state the before/after outcome each phase unlocks.

**Framing.** PRE has two surfaces today: Telegram (live, ephemeral, 2h Redis TTL) and Google Calendar (rigid, workouts-only, event-description-bound). The structured state PRE maintains — sessions, journal, plan changelog, post-activity reviews — lives in SQLite where only `sqlite3` queries can see it. Notion becomes the third surface: where the runner browses, queries, annotates, and edits PRE's structured record. SQLite remains source of truth.

**Build order.** Phase 1 establishes the Notion workspace and write infrastructure. Phase 2 turns the journal mirror into a second editing path. Phases 3 and 4 are independent extensions on the Phase 1 base.

---

## 1. Before: state requires sqlite queries to inspect. After: state is browsable in Notion with filter, board, and calendar views.

**Problem.** Every interesting question the runner might ask about PRE's record — "show me every workout that followed a poor sleep night," "why has Tuesday been swapped three weeks in a row," "what was my average pace on easy runs in April" — currently requires opening a terminal and running SQL or `scripts/state_dump.py`. The data is structured but not browsable. The plan changelog in particular is invisible: append-only text in SQLite that no surface ever renders.

**Build.** A one-way mirror from SQLite to four Notion databases under a single "PRE" parent page. SQLite stays source of truth. On every state write in `state_manager.py`, push the same change to Notion. No reads from Notion in this phase.

**Why Notion specifically.** The four datasets need different views — sessions want a calendar view, plan changes want a filter by reason, journal wants typed properties, reviews want grouping by approved/rejected. Notion gives all of that for free out of one schema. Building this on Postgres + a dashboarding tool gets you a worse version of the same thing with no ability to comment, annotate, or hand-edit.

**Notion artifacts to create.**

Parent page: `PRE`

Database: `Sessions` (mirrors `sessions` table)

| Property | Type | Source |
|---|---|---|
| Date | Title (ISO date) | `sessions.date` |
| Type | Select (easy / workout / long / race / cross / strength) | `sessions.type` |
| Miles | Number | `sessions.miles` |
| Avg pace | Rich text | `sessions.data.pace` |
| Avg HR | Number | `sessions.data.avg_hr` |
| Prescribed | Rich text | from `plan.md` for that date |
| Strava ID | Number | `sessions.data.details.strava_id` |
| Strava URL | URL | constructed |
| Notes | Rich text | `sessions.data.notes` |

Views: All sessions (table), This week (filter), By type (board), Calendar.

Database: `Journal` (mirrors `journal` table)

| Property | Type | Source |
|---|---|---|
| Timestamp | Title | `journal.timestamp` |
| Entry | Rich text | `journal.body` |
| Sleep hours | Number | parsed from entry if present, else null |
| Stress | Select (1 / 2 / 3 / 4 / 5) | parsed if present |
| Tags | Multi-select (travel / illness / soreness / life / decision) | inferred or chat-set |

Views: Recent, By tag, Sleep < 6h.

Database: `Plan changes` (mirrors `plan_changelog` table)

| Property | Type | Source |
|---|---|---|
| Date | Title (ISO date) | changelog entry timestamp |
| Reason | Rich text | reason field |
| Diff | Rich text | before → after summary |
| Triggered by | Relation → Sessions | session that caused the change, if any |

Views: All, By reason, This block.

Database: `Reviews` (mirrors post-activity LLM reviews)

| Property | Type | Source |
|---|---|---|
| Session | Relation → Sessions | linked session |
| Date | Date | session date |
| Critique | Rich text | review body |
| Proposed change | Rich text | proposal text |
| Status | Select (approved / rejected / expired / no-op) | resolution outcome |

Views: All, Pending, By status.

**Implementation.**

New module: `notion_mirror.py`. Wraps the Notion REST client. Idempotent upsert by external key (session date for sessions, timestamp for journal, etc.). Uses the Notion 2026-03-11 version.

Hook points:

- `state_manager.write_session()` → `notion_mirror.upsert_session(row)`
- `state_manager.append_journal()` → `notion_mirror.upsert_journal(entry)`
- `state_manager.write_plan_changelog()` → `notion_mirror.upsert_plan_change(entry)`
- Strava post-activity review path → `notion_mirror.upsert_review(review)`

All writes go through a single `_with_retry()` wrapper that handles 429s and transient 5xx. Failures are logged and dropped — SQLite is still source of truth, so a missed Notion write is not a data-loss event.

Initial seed: one-shot `scripts/notion_seed.py` reads existing SQLite rows and upserts everything. Idempotent so it can be rerun.

Env vars: `NOTION_TOKEN` (internal connection), `NOTION_PARENT_PAGE_ID`, `NOTION_SESSIONS_DB_ID`, `NOTION_JOURNAL_DB_ID`, `NOTION_PLAN_CHANGES_DB_ID`, `NOTION_REVIEWS_DB_ID`.

Notion API endpoints used: `POST /v1/pages` (page create for each DB row), `PATCH /v1/pages/{id}` (update), `POST /v1/databases` (one-time DB creation in `notion_seed.py`).

**Out of scope.** Reading from Notion. Comments. Property edits flowing back to SQLite. Multi-athlete. Custom views beyond the defaults listed above.

**Success.** All four DBs exist with current state. Adding a session via Telegram appears in the Sessions DB within ~2s. The "This week" view in Sessions shows what `/log 7` shows. A new entry in `plan_changelog` shows up as a row in Plan changes. The runner can sort sessions by avg HR ascending and answer "what were my five lowest-HR easy runs" without writing SQL.

**Depends on.** Nothing.

---

## 2. Before: structured journal data (sleep, stress, illness) is chat-hostile and gets lost. After: typed properties capturable directly in Notion, syncing back to SQLite.

**Problem.** Sleep hours and a stress 1–5 score are not natural chat inputs. The runner won't type "I slept 6.5 hours and I'd say my stress was a 4" into Telegram — they'll type "rough night" and the structured signal is lost. The journal table accepts freeform text, but that's not queryable for the patterns that actually matter (does a poor sleep night reliably degrade tomorrow's workout?).

**Build.** Turn the Journal DB from phase 1 into a bidirectional surface. The runner adds or edits a row in Notion → Notion fires a `database-content-updated` webhook → PRE's webhook handler pulls the row, validates, writes to SQLite. Optionally pings Telegram acknowledging the new entry.

**Why Notion specifically.** Notion has typed properties (Number, Select, Multi-select) that Telegram chat doesn't. The two surfaces complement each other: Telegram for ambient capture ("rough night, going easy today"), Notion for structured reflection ("sleep: 6.5h, stress: 4, tags: [travel]"). The same DB serves both views without a schema split.

**Build details.**

New endpoint: `POST /notion/webhook` on the existing Flask app.

Notion webhook subscription: `database-content-updated` on the Journal DB ID.

Handler flow:

1. Verify webhook signature (Notion uses a shared secret in the verification header).
2. Read the changed page IDs from the event payload.
3. For each page, fetch its properties via `GET /v1/pages/{id}`.
4. Reconcile against SQLite: if the Notion entry has a `sqlite_id` property, update that row; otherwise insert a new row and write the resulting id back to Notion to close the loop.
5. Update `journal` table.
6. Optionally `notify_telegram("Logged journal entry from Notion: ...")`.

Edge cases to handle:

- Echo prevention: phase 1's mirror writes also fire `database-content-updated`. Distinguish PRE-originated writes (carry a `source=pre` flag in a hidden property, or check whether the row already matches SQLite) and no-op them.
- Validation: sleep hours must be 0–24, stress must be 1–5. Reject silently and log; don't crash on bad data.
- Conflict: if the same row was edited in Notion and in SQLite (via Telegram) within seconds, last-write-wins, but log both versions.

New file: `notion/webhook.py` (handler), `notion/verify.py` (signature check).

Add `source` and `sqlite_id` hidden properties to the Journal DB schema in phase 1 to support this — easier to add them now and ignore them than to retrofit later.

**Out of scope.** Bidirectional for Sessions, Plan changes, Reviews. Bidirectional for athlete profile. Conflict resolution beyond last-write-wins.

**Success.** Adding a row to the Journal DB in Notion with sleep=6, stress=4, tags=[travel] results in a `journal` row in SQLite within ~5s. Editing that row in Notion updates SQLite. The runner can answer "show me every workout that followed a sleep night under 6 hours" via a Notion filter joining Sessions and Journal by date.

**Depends on.** Phase 1 (need the Journal DB and the `sqlite_id` property scheme).

---

## 3. Before: race-day pacing plans and quality-workout details are jammed into gcal event-description strings. After: rich Notion pages per race and key workout, with comments, gcal events linking out.

**Problem.** Today the per-day `#### YYYY-MM-DD` detail block from `plan.md` gets dumped into the gcal event description for races and quality sessions. That field is a tiny string, hostile to mobile reading, has no comments, no rich content, no drafting iteration, no place to annotate after the fact. The most important moments of the training cycle live in the worst possible UI.

**Build.** For races and any session tagged as a key workout, PRE generates a Notion page under a `PRE / Race day` parent and a `PRE / Key workouts` parent. Page content includes pacing tables, fueling schedule, gear, weather pull (if available), and mental cues. The gcal event description gets replaced with a one-line summary plus a link to the Notion page. The runner edits and annotates before the session; after, comments on the page get pulled into the journal via the existing comment webhook.

**Why Notion specifically.** Pages are the right surface for content that's drafted ahead of time, edited, and annotated. The gcal description was always a hack. Notion also gives comments-as-discussion natively, which means post-race reflection ("nailed mile 8 fueling, bonked at 22") happens on the same artifact as the plan, not in a separate journal entry that's disconnected from the briefing.

**Build details.**

Extend `tools/calendar.py` and the plan parser to detect races and key workouts. For each, the agent (or `sync_plan_to_calendar`) calls `notion_mirror.create_or_update_briefing(date, session_type, content)`.

Page structure (use the markdown content endpoint, `PATCH /v1/pages/{id}/markdown`):

```
# Sunday, July 19 — Goal marathon

**Target:** 3:25 (7:50/mi)
**Course:** [link]
**Weather (T-3 day):** [pulled at sync time]

## Splits
| Mile | Target | Cumulative |
|---|---|---|
| 1   | 7:55  | 7:55  |
| ... | ...   | ...   |

## Fueling
- 0:00 — gel + caffeine
- 0:45 — gel
- ...

## Gear
- ...

## Mental cues
- Miles 1-3: hold back, "patient"
- Miles 18-22: "this is what you trained for"
- ...
```

gcal event description becomes: `Race day. Plan + splits → notion.so/...`

Comments-to-journal flow: subscribe to `comment-created` webhook on the briefing pages' parent. When the runner comments, append a timestamped entry to the `journal` table with a back-reference to the race date. Tag as `race` or `key-workout`.

New file: `notion_briefing.py` (page generation + markdown templates).

Notion API endpoints used: `POST /v1/pages` (create briefing), `PATCH /v1/pages/{id}/markdown` (update content), `POST /v1/comments` not needed (read-only on comments), webhook `comment-created`.

**Out of scope.** Two-way edits (runner edits the pacing table → PRE re-syncs to gcal). Weather integration if not already present. Auto-generating briefings for every quality workout — start with races only, expand if useful.

**Success.** Race weeks have a briefing page rendered with the locked sections above. The gcal event for race day shows a one-liner + link. Opening the link on a phone shows a mobile-readable page. A comment left on the page within 24h of the race shows up in the `journal` table.

**Depends on.** Phase 1 (need the `PRE` parent page and Notion client wiring).

---

## 4. Before: coaching reasoning is ephemeral; only outcomes survive. After: a queryable archive of state-changing exchanges, linked to the changes they caused.

**Problem.** Redis holds ~10 turns for 2h. The conversations that produce plan changes — "I felt awful on Tuesday, the cuban heel is back, can we cut Saturday's long run?" — drive `update_plan`, `update_athlete`, and `append_journal` calls, then disappear. The `plan_changelog` captures the *what*; nothing captures the *why* in the runner's own words. Six weeks later you can see Tuesday got swapped three times but not the conversations that drove each swap.

**Build.** Mirror only the turns that triggered state-modifying tool calls into a Notion `Coaching log` DB. Skip everything else (most chat is noise). Each row links to the change it produced.

**Why Notion specifically.** Two reasons. One, a relational view: a Plan changes row links back to the conversation that produced it. Two, this is a low-volume, high-value dataset (maybe 1–5 entries per week) where browsing matters more than search. Notion's table + filter views are the right shape; a chat history viewer is the wrong shape.

**Build details.**

In `companion.py`'s agent loop, instrument the tool-call dispatch: after a turn completes, if any of `update_plan`, `update_athlete`, `append_journal` were called, capture the full turn (user message + assistant response + tool calls) and push to Notion via `notion_mirror.log_coaching_turn(...)`.

Database: `Coaching log`

| Property | Type | Source |
|---|---|---|
| Timestamp | Title | turn end time |
| User said | Rich text | the runner's message |
| Coach said | Rich text | assistant's user-visible response |
| Tools called | Multi-select | names of state-modifying tool calls |
| Triggered | Relation → Plan changes / Journal / Sessions | what state was modified |
| Summary | Rich text | one-line auto-summary, optional |

Views: All, By tool, This block, Search.

The one-line auto-summary is optional — could be a second LLM pass at low priority, could be skipped entirely. Start without it; add later if browsing becomes hard.

Edge cases:

- Tool calls that no-op'd (called `update_plan` but the diff was empty) — skip, not interesting.
- Long Telegram threads where multiple turns lead to one plan change — log only the turn that triggered the actual tool call. The earlier discussion is recoverable from Redis at the moment but lost after 2h. If this becomes a real gap, expand to log a turn window.
- PII / privacy — single-user app right now, but if this ever goes multi-user the runner's messages need to be opt-in to write to Notion.

**Out of scope.** Logging every turn. Auto-summarization in phase 1. Search across coaching log content (Notion's search is fine for this volume).

**Success.** A week with three plan changes has three Coaching log entries, each linked to its Plan changes row. The runner can click any row in Plan changes and see exactly what conversation produced it.

**Depends on.** Phase 1 (need the Plan changes DB to link to).

---

## Sequencing summary

| Phase | Depends on | Effort | Outcome |
|---|---|---|---|
| 1 | — | weekend | State is browsable. Foundation for everything else. |
| 2 | 1 | half a weekend | Notion becomes a second journal input path. |
| 3 | 1 | weekend | Race briefings get their right surface. |
| 4 | 1 | half a weekend | Coaching reasoning gets preserved. |

Build in order 1 → (3 in parallel with 2 or 4) → finish the rest. Phase 1 is the unlock; everything else is independent on top of it.

## Open questions

- **Notion authentication.** Single-user app, so an internal connection with a long-lived token is fine. If this ever goes multi-user, swap to OAuth public connections.
- **Rate limits.** Notion API is 3 requests/second average. Phase 1 mirror traffic is well under that. Bulk seed should batch with sleeps.
- **Hosting.** All phases can run inside the existing Flask app + Railway worker. No need to reach for the Workers beta yet; it becomes relevant if PRE goes multi-user or if you want the Notion agent to call PRE tools directly (out of scope for this PRD).
- **What lives where.** Source-of-truth stays in SQLite for everything except briefing-page content (Notion is the canonical surface for that; SQLite stores only the gcal event ID and the Notion page ID). Worth revisiting if phase 3 ships and the page-as-content pattern works well.
