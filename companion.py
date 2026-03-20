import logging
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from config import llm_client, HEROKU_MODEL, PRE_PERSONALITY, logger
from memory_manager import retrieve_context_and_constraints, store_conversation, USER_ID
from temporal_context import build_temporal_prompt, get_temporal_context
from conversation_store import get_history, add_turn, clear_history

# Session-level cache for system prompt components
_cached_temporal: Optional[str] = None
_cached_temporal_date: Optional[str] = None


def _get_temporal_prompt() -> str:
    """Get temporal prompt, cached for the same day."""
    global _cached_temporal, _cached_temporal_date
    ctx = get_temporal_context()
    current_date = ctx["date"]

    if _cached_temporal_date != current_date:
        _cached_temporal = build_temporal_prompt()
        _cached_temporal_date = current_date

    return _cached_temporal


def get_system_prompt(user_query: str) -> str:
    """Build system prompt with personality, temporal context, and relevant memories."""
    # Get temporal context (cached within day)
    temporal = _get_temporal_prompt()
    ctx = get_temporal_context()

    # Combined memory retrieval (1 API call instead of 2)
    context, constraints = retrieve_context_and_constraints(user_query, limit=3)

    prompt_parts = [
        f"You are PRE, a running coach. {PRE_PERSONALITY}",
        "",
        temporal,
        "",
        "Keep responses concise and actionable."
    ]

    if context:
        prompt_parts.append(f"\nRelevant context:\n{context}")

    if constraints:
        prompt_parts.append(f"\nPhysical constraints:\n{constraints}")

    # Condensed date rules (~30 tokens instead of ~60)
    prompt_parts.append(
        f"\nToday: {ctx['date']}. Use full dates (e.g., 'Tuesday, March 17') in plans. "
        "Prefer recent user statements when memories conflict."
    )

    return "\n".join(prompt_parts)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((ConnectionError, TimeoutError))
)
def _call_llm(system_prompt: str, history: list) -> str:
    """Call LLM with retry logic."""
    response = llm_client.chat.completions.create(
        model=HEROKU_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            *history
        ],
        max_tokens=500
    )
    return response.choices[0].message.content


def chat(user_message: str, user_id: str = USER_ID) -> str:
    """Process user message and return coach response."""
    try:
        system_prompt = get_system_prompt(user_message)

        # Add user message to Redis history
        add_turn(user_id, "user", user_message)

        # Get full history for context
        history = get_history(user_id)

        # Call LLM with retry
        assistant_response = _call_llm(system_prompt, history)

        # Add assistant response to history
        add_turn(user_id, "assistant", assistant_response)

        # Store to Mem0 (skip trivial messages)
        skip_patterns = ["hi", "hey", "hello", "thanks", "thank you", "bye", "ok", "okay"]
        if user_message.lower().strip() not in skip_patterns:
            store_conversation(user_message, assistant_response)

        return assistant_response

    except Exception as e:
        logger.error(f"Chat error: {e}")
        return "I apologize, but I'm having trouble processing your request. Please try again."


def reset_session(user_id: str = USER_ID) -> None:
    """Clear session history (Mem0 memories preserved)."""
    clear_history(user_id)


def reset_prompt_cache() -> None:
    """Reset the cached system prompt components."""
    global _cached_temporal, _cached_temporal_date
    _cached_temporal = None
    _cached_temporal_date = None
