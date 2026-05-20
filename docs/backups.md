# Backups runbook

Operational reference for the daily SQLite backup job. The bot's authoritative state lives in a single SQLite file (`$DATABASE_PATH`, default `state/coach.db`) on a Railway persistent volume. The volume survives deploys and restarts but is single-region with no point-in-time recovery and no versioned history — a volume failure or accidental deletion would wipe everything.

[`scripts/backup_db.py`](../scripts/backup_db.py) snapshots the DB using SQLite's [online-backup API](https://www.sqlite.org/backup.html) (safe while the bot is writing) and pushes the result to a dedicated `state-snapshot` branch on GitHub. Run it daily from a Railway scheduled job for versioned, off-site backups at zero infra cost.

## Prerequisites

### 1. Fine-grained GitHub PAT

Create the token at <https://github.com/settings/personal-access-tokens/new>:

- **Repository access**: *Only select repositories* → this repo only.
- **Repository permissions** → **Contents**: *Read and write* (nothing else).
- **Expiration**: pick something you're comfortable rotating (90 days is reasonable).

Copy the token once — GitHub won't show it again. Treat it like any other secret.

### 2. Railway persistent volume

The job must mount the same `coach-state` volume the web service writes to, otherwise it'll snapshot an empty DB. See the [README → Deployment](../README.md#deployment) for the volume setup; the backup job needs the volume mounted at the same path as the web service (default `/app/data`).

## Env vars

| Var | Required? | Notes |
|---|---|---|
| `DATABASE_PATH` | yes | Path to the live `coach.db` on the mounted volume (e.g. `/app/data/coach.db`). |
| `GITHUB_BACKUP_TOKEN` | yes | The fine-grained PAT from step 1. Never logged — `_redact()` strips it from any subprocess output. |
| `GITHUB_REPO` | yes | `owner/repo`, e.g. `elimchayseng/pre-running-coach-bot`. |
| `BACKUP_BRANCH` | no | Branch to push snapshots to. Defaults to `state-snapshot`. |
| `BACKUP_FORMAT` | no | `binary` (default — fast restore, opaque diffs) or `sql` (human-diffable text dump via `iterdump()`). |

## Railway scheduled job

1. In the Railway project, create a new scheduled job (Settings → New Service → Cron Job).
2. **Schedule**: `0 11 * * *` — 11:00 UTC daily. Pick a low-traffic hour for the bot; backups are fast (seconds) but they hold a read lock during the copy.
3. **Start command**: `python scripts/backup_db.py`
4. **Mount the `coach-state` volume** at `/app/data` (same mount as the web service).
5. **Env vars**: the three required ones above. Optional `BACKUP_BRANCH` / `BACKUP_FORMAT` if you want non-defaults.
6. Trigger it manually once via Railway's UI and confirm a commit appears on the `state-snapshot` branch with `coach.db` (or `coach.sql`). The log line on success looks like `snapshot 2026-05-20T11-00-00Z pushed to elimchayseng/pre-running-coach-bot on branch state-snapshot`.

## Idempotency and exit codes

- If today's snapshot is byte-identical to the last one already on the branch, the job logs `no changes since last snapshot; skipping commit` and exits 0 without pushing — safe to re-run.
- Missing env vars, a missing DB file, or an invalid `BACKUP_FORMAT` exit with code 2 *before* any network I/O.
- Any clone/push failure raises `CalledProcessError` and propagates a non-zero exit. Configure Railway to alert on non-zero cron exits.

## Manual trigger (from your laptop)

Useful before destructive operations (e.g. running migrations against prod):

```bash
DATABASE_PATH=/path/to/local-or-pulled/coach.db \
GITHUB_BACKUP_TOKEN=ghp_... \
GITHUB_REPO=elimchayseng/pre-running-coach-bot \
python scripts/backup_db.py
```

To pull prod's DB down first, use `./scripts/state_pull.sh -o /tmp/prod-coach.db` and point `DATABASE_PATH` at that.

## Restore procedure

When you need to roll back to a snapshot, the live DB lives on the Railway `coach-state` volume at `/app/data/coach.db` (inside the web service container). You have to get the snapshot file from GitHub onto that volume. Three steps:

### Step A — On your laptop: fetch the snapshot

```bash
# On your laptop.
git clone https://github.com/elimchayseng/pre-running-coach-bot.git restore-tmp
cd restore-tmp
git checkout state-snapshot
# `git log` shows one commit per backup. HEAD = most recent.
git checkout <commit-sha>     # optional; skip to use HEAD
# Snapshot file is now at ~/.../restore-tmp/coach.db (or coach.sql) on your laptop.
```

### Step B — Get the snapshot file into the container

Pick one of these — Railway doesn't give you `scp` into the volume, so you either re-run the container with the file mounted, or you paste it in over the shell.

**Option B1 — base64 paste via `railway shell` (works for any size, but slow for large DBs):**

```bash
# On your laptop, in restore-tmp/, encode the snapshot to your clipboard.
base64 -i coach.db | pbcopy           # or coach.sql for the SQL format

# Open a shell inside the running web service.
railway shell --service web
# Inside the container — paste the clipboard into the heredoc body:
base64 -d > /tmp/coach.db <<'EOF'
<paste here, then a blank line, then EOF>
EOF
```

**Option B2 — `railway run` with the file in the working directory (simpler for small DBs):**

```bash
# On your laptop, from inside restore-tmp/.
railway run --service web bash
# Railway uploads the working directory into the container, so the snapshot
# is visible at ./coach.db inside the shell.
cp ./coach.db /tmp/coach.db
```

Either way, you now have `/tmp/coach.db` (or `/tmp/coach.sql`) **inside the container**.

### Step C — Restore onto the Railway volume

Still inside the container shell from Step B, write onto `/app/data/coach.db` (the path on the mounted `coach-state` volume):

```bash
# Inside the container.
# Keep a safety copy of whatever's currently live before overwriting.
mv /app/data/coach.db /app/data/coach.db.bak

# Binary format:
cp /tmp/coach.db /app/data/coach.db

# OR SQL format — rebuild from the text dump:
sqlite3 /app/data/coach.db < /tmp/coach.sql
```

Then exit the shell and restart the web service from Railway's UI so gunicorn re-opens the DB and the schema migrations in `gunicorn.conf.py:on_starting` re-run idempotently against the restored data.

## Log safety

`_redact()` substitutes `x-access-token:[REDACTED]@` for the PAT-bearing clone URL in every logged command and any captured `stderr`/`stdout` from `git`. Cron logs (Railway's job-run history, log forwarders, error trackers) are safe to share verbatim for debugging — the token never lands in a log line.
