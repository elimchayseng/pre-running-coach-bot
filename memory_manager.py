import logging
from datetime import datetime, timedelta, date
from functools import lru_cache
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from config import mem0_client, logger

USER_ID = "runner"
AGENT_ID = "pre_coach"

# Memory truncation limit (tokens ~ chars/4)
MAX_MEMORY_CHARS = 400  # ~100 tokens per memory


def _truncate_memory(text: str, max_chars: int = MAX_MEMORY_CHARS) -> str:
    """Truncate long memory text to save tokens."""
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError))
)
def _mem0_search(query: str, limit: int = 3) -> list:
    """Mem0 search with retry logic."""
    try:
        result = mem0_client.search(query=query, user_id=USER_ID, limit=limit)
        if result is None:
            return []
        return result.get("results", []) if isinstance(result, dict) else (result or [])
    except Exception as e:
        logger.error(f"Mem0 search failed: {e}")
        return []


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError))
)
def _mem0_add(messages: list, user_id: str = USER_ID, metadata: dict = None, agent_id: str = None) -> None:
    """Mem0 add with retry logic."""
    try:
        kwargs = {"user_id": user_id} if user_id else {}
        if agent_id:
            kwargs = {"agent_id": agent_id}
        if metadata:
            kwargs["metadata"] = metadata
        mem0_client.add(messages, **kwargs)
    except Exception as e:
        logger.error(f"Mem0 add failed: {e}")


# Cache for repeated queries (60 second TTL simulated via lru_cache)
_query_cache = {}
_cache_timestamps = {}
CACHE_TTL_SECONDS = 60


def _get_cached_search(query: str, limit: int = 3) -> list:
    """Search with simple TTL cache."""
    cache_key = f"{query}:{limit}"
    now = datetime.now()

    if cache_key in _query_cache:
        cached_time = _cache_timestamps.get(cache_key)
        if cached_time and (now - cached_time).seconds < CACHE_TTL_SECONDS:
            return _query_cache[cache_key]

    result = _mem0_search(query, limit)
    _query_cache[cache_key] = result
    _cache_timestamps[cache_key] = now
    return result


def store_conversation(user_message: str, assistant_response: str) -> None:
    """Store a conversation exchange with temporal metadata."""
    from temporal_context import get_temporal_context
    ctx = get_temporal_context()

    dated_user_msg = f"[{ctx['date']}] {user_message}"
    dated_assistant_msg = f"[{ctx['date']}] {assistant_response}"

    messages = [
        {"role": "user", "content": dated_user_msg},
        {"role": "assistant", "content": dated_assistant_msg}
    ]
    _mem0_add(
        messages,
        user_id=USER_ID,
        metadata={
            "stored_date": date.today().isoformat(),
            "training_phase": ctx["training_phase"],
            "days_to_race": ctx["days_to_race"]
        }
    )


def retrieve_context_and_constraints(query: str, limit: int = 3) -> tuple[str, str]:
    """Combined retrieval for context and constraints (reduces API calls from 2 to 1+filtering)."""
    # Single broader search
    combined_query = f"{query} injury limitation constraint recovery"
    memories = _get_cached_search(combined_query, limit=limit + 2)

    if not memories:
        return "", ""

    today = date.today()
    context_parts = []
    constraint_parts = []

    for mem in memories:
        if mem is None:
            continue
        memory_text = _truncate_memory(mem.get("memory", ""))
        if not memory_text:
            continue

        metadata = mem.get("metadata") or {}

        # Check if this is a constraint/injury
        is_constraint = metadata.get("type") == "injury" or any(
            kw in memory_text.lower()
            for kw in ["injury", "pain", "sore", "limit", "constraint", "recovery"]
        )

        if is_constraint:
            # Check expiration for injuries
            exp_date = metadata.get("expiration_date")
            if exp_date and date.fromisoformat(exp_date) < today:
                continue  # Skip expired
            constraint_parts.append(f"- {memory_text}")
        else:
            context_parts.append(f"- {memory_text}")

    return "\n".join(context_parts), "\n".join(constraint_parts)


def retrieve_context(query: str, limit: int = 3) -> str:
    """Retrieve relevant memories for the current query."""
    memories = _get_cached_search(query, limit)
    if not memories:
        return ""

    context_parts = []
    for mem in memories:
        if mem is None:
            continue
        memory_text = _truncate_memory(mem.get("memory", ""))
        if memory_text:
            context_parts.append(f"- {memory_text}")

    return "\n".join(context_parts)


def store_agent_personality() -> None:
    """Initialize coach personality in agent memory."""
    from config import PRE_PERSONALITY
    _mem0_add(
        [{"role": "system", "content": PRE_PERSONALITY}],
        user_id=None,
        agent_id=AGENT_ID
    )


def get_constraints() -> str:
    """Get current injuries and limitations, respecting expiration dates."""
    memories = _get_cached_search("injury limitation constraint recovery", limit=3)
    if not memories:
        return ""

    today = date.today()
    constraints = []
    for mem in memories:
        if mem is None:
            continue
        metadata = mem.get("metadata") or {}
        exp_date = metadata.get("expiration_date")
        if exp_date and date.fromisoformat(exp_date) < today:
            continue
        memory_text = _truncate_memory(mem.get("memory", ""))
        if memory_text:
            constraints.append(f"- {memory_text}")

    return "\n".join(constraints)


def update_goal(goal_description: str) -> None:
    """Update race goal, deduplicating existing goals."""
    existing = _mem0_search("race goal target time marathon", limit=3)

    # If similar goal exists, skip adding duplicate
    goal_lower = goal_description.lower()
    for mem in existing:
        if mem and goal_lower in mem.get("memory", "").lower():
            logger.info(f"Goal already exists, skipping: {goal_description}")
            return

    _mem0_add(
        [{"role": "user", "content": f"My marathon goal is: {goal_description}"}],
        user_id=USER_ID
    )


def store_injury(description: str, days_until_recovery: int = 14) -> None:
    """Store injury with automatic expiration."""
    expiration = (datetime.now() + timedelta(days=days_until_recovery)).strftime("%Y-%m-%d")
    _mem0_add(
        [{"role": "user", "content": f"Injury: {description}"}],
        user_id=USER_ID,
        metadata={"type": "injury", "expiration_date": expiration}
    )


def get_all_memories() -> list:
    """Get all stored memories for the user."""
    try:
        result = mem0_client.get_all(user_id=USER_ID)
        if result is None:
            return []
        return result.get("results", []) if isinstance(result, dict) else (result or [])
    except Exception as e:
        logger.error(f"Failed to get all memories: {e}")
        return []


def clear_all_memories() -> None:
    """Clear all memories for the user."""
    try:
        mem0_client.delete_all(user_id=USER_ID)
    except Exception as e:
        logger.error(f"Failed to clear memories: {e}")


def store_race_date(race_name: str, race_date: str) -> None:
    """Store race date in memory for persistence."""
    _mem0_add(
        [{"role": "user", "content": f"My target race: {race_name} on {race_date}"}],
        user_id=USER_ID,
        metadata={"type": "race_date", "date": race_date}
    )


def get_race_date() -> Optional[str]:
    """Retrieve race date from memory."""
    memories = _get_cached_search("target race date", limit=1)
    if not memories or memories[0] is None:
        return None
    return memories[0].get("memory")


def clear_cache() -> None:
    """Clear the query cache (useful for testing or forced refresh)."""
    global _query_cache, _cache_timestamps
    _query_cache = {}
    _cache_timestamps = {}
