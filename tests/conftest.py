import os

# Set TESTING before any project imports
os.environ["TESTING"] = "1"

import fakeredis
import pytest


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance and patch conversation_store to use it."""
    r = fakeredis.FakeRedis(decode_responses=True)

    import conversation_store

    original = conversation_store._redis_client
    conversation_store._redis_client = r

    yield r

    r.flushall()
    conversation_store._redis_client = original
