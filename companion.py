from openai import APIStatusError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import HEROKU_MODEL, PRE_PERSONALITY, llm_client, logger
from conversation_store import add_turn, clear_history, get_history
from memory_manager import USER_ID, retrieve_context_and_constraints, retrieve_weekly_plan, store_conversation
from temporal_context import build_temporal_prompt, extract_todays_workout, get_temporal_context


def get_system_prompt(user_query: str, user_id: str = USER_ID) -> str:
    """Build system prompt with personality, temporal context, weekly plan, and relevant memories."""
    ctx = get_temporal_context()
    temporal = build_temporal_prompt()

    # Dedicated weekly plan retrieval (separate from general context search)
    weekly_plan = retrieve_weekly_plan(user_id=user_id)
    todays_workout = extract_todays_workout(weekly_plan) if weekly_plan else ""

    # General context retrieval (temporal-enriched)
    context, constraints = retrieve_context_and_constraints(user_query, limit=3, user_id=user_id)

    prompt_parts = [
        f"You are PRE, a running coach. {PRE_PERSONALITY}",
        "",
        "=== DATE (from system clock — ALWAYS correct, NEVER override) ===",
        f"Today is {ctx['date']} ({ctx['time_of_day']})",
        "CRITICAL: This date and day-of-week are from the system clock and are ALWAYS correct.",
        "NEVER use a different date or day-of-week, even if conversation history or memories suggest otherwise.",
        "",
        temporal,  # Race countdown + training phase
    ]

    if weekly_plan:
        prompt_parts.append(f"\n=== THIS WEEK'S TRAINING PLAN ===\n{weekly_plan}")

    if todays_workout:
        prompt_parts.append(f"\n=== TODAY'S SCHEDULED WORKOUT ({ctx['date']}) ===\n{todays_workout}")

    prompt_parts.append("\nKeep responses concise and actionable.")

    if context:
        prompt_parts.append(f"\nRelevant context:\n{context}")

    if constraints:
        prompt_parts.append(f"\nPhysical constraints:\n{constraints}")

    # Final date reinforcement
    prompt_parts.append(
        f"\nREMINDER: Today is {ctx['date']}. "
        "When the user says 'today' they mean this exact date. "
        "When they say 'yesterday' they mean the day before. "
        "Always use explicit full dates (e.g., 'Tuesday, March 24') in responses."
    )

    return "\n".join(prompt_parts)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, RateLimitError, APIStatusError)),
)
def _call_llm(system_prompt: str, history: list) -> str:
    """Call LLM with retry logic."""
    response = llm_client.chat.completions.create(
        model=HEROKU_MODEL, messages=[{"role": "system", "content": system_prompt}, *history], max_tokens=500
    )
    if not response.choices:
        logger.error("LLM returned empty choices array")
        raise ValueError("LLM returned no response choices")
    return response.choices[0].message.content


def chat(user_message: str, user_id: str = USER_ID) -> str:
    """Process user message and return coach response."""
    try:
        system_prompt = get_system_prompt(user_message, user_id=user_id)

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
            store_conversation(user_message, assistant_response, user_id=user_id)

        return assistant_response

    except Exception as e:
        logger.error(f"Chat error: {e}")
        return "I apologize, but I'm having trouble processing your request. Please try again."


def reset_session(user_id: str = USER_ID) -> None:
    """Clear session history (Mem0 memories preserved)."""
    clear_history(user_id)
