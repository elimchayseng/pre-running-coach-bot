# Backups runbook

Operational reference for the SQLite backup playbook. The bot's authoritative state lives in a single SQLite file (`$DATABASE_PATH`, default `state/coach.db`) on a Railway persistent volume. The volume survives deploys and restarts but is single-region with no point-in-time recovery and no versioned history — a volume failure or accidental deletion would wipe everything.

[`scripts/backup_db.py`](../scripts/backup_db.py) snapshots the DB using SQLite's [online-backup API](https://www.sqlite.org/backup.html) (safe while the bot is writing) and pushes the result to a dedicated `state-snapshot` branch on GitHub. **Backups are manual.** Railway's volume model is single-attach, so a separate cron service can't read the volume; the script runs inside the web service container via `railway ssh` on whatever cadence you commit to.

## Prerequisites

### 1. Fine-grained GitHub PAT

Create the token at <https://github.com/settings/personal-access-tokens/new>:

- **Repository access**: *Only select repositories* → this repo only.
- **Repository permissions** → **Contents**: *Read and write* (nothing else).
- **Expiration**: pick something you're comfortable rotating (1 year is reasonable; calendar-remind yourself).

Copy the token once — GitHub won't show it again. Stash it in 1Password / Notes / wherever you keep secrets.

### 2. Railway CLI

```bash
brew install railwayapp/railway/railway   # skip if installed
railway login                              # browser-based auth
railway link                               # select this project
```

Verify SSH into the web service works:

```bash
railway ssh --service web "ls -la /app/data/coach.db"
```

Should print the DB file with a non-zero size. If the service isn't named `web`, find the actual name in the Railway dashboard.

### 3. Shell alias (recommended)

So you don't retype env vars every time. Append to `~/.zshrc` (or your shell's rc file):

```bash
export PRE_BACKUP_PAT='github_pat_...paste-here...'
alias pre-backup='railway ssh --service web "DATABASE_PATH=/app/data/coach.db GITHUB_BACKUP_TOKEN=$PRE_BACKUP_PAT GITHUB_REPO=elimchayseng/pre-running-coach-bot python scripts/backup_db.py"'
```

`source ~/.zshrc` to pick it up.

## Env vars

| Var | Required? | Notes |
|---|---|---|
| `DATABASE_PATH` | yes | Path to the live `coach.db` on the mounted volume. Inside the container: `/app/data/coach.db`. |
| `GITHUB_BACKUP_TOKEN` | yes | The fine-grained PAT from step 1. Never logged — `_redact()` strips it from any subprocess output. |
| `GITHUB_REPO` | yes | `owner/repo`, e.g. `elimchayseng/pre-running-coach-bot`. |
| `BACKUP_BRANCH` | no | Branch to push snapshots to. Defaults to `state-snapshot`. |
| `BACKUP_FORMAT` | no | `binary` (default — fast restore, opaque diffs) or `sql` (human-diffable text dump via `iterdump()`). |

## Taking a snapshot

With the alias set up:

```bash
pre-backup
```

Or the long-form, runnable from anywhere:

```bash
railway ssh --service web "DATABASE_PATH=/app/data/coach.db GITHUB_BACKUP_TOKEN='<paste-PAT>' GITHUB_REPO=elimchayseng/pre-running-coach-bot python scripts/backup_db.py"
```

Expected output: `snapshot 2026-05-20T... pushed to elimchayseng/pre-running-coach-bot on branch state-snapshot`. If the DB hasn't changed since the last run, you'll see `no changes since last snapshot; skipping commit` — that's the idempotency guard, not an error.

**Verify** by visiting <https://github.com/elimchayseng/pre-running-coach-bot/tree/state-snapshot>. Latest commit timestamp should match "just now" and the tree should contain `coach.db`.

## When to run it

- **Mandatory**: before any schema migration, column drop, or bulk `UPDATE` / `DELETE` against prod.
- **Hygiene**: pick a weekly cadence (e.g. Sunday evenings) and calendar-remind yourself. Manual is only as reliable as your discipline.
- **Ad hoc**: after manually backfilling data you'd hate to re-enter.

## Idempotency and exit codes

- If today's snapshot is byte-identical to the last one already on the branch, the job logs `no changes since last snapshot; skipping commit` and exits 0 without pushing — safe to re-run.
- Missing env vars, a missing DB file, or an invalid `BACKUP_FORMAT` exit with code 2 *before* any network I/O.
- Any clone/push failure raises `CalledProcessError` and propagates a non-zero exit.

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

## What this protects against

- Railway volume loss or corruption.
- Bad schema migration that scrambles the live DB.
- Accidental `DELETE` / `UPDATE` on prod.
- Single-region Railway outage — the GitHub copy is independent of Railway's storage layer.

## What this does NOT protect against

- **Forgetting to run it.** Manual is only as reliable as your discipline; the longer between runs, the more recent data you'd lose in a recovery.
- Notion being out of sync with the snapshot timestamp. Notion is best-effort one-way; not authoritative.
- GitHub itself being inaccessible at restore time. Rare but real — a global GitHub outage would block restore.

## Log safety

`_redact()` substitutes `x-access-token:[REDACTED]@` for the PAT-bearing clone URL in every logged command and any captured `stderr`/`stdout` from `git`. Logs are safe to share verbatim for debugging — the token never lands in a log line.
