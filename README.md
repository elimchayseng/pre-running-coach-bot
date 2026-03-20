# PRE - AI Running Coach Bot

An AI-powered running coach that provides personalized marathon training guidance with long-term memory, temporal awareness, and injury tracking.

## Features

- **Persistent Memory** — Remembers your goals, training history, and preferences across sessions using Mem0
- **Temporal Awareness** — Knows the current date, days until race day, and your training phase
- **Injury Tracking** — Log injuries with automatic 14-day follow-up tracking
- **Dual Interface** — CLI for local use, Telegram bot for mobile access
- **Session History** — Redis-backed conversation context within sessions

## Tech Stack

- **LLM**: Claude via Heroku Inference (OpenAI-compatible API)
- **Memory**: [Mem0](https://mem0.ai) for long-term athlete context
- **Session Store**: Redis for conversation history
- **Telegram**: python-telegram-bot with Flask webhook
- **Deployment**: Railway (gunicorn)

## Prerequisites

- Python 3.9+
- Redis server (local or hosted)
- Mem0 API key
- Heroku Inference API key
- Telegram Bot token (for Telegram mode)

## Installation

```bash
git clone https://github.com/elimchayseng/pre-running-coach-bot.git
cd pre-running-coach-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example environment file and fill in your keys:

```bash
cp .env.example .env
```

See `.env.example` for all required variables.

## Usage

### CLI Mode

```bash
python main.py
```

### Telegram Bot (webhook)

```bash
python app.py
```

Or with gunicorn:

```bash
gunicorn app:app
```

### CLI Commands

| Command | Description |
|---------|-------------|
| `/goal <time>` | Set race goal (e.g., `/goal 3:25`) |
| `/injury <desc>` | Log an injury with 14-day tracking |
| `/race` | Show race countdown and training phase |
| `/today` | Show current date/time context |
| `/history` | Show stored memories |
| `/reset` | Clear session history (memories preserved) |
| `/clear` | Reset all memories |
| `/health` | Check system health (Redis, Mem0, LLM) |
| `/quit` | Exit |

## Deployment

Deploy to Railway with the included `Procfile`:

1. Create a new project on [Railway](https://railway.app)
2. Connect your GitHub repo
3. Add environment variables from `.env.example`
4. Railway will auto-detect the `Procfile` and deploy

## Project Structure

```
├── app.py                  # Flask webhook server (Telegram)
├── bot.py                  # Telegram bot handlers
├── main.py                 # CLI chat interface
├── companion.py            # Core chat logic and system prompt
├── config.py               # Configuration and client initialization
├── memory_manager.py       # Mem0 memory operations
├── conversation_store.py   # Redis session storage
├── temporal_context.py     # Date/time and training phase logic
├── test_heroku_llm.py      # LLM connectivity test
├── requirements.txt        # Python dependencies
├── Procfile                # Railway process definition
└── .env.example            # Environment variable template
```
