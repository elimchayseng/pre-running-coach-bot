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

# The proposal text is LLM output derived (in the readiness case) from
# third-party COROS free text, and it gets rendered straight into the chat
# agent's system prompt (companion.py) and — on "yes" — into plan.md. Clamp
# it at the single stash boundary both producers (strava + coros review) go
# through. summary/reason are single prompt lines, so newlines in them could
# forge a fake "=== SECTION ===" header; collapse + cap them. new_plan_md is
# fenced at render time (companion picks a fence longer than any inner
# backtick run), so here it only needs a hard size cap.
_MAX_SUMMARY_CHARS = 280
_MAX_REASON_CHARS = 280
_MAX_NEW_PLAN_MD_CHARS = 32 * 1024

_redis_client: Optional[redis.Redis] = None


def _clamp_line(value, limit: int) -> str:
    """Coerce to a single trimmed line, length-capped — defeats newline
    injection into the single-line prompt fields."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _sanitize_proposal(payload: dict) -> dict:
    """Clamp the free-text fields before they're stashed. Oversized
    new_plan_md raises ValueError so the caller degrades to delivering the
    analysis without an (unapplyable) proposal — mirrors the existing
    strava-review drop behavior, now enforced for both producers."""
    out = dict(payload)
    if "summary" in out:
        out["summary"] = _clamp_line(out.get("summary"), _MAX_SUMMARY_CHARS)
    if "reason" in out:
        out["reason"] = _clamp_line(out.get("reason"), _MAX_REASON_CHARS)
    if "new_plan_md" in out and out["new_plan_md"] is not None:
        md = str(out["new_plan_md"])
        if len(md) > _MAX_NEW_PLAN_MD_CHARS:
            raise ValueError(f"new_plan_md exceeds {_MAX_NEW_PLAN_MD_CHARS} chars ({len(md)})")
        out["new_plan_md"] = md
    return out


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
    payload = _sanitize_proposal(payload)
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
