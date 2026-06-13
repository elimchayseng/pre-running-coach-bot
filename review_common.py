"""Shared internals for the two LLM reviews — post-activity (strava/review.py)
and nightly readiness (coros/review.py).

Both reviews make the same shape of call: one JSON-mode completion, no tools,
parsed with the same tolerant parser, capped to the same Telegram length.
This module owns those pieces so coros/review.py doesn't reach into
strava/review.py's underscore-private surface (issue #56). The names here are
public (no leading underscore) — they are a real shared API now. Each caller
keeps its own local alias for backward compatibility.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from config import HEROKU_MODEL, llm_client

logger = logging.getLogger("pre_coach.review_common")

# Telegram hard-caps messages ~4096 chars; stay under with headroom for the
# "…" truncation marker the callers append.
TELEGRAM_MAX_CHARS = 3900

# A proposed new_plan_md larger than this is almost certainly a runaway
# generation, not a real plan — drop the plan_change rather than stash it.
MAX_NEW_PLAN_MD_CHARS = 32 * 1024


def call_review_llm(messages: list[dict], max_tokens: int = 4000) -> str:
    """Single LLM call, no tools, JSON-mode response. Returns raw text content.

    Callers whose prompts demand a FULL new_plan_md (e.g. the COROS readiness
    check-in) pass a higher max_tokens — a truncated completion parses as
    malformed JSON and silently drops the review.
    """
    response = llm_client.chat.completions.create(
        model=HEROKU_MODEL,
        messages=messages,
        max_tokens=max_tokens,
    )
    if not response.choices:
        raise ValueError("LLM returned no response choices")
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        logger.warning("Review LLM output truncated at max_tokens=%s", max_tokens)
    return choice.message.content or ""


def parse_review_output(raw: str) -> Optional[dict]:
    """Parse the model's JSON output. Returns None on malformed output.

    Tolerates the model wrapping its JSON in ```json fences despite
    instructions otherwise.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip a leading fence and any trailing fence.
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Review LLM returned malformed JSON: {e}; raw: {raw[:300]}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("feedback"), str) or not data["feedback"].strip():
        logger.error(f"Review LLM JSON missing/empty feedback: {raw[:300]}")
        return None
    plan_change = data.get("plan_change")
    if plan_change is not None:
        if not isinstance(plan_change, dict):
            logger.error("plan_change present but not an object; dropping")
            data["plan_change"] = None
        else:
            required = ("summary", "new_plan_md", "reason")
            if not all(isinstance(plan_change.get(k), str) and plan_change.get(k) for k in required):
                logger.error("plan_change missing required string fields; dropping")
                data["plan_change"] = None
            elif len(plan_change["new_plan_md"]) > MAX_NEW_PLAN_MD_CHARS:
                logger.error(f"plan_change.new_plan_md exceeds {MAX_NEW_PLAN_MD_CHARS} chars; dropping")
                data["plan_change"] = None
    return data
