"""Short-term conversation history in Redis.

Single-user app — no namespacing. Long-term coaching state lives in the
state/ files (see state_manager); this module only persists the last ~10
turns of the current conversation, with a 2-hour TTL.
"""

import json
import logging
import os
from typing import Optional

import redis
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("pre_coach")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SESSION_KEY = "session:history"
MAX_HISTORY_TURNS = 10
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours

_redis_client: Optional[redis.Redis] = None


def _get_redis() -> redis.Redis:
    """Lazy Redis client with reconnection support."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _redis_client


def _reset_redis() -> None:
    global _redis_client
    _redis_client = None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError)),
)
def get_history() -> list[dict]:
    try:
        data = _get_redis().get(SESSION_KEY)
        if not data:
            return []
        return json.loads(data)
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to get history: {e}")
        return []


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError)),
)
def add_turn(role: str, content: str) -> None:
    try:
        r = _get_redis()
        history = get_history()
        history.append({"role": role, "content": content})
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2) :]
        r.setex(SESSION_KEY, SESSION_TTL_SECONDS, json.dumps(history))
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to add turn: {e}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError)),
)
def clear_history() -> None:
    try:
        _get_redis().delete(SESSION_KEY)
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to clear history: {e}")


def check_redis_health() -> bool:
    try:
        _get_redis().ping()
        return True
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return False
