"""Pending plan-change proposals in Redis.

A post-activity review can propose a plan change but never auto-applies it.
The proposal is stashed here under a single key, surfaced in the next chat
system prompt, and applied (or discarded) based on the user's reply.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import redis
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("pre_coach")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
PROPOSAL_KEY = "pending_plan_proposal"
PROPOSAL_TTL_SECONDS = 24 * 60 * 60  # 24 hours

_redis_client: Optional[redis.Redis] = None


def _get_redis() -> redis.Redis:
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
def set_pending_plan_proposal(payload: dict) -> None:
    try:
        _get_redis().setex(PROPOSAL_KEY, PROPOSAL_TTL_SECONDS, json.dumps(payload))
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        # Re-raise: swallowing here would return "success" for a proposal
        # that was never stored — the user gets "Reply 'yes' to apply" for
        # nothing. Both callers (strava/coros review) catch and degrade by
        # stripping the proposal from the message.
        logger.error(f"Failed to set pending plan proposal: {e}")
        raise


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError)),
)
def get_pending_plan_proposal() -> Optional[dict]:
    try:
        data = _get_redis().get(PROPOSAL_KEY)
        if not data:
            return None
        return json.loads(data)
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to get pending plan proposal: {e}")
        return None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    retry=retry_if_exception_type((redis.ConnectionError, redis.TimeoutError)),
)
def clear_pending_plan_proposal() -> None:
    try:
        _get_redis().delete(PROPOSAL_KEY)
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning(f"Redis connection error, resetting client: {e}")
        _reset_redis()
        raise
    except Exception as e:
        logger.error(f"Failed to clear pending plan proposal: {e}")
