# Session Reflection Bidirectional Sync — Implementation Plan

## Scope

Add one bidirectional field — `Reflection` — to the PRE Sessions Notion database. Notion is the only writer; SQLite is the eventual store. Coach reads from SQLite on next chat.

**Out of scope:** all other session fields stay one-way mirror writes. Journal stays as-is. Plan rows stay as-is.

---

## Phase 1 — SQLite schema (15 min)

**Goal:** add a `reflection` column to `sessions` without disturbing existing data.

1. In `state/schema.sql`, add `reflection TEXT DEFAULT NULL` to the `sessions` table definition
2. Bump schema version v5 → v6 in your schema version constant
3. In `_ensure_schema` (wherever that lives in `state_manager.py`), add an additive migration:
   ```sql
   ALTER TABLE sessions ADD COLUMN reflection TEXT DEFAULT NULL;
   ```
   Wrap in a check that the column doesn't already exist (`PRAGMA table_info(sessions)`)
4. Add `set_session_reflection(session_id: int, text: str | None) -> bool` to `state_manager.py`
5. Update `get_sessions` to include `reflection` in its return shape

**Test:** run migration on a copy of prod DB, verify existing rows are intact and the column is present.

---

## Phase 2 — Update Notion mirror (30 min)

**Goal:** add `Reflection` as a Notion DB property; ensure the mirror never writes to it.

1. In `notion/schema.py`, add `Reflection` to the PRE Sessions DB schema as a rich-text property
2. Re-run `scripts/notion_bootstrap.py` — verify it's idempotent and adds the missing property without recreating the DB
3. In `notion/mirror.py`, audit every code path that builds a session-page update payload. Confirm none of them include the `Reflection` property in the `properties` object. Notion's PATCH semantics only update properties you specify, so omission = preservation. Add a comment naming this contract so future edits don't break it.
4. Update `notion/markdown.py:render_session_body` if needed — make sure the body content rendered by the mirror doesn't visually compete with the Reflection property

**Test:** trigger a session update via Strava auto-log (or manually invoke `mirror_session`), verify the existing Reflection value in Notion is preserved.

---

## Phase 3 — Railway bridge endpoint (45 min)

**Goal:** an authenticated HTTP endpoint the Worker can call to update reflections in SQLite.

1. In `app.py`, add `PUT /sessions/<int:session_id>/reflection`
2. Auth: shared secret in `Authorization: Bearer <token>` header, verified against a new env var `WORKER_BRIDGE_SECRET`
3. Body: `{"reflection": "..." | null}`
4. Handler: validate, call `set_session_reflection`, return 200 with the new state or 404 if session not found
5. Add the secret to `.env.example` and Railway env
6. Log every call (session_id, length of text, source IP) — useful for debugging

**Test:** `curl` the endpoint locally with and without the auth header. Verify SQLite reflects the change.

---

## Phase 4 — Build the Notion Worker (1-2 hours)

**Goal:** a Worker that listens for `Reflection` property changes on PRE Sessions rows and calls the bridge endpoint.

1. Scaffold:
   ```bash
   ntn workers new pre-reflection-sync
   cd pre-reflection-sync
   ```
2. Configure secrets via the Notion CLI:
   - `RAILWAY_BASE_URL` — e.g. `https://pre-coach.up.railway.app`
   - `WORKER_BRIDGE_SECRET` — same value as in Railway env
   - `NOTION_SESSIONS_DS_ID` — the data source ID for PRE Sessions
3. Register a webhook capability that subscribes to property-change events on the PRE Sessions data source, filtered to the `Reflection` property
   - **Note:** the exact API for Notion-side webhook triggers isn't in the doc snippet you shared. Check Notion's Worker docs for the trigger registration syntax (likely `worker.webhook("onReflectionEdit", { source: dataSourceId, on: "property_changed", property: "Reflection" })` or similar). If property-level filtering isn't supported, filter inside the handler.
4. Handler logic (pseudocode):
   ```typescript
   worker.webhook("onReflectionEdit", async (event) => {
     const pageId = event.page.id;
     const sourceKey = event.page.properties["source_key"]; // hidden key set by mirror
     if (!sourceKey?.startsWith("sid:")) return; // not a real session row
     const sessionId = parseInt(sourceKey.slice(4), 10);
     const reflectionText = extractRichText(event.page.properties["Reflection"]);

     const res = await fetch(`${RAILWAY_BASE_URL}/sessions/${sessionId}/reflection`, {
       method: "PUT",
       headers: {
         "Authorization": `Bearer ${WORKER_BRIDGE_SECRET}`,
         "Content-Type": "application/json",
       },
       body: JSON.stringify({ reflection: reflectionText || null }),
     });
     if (!res.ok) console.error(`Bridge call failed: ${res.status}`);
   });
   ```
