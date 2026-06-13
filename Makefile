.PHONY: lint test check format gcal-reauth-prod gcal-status-prod coros-reauth-prod coros-status-prod

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	TESTING=1 pytest tests/ -v

check: lint test

# The four *-prod targets all need the Railway Redis public URL. The railway
# preamble (CLI present, logged in) + REDIS_PUBLIC_URL extraction lives once in
# scripts/railway_redis_url.sh; each target sources it via $(REDIS_URL_CMD).
REDIS_URL_CMD = scripts/railway_redis_url.sh

# Re-authorize Google Calendar against PROD Redis in one command. Runs the
# loopback OAuth flow locally (browser on your Mac) but points REDIS_URL at the
# Railway Redis public proxy so the fresh token lands in prod's token store.
# load_dotenv(override=False) means these inline vars win over .env.
gcal-reauth-prod:
	@echo "Target → $$(railway status 2>/dev/null | grep -iE 'project|environment' | tr '\n' ' ')"
	@echo "This writes a fresh token to the above environment's Redis. Ctrl-C now if that's wrong."
	@PUB="$$($(REDIS_URL_CMD))" || exit 1; \
	echo "Re-authing Google Calendar against prod Redis..."; \
	REDIS_URL="$$PUB" GCAL_TOKENS_BACKEND=redis ./venv/bin/python scripts/google_calendar_setup.py auth

# Print prod's Google Calendar auth/token/calendar status (reads prod Redis).
gcal-status-prod:
	@PUB="$$($(REDIS_URL_CMD))" || exit 1; \
	REDIS_URL="$$PUB" GCAL_TOKENS_BACKEND=redis ./venv/bin/python scripts/google_calendar_setup.py status

# Re-authorize COROS against PROD Redis in one command. Same trick as
# gcal-reauth-prod: the loopback OAuth flow runs locally (browser on your Mac)
# but REDIS_URL points at the Railway Redis public proxy so the fresh token
# lands in prod's token store.
coros-reauth-prod:
	@echo "Target → $$(railway status 2>/dev/null | grep -iE 'project|environment' | tr '\n' ' ')"
	@echo "This writes a fresh COROS token to the above environment's Redis. Ctrl-C now if that's wrong."
	@PUB="$$($(REDIS_URL_CMD))" || exit 1; \
	echo "Re-authing COROS against prod Redis..."; \
	REDIS_URL="$$PUB" COROS_TOKENS_BACKEND=redis ./venv/bin/python scripts/coros_setup.py auth

# Print prod's COROS auth/token status + live MCP round-trip (reads prod Redis).
coros-status-prod:
	@PUB="$$($(REDIS_URL_CMD))" || exit 1; \
	REDIS_URL="$$PUB" COROS_TOKENS_BACKEND=redis ./venv/bin/python scripts/coros_setup.py status
