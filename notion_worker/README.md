# pre-reflection-sync — Notion Worker

A one-handler Notion Worker that mirrors **PRE Sessions → Reflection** edits
back into PRE's SQLite store. See the full architecture overview at
[`../docs/notion-workers-architecture.md`](../docs/notion-workers-architecture.md).

## What it does

```
Notion UI (edit Reflection)
        │
        ▼
PRE Sessions data source ── page.content_updated ──▶ Notion Worker
                                                      │  (this code)
                                                      ▼
                                    PUT /sessions/{id}/reflection
                                    Authorization: Bearer …
                                                      │
                                                      ▼
                                       Railway (Flask + SQLite)
```

The Python mirror (`notion/mirror.py`) is the only other surface that writes
the PRE Sessions DB, and it deliberately omits the `Reflection` property
from every update payload — so there is no echo loop.

## Files

- `src/worker.ts` — the Worker. One `worker.webhook("onReflectionEdit", …)`
  registration. Fetches the changed page, extracts `source_key` (`sid:<n>`)
  and `Reflection`, calls the bridge endpoint.
- `package.json` / `tsconfig.json` — minimal TS + `@notionhq/workers` scaffold.

## One-time setup

```bash
# 1. Install the Notion CLI if you don't have it.
curl -fsSL https://ntn.dev | bash
ntn login

# 2. Install local deps (for type-checking; the runtime resolves the SDK).
cd notion_worker
npm install

# 3. Configure the Worker secrets.
ntn workers secrets set NOTION_TOKEN <internal-integration-token>
ntn workers secrets set RAILWAY_BASE_URL https://pre-coach.up.railway.app
ntn workers secrets set WORKER_BRIDGE_SECRET <same-value-as-railway-env>

# 4. Deploy. The CLI bundles, uploads, and starts the Worker.
ntn workers deploy

# 5. Grab the webhook URL — copy this into the Notion subscription below.
ntn workers webhooks list
```

In Notion, on the **PRE Sessions** database:

1. `••• → Connections → connect` the same integration that owns `NOTION_TOKEN`.
2. `••• → Integrations → Webhooks → New webhook`.
3. Event type: `page.content_updated`. Target URL: the URL from step 5.
4. Save.

## Verifying

1. Open any row in **PRE Sessions** in Notion.
2. Type something in the **Reflection** property.
3. Within a few seconds:
   ```bash
   sqlite3 state/coach.db "SELECT id, date, reflection FROM sessions WHERE reflection IS NOT NULL"
   ```
   should show your text.
4. Clear the Reflection cell in Notion → the SQLite column flips back to NULL.

## Updating

```bash
# After any edit to src/worker.ts:
ntn workers deploy
```

Secrets are sticky — `ntn workers secrets set` only when rotating.

## Failure modes

| Event | Result |
|---|---|
| Bridge returns 404 (session deleted in SQLite) | Worker logs warning, returns clean. No retry. |
| Bridge returns 5xx / network failure | Worker throws → Notion retries the delivery (up to 3x). |
| Page has no `sid:*` source_key (created manually) | Worker skips and logs. |
| `Reflection` empty / cleared | Worker sends `{"reflection": null}` → bridge clears column. |
| Mirror writes the page (Strava completion etc.) | `_session_properties` omits Reflection → property preserved → Worker still fires on the page.content_updated event but reads the same value back and writes it again (idempotent). |

The idempotent-rewrite in the last row is harmless but wasteful. Optional
optimization (deferred): the bridge endpoint short-circuits when the
incoming value equals the current column value.
