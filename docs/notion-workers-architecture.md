# Notion Workers in PRE — Architecture Overview

> **Audience.** Engineering interview / code-review reader who wants to see
> how PRE uses Notion's 3.5 Developer Platform (May 2026) to add a
> bidirectional surface without owning new infrastructure.
> **One-line pitch.** PRE shipped a one-field bidirectional sync —
> session reflections — on top of a Notion Worker. Notion-hosted compute
> receives a webhook trigger, parses the changed page, and calls a
> Bearer-authenticated PUT into the existing Railway service. No second
> server, no second auth model, no Flask handler dedicated to Notion.

## Why this matters

PRE's Notion integration was already a one-way mirror: every SQLite write
fan-outs to four Notion databases (PRE Sessions, Journal, Plan Changes,
Reviews) via fire-and-forget daemon threads. SQLite is authoritative;
Notion is browseable. See [`../README.md#notion-mirror`](../README.md#notion-mirror).

Going **bidirectional** has historically meant standing up a webhook
endpoint somewhere (verify a shared secret, parse Notion's payload,
fetch the page, validate, write). Notion's 3.5 release introduced
**Workers** — hosted Node/TypeScript compute that sits inside Notion's
edge — which collapses that work into a single TypeScript file deployed
through the `ntn` CLI.

The smallest field that was worth syncing back: **session reflections**.
The athlete is the sole writer, the value is concrete post-run context
the coach actually uses, and the surface is one Notion property.

## What Notion Workers are

Workers are small Node/TypeScript programs that run inside Notion's
sandboxed runtime. You write one file, register capabilities on a
`Worker` instance, and deploy with `ntn workers deploy`. There are three
capability types:

| Capability | What it is | PRE uses it for |
|---|---|---|
| `worker.sync(name, ...)` | Scheduled writer into a Notion DB (default cadence: every 30 minutes). The right tool for "pull from an external API into a Notion database." | Not used (yet). Could replace the planned-sessions push if we wanted to invert direction. |
| `worker.tool(name, ...)` | Callable surface for Notion Custom Agents — agents in Notion invoke the tool on demand. | Not used (yet). Phase 4 of the original PRD (coaching log) would register PRE as a tool so `@PRE` works from any Notion page. |
| `worker.webhook(name, ...)` | HTTP endpoint at a Notion-signed URL. Subscribed to by **either** an external webhook publisher (GitHub, Stripe, ...) **or** a Notion webhook subscription (`page.content_updated`, `comment.created`, `data_source.schema_updated`, ...). | **This is the one PRE ships.** |

Secrets are injected via `process.env.*` at runtime; you configure them
out-of-band with `ntn workers secrets set <KEY> <value>`.

## How PRE uses it

A single `worker.webhook("onReflectionEdit", …)` registered in
[`notion_worker/src/worker.ts`](../notion_worker/src/worker.ts). End-to-end:

```
┌──────────────┐  edit Reflection   ┌──────────────────┐
│ Notion UI    │ ─────────────────▶ │ Notion data      │
│ (PRE         │                    │ source           │
│  Sessions)   │                    │ (PRE Sessions)   │
└──────────────┘                    └────────┬─────────┘
                                             │ page.content_updated
                                             │ subscription (Notion-side)
                                             ▼
                                    ┌──────────────────┐
                                    │ Notion Worker    │
                                    │ onReflectionEdit │
                                    │ • fetch page     │
                                    │ • parse source_  │
                                    │   key + Reflect. │
                                    └────────┬─────────┘
                                             │ PUT /sessions/{id}/reflection
                                             │ Authorization: Bearer …
                                             ▼
┌──────────────┐  load_full_context ┌──────────────────┐
│ Coach LLM    │ ◀───────────────── │ Flask (Railway)  │
│ (companion)  │   (next chat)      │ + SQLite         │
└──────────────┘                    └──────────────────┘
```

Files in this repo that participate:

| File | Role |
|---|---|
| [`notion_worker/src/worker.ts`](../notion_worker/src/worker.ts) | The Worker. One `worker.webhook` handler that fetches the changed page, extracts `source_key` (must be `sid:<n>`) and the `Reflection` rich-text, then calls the bridge. |
| [`notion_worker/README.md`](../notion_worker/README.md) | Deploy / subscribe / verify runbook for the Worker. |
| [`notion/schema.py`](../notion/schema.py) | Adds `"Reflection": {"rich_text": {}}` to `SESSIONS_PROPERTIES` — the bootstrap is idempotent so re-running it adds the property in-place. |
| [`notion/mirror.py`](../notion/mirror.py) | `_session_properties` **omits** `Reflection`. Notion PATCH preserves unset properties → no echo loop. |
| [`app.py`](../app.py) — `put_session_reflection` | Bearer-token (`WORKER_BRIDGE_SECRET`) bridge endpoint. Accepts `{"reflection": "…" \| null}`. Calls `StateManager.set_session_reflection`. |
| [`state_manager.py`](../state_manager.py) — `set_session_reflection` | Writes the column. `_notify_mirror` re-fires so the page body refreshes; the mirror still doesn't touch the Reflection property. |
| [`state/schema.sql`](../state/schema.sql) — schema v6 | `sessions.reflection TEXT DEFAULT NULL`. v5→v6 lands as an additive `ALTER TABLE` in `_ensure_schema`. |
| [`tools/state.py`](../tools/state.py) — `get_sessions` | Tool description teaches the agent to treat reflections as primary context. |
| [`companion.py`](../companion.py) — `build_system_prompt` | One adaptation-norm line: weigh reflections alongside prescribed/actuals. |

## Why Workers instead of a second Flask endpoint

A reasonable alternative — and the original PRD's default —
([`../notion-integration-prd.md`](../notion-integration-prd.md) §2,
also [`../notion_project_state_may_20.md`](../notion_project_state_may_20.md)
"Phase 2 — initial plan") — was a `POST /notion/webhook` endpoint on the
existing Flask app. We picked Workers instead:

1. **No public Railway URL exposed for Notion to call.** Railway is the
   single-writer database tier; reducing its publicly-reachable surface
   means fewer auth paths to harden. The Worker URL is generated by
   Notion and acts as a shared secret (the URL itself is unguessable).
2. **First-party retries.** Notion auto-retries failed deliveries up to
   3×. Equivalent behavior on Flask would mean adding a queue or
   accepting that transient 5xx loses the edit.
3. **Amortization for later phases.** PRD Phase 3 (race-day briefing
   pages) and Phase 4 (`@PRE` from any Notion page) both need
   Notion-side compute — registering tools, authoring pages. Workers is
   the single platform primitive that covers all three; Flask handlers
   would stack.
4. **Demonstrable use of the 3.5 platform.** This is the interview
   artifact: the PR shows `ntn workers deploy` in the runbook, the
   handler in TypeScript, and the bridge auth model — all without
   touching the gunicorn deployment.

## Auth model

Two trust boundaries:

| Boundary | Mechanism |
|---|---|
| Notion → Worker | Notion-signed webhook URL. The URL itself is the bearer token (long random path). Rotated by re-deploying / regenerating the webhook. |
| Worker → Railway bridge | `Authorization: Bearer <WORKER_BRIDGE_SECRET>` header, validated by the Flask handler. Same value lives in Railway env var and `ntn workers secrets`. |

No webhook **signature** verification on the Worker side: the URL secrecy
is the auth. (Notion can — and may in future — sign payloads; the Worker
verifies any `event.headers` / `event.rawBody` signature once available.)

## Failure modes & the no-echo guarantee

| Scenario | Behavior |
|---|---|
| Athlete edits Reflection in Notion | Worker fetches page → PUT /sessions/<id>/reflection → SQLite updates within ~5s. |
| Strava upload completes a planned row (mirror fires) | `_session_properties` omits Reflection from the PATCH payload → Notion preserves the existing Reflection text. **No echo.** |
| `scripts/notion_seed.py` re-runs from SQLite | Same omission contract — Reflection survives. |
| Athlete edits Reflection on a planned (not-yet-completed) session | Worker handles it identically; the column lives on every row regardless of status. |
| Athlete deletes the session in SQLite, then edits Reflection | Bridge returns 404; Worker logs warning, no retry. |
| Bridge returns 5xx | Worker throws → Notion retries up to 3×. |
| Worker is misconfigured (missing secret) | Throws on startup of the handler → Notion marks delivery failed; ops sees it in `ntn workers webhooks list`. |
| Two rapid edits | Each fires its own webhook delivery; last-write-wins in SQLite. |

The load-bearing primitive is the **omission contract** in
[`notion/mirror.py:_session_properties`](../notion/mirror.py). It is
docstring-named and guarded by
[`tests/test_notion_mirror.py::TestSessionProperties::test_omits_reflection_property`](../tests/test_notion_mirror.py).
Regressing it would silently overwrite athlete notes on every Strava
completion; the test is the trip-wire.

## What ships in this phase vs what's deferred

**Ships.** One Notion property (`Reflection`) on one database
(PRE Sessions). End-to-end coach-readable in the next chat turn.

**Deferred** (intentionally — keeps the surface single-purpose):

- Bidirectional for any other field. Journal, Plan changes, Reviews
  stay one-way. The plan-edit and journal flows have an explicit second
  writer (the agent), so conflict resolution is non-trivial; ship that
  separately if/when the value is clear.
- Property-level webhook filtering. If Notion's `page.content_updated`
  carries every property change on the page, the Worker filters in-handler
  rather than at subscription time. Costs one Notion API roundtrip per
  irrelevant edit on a Sessions page — acceptable at our volume.
- Same-value short-circuit. The bridge endpoint accepts every PUT
  unconditionally; an "incoming text equals current value → no-op"
  optimization is an easy follow-up if log volume on `_notify_mirror`
  becomes annoying.
- Worker-side automated tests. Verified manually via the live edit
  flow; the Python side is fully covered.

## How to demo / verify

```bash
# 1. Bootstrap the Notion property (idempotent — adds Reflection to PRE Sessions).
./venv/bin/python scripts/notion_bootstrap.py

# 2. Deploy the Worker (assuming `ntn login` already done).
cd notion_worker
npm install
ntn workers secrets set NOTION_TOKEN <token>
ntn workers secrets set RAILWAY_BASE_URL https://pre-coach.up.railway.app
ntn workers secrets set WORKER_BRIDGE_SECRET <same-as-railway>
ntn workers deploy
ntn workers webhooks list   # copy URL → Notion webhook subscription on PRE Sessions

# 3. Live edit in Notion: type into a Reflection cell on any session row.
sqlite3 state/coach.db "SELECT id, date, reflection FROM sessions WHERE reflection IS NOT NULL"

# 4. Ask the coach a question that should reference the reflection
#    (e.g. you wrote "ran out of water at mile 8" → "how should I adjust
#    hydration for Sunday's long run?"). Confirm it lands.
```

Full runbook lives in [`../notion_worker/README.md`](../notion_worker/README.md).
Phase-by-phase implementation plan in
[`session-reflection-sync-plan.md`](session-reflection-sync-plan.md).

## Interview talking points (compressed)

> "I shipped a one-field bidirectional sync — session reflections — on
> top of Notion Workers. The interesting question wasn't 'how do I
> sync,' it was 'which field is worth syncing?' Plan rows are too
> contentious (the agent and the user both want to write them);
> journal entries are too freeform. Session notes are perfect — the
> athlete is the only writer, the value is concrete post-run context
> the coach actually uses, and the surface is one Notion property.
> Smallest possible thing that proves the platform works."

> "The architectural decision was Workers vs. a second Flask endpoint.
> Workers won on four counts: no new public URL on the Railway tier,
> first-party retries, amortization across later phases, and a clean
> interview narrative — all in a single TS file behind `ntn workers
> deploy`."

> "The load-bearing primitive is a one-line omission contract: the
> Python mirror never writes the Reflection property. Notion PATCH
> preserves unset fields, so a Strava upload that re-mirrors the
> session never clobbers what the athlete typed. Guarded by a
> regression test so a future change can't silently break it."