5. Deploy:
   ```bash
   ntn workers deploy
   ```

**Test:** edit `Reflection` on a real session row in Notion. Confirm SQLite updates within a few seconds via `sqlite3 state/coach.db "SELECT id, reflection FROM sessions WHERE id = X"`.

---

## Phase 5 — Coach integration (30 min)

**Goal:** the agent actually uses reflections in its context.

1. In `companion.py:build_system_prompt`, when including recent sessions, append the `reflection` field where non-null. Format something like:
   ```
   Wed 5/21 — 8x800 @ 5K pace — completed
     Athlete note: "felt strong after the third rep, gels worked, slight tightness in left calf last 2"
   ```
2. In `tools/state.py:get_sessions`, surface `reflection` in the tool response
3. Update the coach personality prompt to acknowledge reflections explicitly — one line is enough: "When the athlete has left reflections on past sessions, weigh them alongside the prescribed/actuals data when planning the next block."

**Test:** add a reflection to a recent session in Notion, wait for sync, then ask coach a question that should reference it (e.g. "how should I adjust hydration for Sunday's long run?" with a reflection that mentions running out of water).

---

## Phase 6 — Loop / clobber audit (15 min)

**Goal:** confirm no edge case can wipe a user's reflection.

Walk through each scenario, expected behavior:

| Scenario | Expected |
|---|---|
| Strava auto-logs an activity that updates an existing session | Reflection preserved (mirror omits Reflection from update payload) |
| Mirror re-runs a backfill via `scripts/notion_seed.py` | Reflection preserved (seed script must read existing page properties, not blindly upsert) |
| User edits Reflection on a session that was just deleted in SQLite | Worker calls bridge → 404 → logs warning, no crash |
| User edits Reflection on a planned (not-yet-completed) session | Bridge accepts it. Reflection lives on the row regardless of session status. |
| Two rapid edits in Notion | Last write wins via Worker; each fires the webhook independently |

The seed script case is the sneakiest — make sure `notion_seed.py` is patched to skip the Reflection property entirely, or to read-then-write so it preserves existing values.

---

## Phase 7 — Demo prep (30 min)

1. Pick 3-5 recent real sessions; add varied reflections that the coach could plausibly use:
   - One about hydration ("ran out of water at mile 8")
   - One about gear ("new shoes — heels rubbed")
   - One about pacing ("started too hot, blew up at mile 4")
2. Pre-prepare one chat question per reflection that should make the coach reference it
3. Have `sqlite3` + Notion + Telegram open in three windows for the demo
4. Optional: record a 60-second screen capture as backup in case live demo fails

---

## Risks and known unknowns

1. **Notion webhook trigger API surface.** The Workers doc snippet is high-level. The exact syntax for subscribing to a property-level change event on a specific database needs to come from Notion's full Workers docs. If property-level filtering isn't available, filter in the handler (you'll just receive more events than you need).

2. **Worker → Railway latency.** Notion webhook → Worker → Railway HTTP → SQLite is probably under 2 seconds end-to-end, but if Railway is sleeping or restarting, the Worker call could time out. Add a retry with exponential backoff in the Worker (Workers may give you retry semantics for free — check the docs).

3. **The source_key property.** Your existing mirror stores `source_key` as a hidden property on each page. Confirm the Worker can read it from the webhook event payload. If not, fall back to looking it up via a Notion API call inside the handler.

4. **No verification that the Worker is healthy.** Add a `/health/worker` check that the Worker hits a Railway endpoint every N minutes so you can see in your existing `/health` whether the sync path is alive. Or skip for the demo and add later.

---

## Time estimate

End-to-end with focused work: **4-6 hours**, plus another hour for demo prep. Most of the cost is Phase 4 (first time using Workers) and Phase 6 (paranoid clobber-prevention testing).

If you hit a wall on the Workers API specifically, the rest of Phases 1-3 and 5 are all useful regardless and can ship without the Worker — you'd just have a Railway endpoint with no caller yet.

---

## What to lead the interview with

The product framing, not the plan:

> "I shipped a one-field bidirectional sync — session reflections. The interesting question wasn't 'how do I sync', it was 'which field is worth syncing?' Plan rows are too contentious; the agent and the user both want to write them. Journal entries are too freeform; conflicts are messy. Session notes are perfect — the user is the only writer, the value is concrete (post-run context the coach actually uses), and the surface is one Notion property. Smallest possible thing that proves the platform works."

Then walk through the demo. Plan stays in your back pocket for follow-up engineering questions.
