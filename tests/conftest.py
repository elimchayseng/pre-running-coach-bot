import os

# Set TESTING before any project imports
os.environ["TESTING"] = "1"

import fakeredis
import pytest


@pytest.fixture(autouse=True)
def _no_real_notion(monkeypatch):
    """Strip Notion credentials for every test.

    config.py runs load_dotenv() at import time, so a developer's .env (with
    a real NOTION_TOKEN) leaks into the pytest process. The Notion mirror
    gates only on env vars — without this, any StateManager write in a test
    fires daemon threads that create/clobber REAL Notion pages (observed:
    fake PRE Health rows and an overwritten PRE Reviews page). Tests that
    exercise the mirror's gating set the env explicitly via monkeypatch.
    """
    monkeypatch.delenv("NOTION_TOKEN", raising=False)


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance and patch the Redis-backed stores to use it."""
    r = fakeredis.FakeRedis(decode_responses=True)

    import conversation_store
    import pending_proposal_store

    conv_original = conversation_store._redis_client
    prop_original = pending_proposal_store._redis_client
    conversation_store._redis_client = r
    pending_proposal_store._redis_client = r

    yield r

    r.flushall()
    conversation_store._redis_client = conv_original
    pending_proposal_store._redis_client = prop_original
