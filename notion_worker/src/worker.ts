/**
 * PRE Reflection-Sync Worker
 * --------------------------
 *
 * Runs on Notion's hosted Worker runtime (3.5 Developer Platform). Wakes
 * when a Notion webhook subscription on the PRE Sessions data source posts
 * a `page.content_updated` event. For every changed page that carries our
 * `source_key` ("sid:<id>") and a non-empty `Reflection` rich-text, this
 * Worker calls the Railway bridge endpoint
 *
 *     PUT {RAILWAY_BASE_URL}/sessions/{sessionId}/reflection
 *     Authorization: Bearer {WORKER_BRIDGE_SECRET}
 *     {"reflection": "..." | null}
 *
 * which writes the value into SQLite. The Python mirror never writes the
 * Reflection property (see `notion/mirror._session_properties`), so there
 * is no echo loop to guard against.
 *
 * Architecture overview:  docs/notion-workers-architecture.md
 * Phase plan:             docs/session-reflection-sync-plan.md
 *
 * Secrets (configure via `ntn workers secrets set`):
 *   - NOTION_TOKEN          internal-integration token (Read content)
 *   - RAILWAY_BASE_URL      e.g. https://pre-coach.up.railway.app
 *   - WORKER_BRIDGE_SECRET  shared with Railway's WORKER_BRIDGE_SECRET env
 */

// The `@notionhq/workers` package is supplied by the Notion runtime; types
// resolve at deploy time. The Worker file is a single export.
// @ts-ignore: resolved by the Notion runtime, not present in local node_modules.
import { Worker } from "@notionhq/workers";

const worker = new Worker();

const NOTION_API = "https://api.notion.com/v1";
const NOTION_VERSION = "2026-03-11";

type RichText = { plain_text?: string };
type Property =
  | { type: "rich_text"; rich_text: RichText[] }
  | { type: string; [k: string]: unknown };

type NotionPage = {
  id: string;
  properties: Record<string, Property>;
};

/** Pull plain text out of a Notion `rich_text` property. */
function extractRichText(prop: Property | undefined): string {
  if (!prop || prop.type !== "rich_text") return "";
  const parts = (prop as { rich_text: RichText[] }).rich_text ?? [];
  return parts.map((p) => p.plain_text ?? "").join("").trim();
}

/** Parse "sid:<n>" into a numeric session id. Returns null when malformed. */
function parseSourceKey(value: string): number | null {
  if (!value.startsWith("sid:")) return null;
  const n = parseInt(value.slice(4), 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Fetch a single page's properties from Notion. */
async function fetchPage(pageId: string, token: string): Promise<NotionPage> {
  const res = await fetch(`${NOTION_API}/pages/${pageId}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Notion-Version": NOTION_VERSION,
    },
  });
  if (!res.ok) {
    throw new Error(`Notion page fetch failed: ${res.status} ${await res.text()}`);
  }
  return (await res.json()) as NotionPage;
}

/** Forward the reflection text to the Railway bridge. */
async function pushReflection(
  baseUrl: string,
  secret: string,
  sessionId: number,
  reflection: string | null,
): Promise<void> {
  const res = await fetch(`${baseUrl}/sessions/${sessionId}/reflection`, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${secret}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ reflection }),
  });
  if (res.status === 404) {
    // Session was deleted in SQLite (rare race). Don't retry.
    console.warn(`Bridge 404 for session_id=${sessionId} — page likely orphaned`);
    return;
  }
  if (!res.ok) {
    // 5xx / 401 / etc. — throw so Notion's retry mechanism re-runs us.
    throw new Error(`Bridge call failed: ${res.status} ${await res.text()}`);
  }
}

/**
 * Worker webhook handler.
 *
 * The webhook URL produced by `ntn workers webhooks list` is registered as
 * the destination of a Notion webhook subscription on the PRE Sessions data
 * source. Notion posts `page.content_updated` events; we filter to pages
 * with a `sid:<id>` source_key and write any non-empty Reflection text
 * back to SQLite via the bridge.
 *
 * Notion delivers events in batches and retries the entire batch (up to 3x)
 * if this handler throws. Per-page failures log and continue so a single bad
 * page doesn't block the rest of the batch.
 */
worker.webhook("onReflectionEdit", {
  title: "PRE Reflection Sync",
  description:
    "Notion → PRE bridge for athlete-typed Reflection edits on the PRE Sessions database.",
  execute: async (events: Array<{ deliveryId: string; body: unknown }>) => {
    const notionToken = process.env.NOTION_TOKEN;
    const baseUrl = process.env.RAILWAY_BASE_URL;
    const secret = process.env.WORKER_BRIDGE_SECRET;
    if (!notionToken || !baseUrl || !secret) {
      throw new Error(
        "Missing required secret: set NOTION_TOKEN, RAILWAY_BASE_URL, WORKER_BRIDGE_SECRET via `ntn workers secrets set`.",
      );
    }

    for (const event of events) {
      try {
        // `page.content_updated` payloads carry the changed page id under
        // `entity.id`. Defensive parsing: bail cleanly on shapes we don't
        // recognize so a Notion schema bump doesn't crash the Worker.
        const body = (event.body ?? {}) as {
          entity?: { id?: string; type?: string };
          type?: string;
        };
        const pageId = body.entity?.id;
        if (!pageId) {
          console.log(`event ${event.deliveryId}: no entity.id, skipping`);
          continue;
        }

        const page = await fetchPage(pageId, notionToken);
        const sourceKeyText = extractRichText(page.properties["source_key"]);
        const sessionId = parseSourceKey(sourceKeyText);
        if (!sessionId) {
          console.log(
            `event ${event.deliveryId}: page ${pageId} has no sid:* source_key (got ${JSON.stringify(sourceKeyText)}), skipping`,
          );
          continue;
        }

        const reflectionText = extractRichText(page.properties["Reflection"]);
        const payload = reflectionText.length > 0 ? reflectionText : null;
        await pushReflection(baseUrl, secret, sessionId, payload);
        console.log(
          `event ${event.deliveryId}: session_id=${sessionId} synced (len=${payload?.length ?? 0})`,
        );
      } catch (err) {
        // Per-page errors log and continue. If a transient outage hits the
        // bridge, throwing from inside pushReflection above bubbles here and
        // Notion retries the whole delivery — that's the desired behavior.
        console.error(`event ${event.deliveryId} failed:`, err);
        throw err;
      }
    }
  },
});

export default worker;
