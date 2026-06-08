.PHONY: lint test check format gcal-reauth-prod gcal-status-prod

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	TESTING=1 pytest tests/ -v

check: lint test

# Re-authorize Google Calendar against PROD Redis in one command. Runs the
# loopback OAuth flow locally (browser on your Mac) but points REDIS_URL at the
# Railway Redis public proxy so the fresh token lands in prod's token store.
# load_dotenv(override=False) means these inline vars win over .env.
gcal-reauth-prod:
	@command -v railway >/dev/null 2>&1 || { echo "ERROR: railway CLI not found. Install: https://docs.railway.app/guides/cli"; exit 1; }
	@railway whoami >/dev/null 2>&1 || { echo "ERROR: railway not logged in / linked. Run: railway login && railway link"; exit 1; }
	@PUB="$$(railway variables --service Redis --json | ./venv/bin/python -c 'import sys,json; u=json.load(sys.stdin).get("REDIS_PUBLIC_URL"); sys.exit("ERROR: REDIS_PUBLIC_URL not found on the Redis service") if not u else print(u)')" || exit 1; \
	echo "Re-authing Google Calendar against prod Redis..."; \
	REDIS_URL="$$PUB" GCAL_TOKENS_BACKEND=redis ./venv/bin/python scripts/google_calendar_setup.py auth

# Print prod's Google Calendar auth/token/calendar status (reads prod Redis).
gcal-status-prod:
	@command -v railway >/dev/null 2>&1 || { echo "ERROR: railway CLI not found. Install: https://docs.railway.app/guides/cli"; exit 1; }
	@railway whoami >/dev/null 2>&1 || { echo "ERROR: railway not logged in / linked. Run: railway login && railway link"; exit 1; }
	@PUB="$$(railway variables --service Redis --json | ./venv/bin/python -c 'import sys,json; u=json.load(sys.stdin).get("REDIS_PUBLIC_URL"); sys.exit("ERROR: REDIS_PUBLIC_URL not found on the Redis service") if not u else print(u)')" || exit 1; \
	REDIS_URL="$$PUB" GCAL_TOKENS_BACKEND=redis ./venv/bin/python scripts/google_calendar_setup.py status
