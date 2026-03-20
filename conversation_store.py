import os
import json
import logging
import redis
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("pre_coach")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MAX_HISTORY_TURNS = 10
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2 hours (increased from 30 min)

_redis_client = None


def _get_redis() -> redis.Redis:
    """Get Redis client with lazy initialization and reconnection support."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5
        )
    return _redis_client


def _reset_redis() -> None:
    """Reset Redis client (for reconnection)."""
    global _redis_client
    _redis_client = None


def _key(user_id: str) -> str:
    return f"session:{user_id}:history"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError))
)
def get_history(user_id: str) -> list[dict]:
    """Get conversation history for user with retry."""
    try:
        r = _get_redis()
        data = r.get(_key(user_id))
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
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError))
)
def add_turn(user_id: str, role: str, content: str) -> None:
    """Add a message to history with retry."""
    try:
        r = _get_redis()
        history = get_history(user_id)
        history.append({"role": role, "content": content})

        # Sliding window: keep last N turns (N*2 messages)
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]

        r.setex(_key(user_id), SESSION_TTL_SECONDS, json.dumps(history))
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to add turn: {e}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError))
)
def clear_history(user_id: str) -> None:
    """Clear conversation history for user with retry."""
    try:
        r = _get_redis()
        r.delete(_key(user_id))
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to clear history: {e}")


def check_redis_health() -> bool:
    """Check if Redis is reachable (for health checks)."""
    try:
        r = _get_redis()
        r.ping()
        return True
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return False
