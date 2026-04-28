# PRE — AI Running Coach Bot

A coaching agent for endurance athletes. Reads and writes a structured local
state (athlete profile, training plan, session log, journal) and adapts week
to week using Claude's tool-use API.

## How it works

Every turn the agent loads the full state into the system prompt and decides
which tools to call:

- `get_today` / `get_todays_workout` / `get_week_plan` — read the prescribed plan
- `log_session` — append a session to `log.jsonl`
- `update_plan` — replace `plan.md` (preserving the locked weekly table format)
- `update_athlete` — patch `athlete.yaml` (PRs, zones, resolved injuries)
- `append_journal` — add a timestamped life-context note
- `get_sessions` — date-range query on `log.jsonl`
- `get_fitness_summary` — soft-touch trailing snapshot: weekly volume, pace-vs-zone
  decoration on quality sessions, HR context, English signals (no prescriptions)

Long-term context lives in `state/` files. Short-term in-conversation history
lives in Redis (~10 turns, 2-hour TTL).

## Tech stack

- **LLM**: Claude Sonnet 4.6 (default) via Heroku Inference, OpenAI-compatible client
- **State**: local files — `athlete.yaml` (round-trip via `ruamel.yaml`),
  `plan.md`, `log.jsonl`, `journal.md`
- **Session store**: Redis (single-user, single key)
- **Interfaces**: Telegram webhook (`app.py` + `bot.py`), CLI (`main.py`),
  test harness (`scripts/test_agent.py`)
- **Deployment**: Railway via gunicorn (`Procfile`)

## State files

```
state/
├── athlete.yaml        identity, target_races, prs, zones, preferences,
│                       hr_zones, injury_history, race_history
├── plan.md             current training block; locked
│                       "| Day | Date | Workout | Pace target | Notes |"
│                       table for the current week (parsed by /today)
├── log.jsonl           one session per line: date, type, miles, pace_avg,
│                       hr_avg, rpe, notes, details{}
├── journal.md          freeform timestamped notes
└── plan_changelog.md   append-only log of plan edits + reasons
```

`state/` is gitignored — keep it local or in a separate private repo.

## Prerequisites

- Python 3.9+
- Redis (local or hosted)
- Heroku Inference API key (with access to `claude-sonnet-4-6` or `claude-opus-4-7`)
- Telegram Bot token (for the Telegram interface)

## Installation

```bash
git clone https://github.com/elimchayseng/pre-running-coach-bot.git
cd pre-running-coach-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in your keys
```

Then create your `state/athlete.yaml` and `state/plan.md`. See the example
shapes inline at the top of `state_manager.py` and the schema descriptions in
`tools/state.py`.

## Usage

### Test harness (recommended for iteration)

```bash
./venv/bin/python scripts/test_agent.py
```

REPL with no Telegram or Redis dependency. Slash commands: `/quit /reset /state
/tools /system /raw`.

### CLI

```bash
./venv/bin/python main.py
```

### Telegram (webhook)

```bash
./venv/bin/python app.py        # local
gunicorn app:app                 # production
```

### Slash commands (CLI + Telegram)

| Command | Description |
|---------|-------------|
| `/today` | Today's prescribed workout (no LLM round-trip) |
| `/plan` | Print the current training plan |
| `/log [days]` | Recent sessions, default last 7 days |
| `/race` | Race countdown and training phase |
| `/reset` | Clear short-term Redis history (state files unchanged) |
| `/health` | Check Redis + LLM connectivity |
| `/help` | Show commands |
| `/quit` (CLI only) | Exit |

Free-text messages route through the agent and may invoke any of the tools above.

## Deployment

Deploy to Railway with the included `Procfile`:

1. Create a new project on [Railway](https://railway.app)
2. Connect your GitHub repo
3. Add environment variables from `.env.example`
4. Railway will auto-detect the `Procfile` and deploy

State files are not included in the deploy by default (gitignored). Either:
- Keep state in a private companion repo and clone on deploy, or
- Use a Railway volume mounted at `state/`.

## Tests

```bash
./venv/bin/python -m pytest -q
```

## Project structure

```
├── app.py                  Flask webhook for Telegram
├── bot.py                  Telegram handlers
├── main.py                 CLI chat
├── companion.py            Agent loop: build_system_prompt, agent_turn, chat
├── state_manager.py        State file I/O (read/write/atomic/round-trip YAML)
├── temporal_context.py     Timezone-aware now/today + race resolution
├── conversation_store.py   Redis short-term history
├── config.py               LLM client, env, personality
├── health.py               Health checks + slash command list
├── tools/                  Tool schemas + handlers
│   ├── state.py            log_session, update_plan, append_journal,
│   │                       update_athlete, get_sessions
│   ├── plan.py             get_today, get_todays_workout, get_week_plan
│   └── fitness.py          get_fitness_summary (soft-touch signals)
├── scripts/test_agent.py   CLI harness, no Redis required
└── tests/                  pytest
```
