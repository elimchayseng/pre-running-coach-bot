# Keeping the training calendar healthy

The plan→calendar sync is one-way and event-driven: it fires when you edit the
plan or when Strava logs a workout. Two things break that silently:

1. **OAuth refresh-token expiry.** While the OAuth app is in *testing* mode,
   Google expires unverified refresh tokens after **7 days**. After that every
   sync raises `GcalAuthError`, the fire-and-forget sync thread just logs a
   warning and exits, and the calendar quietly stops updating.
2. **No periodic sweep.** If you don't edit the plan and don't log a run, the
   calendar can drift from the SQLite source of truth with nothing to correct it.

This doc covers the permanent fix for (1) and the watchdog that covers both.

---

## 1. Root-cause fix — publish the OAuth app (do this once)

Publishing the app to *production* removes the 7-day refresh-token cap. You do
**not** need to complete Google verification — an unverified *published* app
still works; you just click through a one-time warning. We request only the
**sensitive** scope `calendar.events` (not a *restricted* scope), so there is
no CASA security assessment involved.

**Click-path** (Google Cloud Console, in the project that owns `GCAL_CLIENT_ID`):

1. Go to **APIs & Services → OAuth consent screen** (newer console: **Google
   Auth Platform → Audience**).
2. Find **Publishing status: Testing**. Click **Publish App** → confirm
   **Push to production**.
3. Status now reads **In production**. That's it — the 7-day expiry is gone.
4. Re-auth once against prod so you mint a fresh, non-expiring refresh token in
   prod's token store:
   ```
   make gcal-reauth-prod
   ```
   You'll see a **"Google hasn't verified this app"** screen →
   **Advanced** → **Go to PRE (unsafe)** → allow. This warning only appears at
   consent time and only you ever see it. (See §3 for what this command does.)

> **Status:** the app is already **published to production**, so the 7-day cap
> no longer applies.

After this, refresh tokens persist until revoked or unused for **6 months**. The
in-process watchdog (below) sweeps every 6h, and each pass exercises the refresh
token (`classify_auth` → `get_access_token`, then `sync_plan`), keeping it warm
so the 6-month inactivity expiry never triggers. The watchdog is now a
rarely-firing safety net rather than a weekly chore.

> **Going multi-user later?** Publishing unverified caps you at 100 users and
> shows them that warning — fine for a private beta. A polished public launch
> needs Google **verification** (privacy policy, a homepage on a domain you
> own, app logo, demo video; days-to-weeks review) *and* a multi-tenant
> refactor (per-user tokens + a calendar per user). A lower-friction
> alternative is a per-user **`.ics` subscription feed** — no OAuth at all, but
> one-way only (you lose the completion checkmarks). Decide based on whether
> two-way completion tracking is core.

---

## 2. The watchdog — `calendar_health.py`

Runs one pass: classify auth → if OK, run a full sync sweep → if a real re-auth
is needed, send a **deduplicated Telegram alert** with the fix command.

```
./venv/bin/python calendar_health.py            # check + sync + alert
./venv/bin/python calendar_health.py --dry-run  # check + plan diff, no writes
./venv/bin/python calendar_health.py --check     # classify auth only, no sync
```

Exit codes: `0` healthy · `2` needs re-auth · `3` infrastructure (e.g. Redis
unreachable — tokens are *not* lost, so it deliberately does **not** tell you
to re-auth).

It distinguishes a dead refresh token (alert you) from an unreachable token
store (log for ops, no alert). Alerts are rate-limited to once per
`GCAL_HEALTH_ALERT_COOLDOWN_HOURS` (default 12) and reset after any healthy run.
Auth status also now shows up in the bot's `/health` command.

### Auto-run in prod — an in-process scheduler

The watchdog runs as a **daemon thread inside the web service**, not a separate
Railway service. This is deliberate: the plan lives in SQLite on a Railway
**volume**, and volumes attach to only one service — a separate cron service
would have its own empty DB and couldn't sweep the plan. Running in the web
worker means it shares both the volume DB (`$DATABASE_PATH`) and the prod Redis
token store. (Single gunicorn worker + no `preload_app` ⇒ the thread starts
exactly once; see `app.py` → `calendar_health.start_scheduler_if_enabled()`.)

It starts ~60s after boot, then sweeps every `CALENDAR_HEALTH_INTERVAL_HOURS`
(default **6**). Each pass: classify auth → sync sweep (self-heals drift,
auto-recovers after a re-auth, keeps the token warm) → dedup Telegram alert on
auth failure.

**Enablement — auto, no manual flag.** It turns on only inside the Railway
deploy: the gate requires a Railway-injected runtime var (`RAILWAY_ENVIRONMENT`
/ `RAILWAY_SERVICE_NAME` / `RAILWAY_PROJECT_ID` — absent in pytest, the `flask`
CLI, and a local `python app.py`) plus `CALENDAR_ID` + `TELEGRAM_BOT_TOKEN`. The
Railway signal is deliberate: without it, a dev running locally with a
prod-shaped `.env` would sweep the **local stale DB** onto the **prod calendar**.
Force it off with `DISABLE_CALENDAR_HEALTH_SCHEDULER=1`; always off under `TESTING`.

**Env to set on the `web` service:** `CALENDAR_ID`, `TELEGRAM_BOT_TOKEN`,
`USER_TELEGRAM_CHAT_ID`, `GCAL_TOKENS_BACKEND=redis` (plus the OAuth creds and
`REDIS_URL` it already has). Optional tuning: `CALENDAR_HEALTH_INTERVAL_HOURS`,
`GCAL_HEALTH_ALERT_COOLDOWN_HOURS` (default 12).

Confirm it's live in the Railway logs: `calendar watchdog scheduler started`
at boot, then `calendar watchdog pass complete` ~60s later.

---

## 3. When you get the alert — re-auth against prod (one command)

The tokens live in prod's **Redis**, so re-auth must target that store, not your
local `.gcal_tokens.json`. One command handles it:

```
make gcal-reauth-prod
```

What it does: runs the loopback OAuth flow **locally** (so the `127.0.0.1`
browser redirect works on your Mac) but overrides `REDIS_URL` with the Railway
Redis **public proxy** (fetched from `railway variables --service Redis`) and
sets `GCAL_TOKENS_BACKEND=redis` — so the fresh token is written straight to
prod Redis. Because `load_dotenv(override=False)`, these inline vars win over
`.env`. You'll click through the consent screen; success prints
`✓ Wrote tokens to redis backend.`

> Why not `railway run … auth`? That injects the **internal** `REDIS_URL`
> (`redis.railway.internal`), which only resolves *inside* Railway's network —
> the write fails from your laptop. The make target uses the public proxy
> instead. (Requires the `railway` CLI logged in and linked to the project.)

Verify, then let it sync:

```
make gcal-status-prod     # reads prod Redis: token present, calendar reachable
```

No manual sweep needed — the in-process watchdog auto-syncs within ≤6h (or
restart the `web` service to trigger the ~60s post-boot pass immediately).
