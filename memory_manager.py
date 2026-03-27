import re
from datetime import date, datetime, timedelta
from typing import Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import logger, mem0_client

USER_ID = "runner"
AGENT_ID = "pre_coach"

# Memory truncation limit (tokens ~ chars/4)
MAX_MEMORY_CHARS = 400  # ~100 tokens per memory

# Day abbreviation to full name mapping
_DAY_ABBREVS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
                "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}

# Weekly plan: higher truncation limit since plans are structured data
MAX_PLAN_CHARS = 800


def _truncate_memory(text: str, max_chars: int = MAX_MEMORY_CHARS) -> str:
    """Truncate long memory text to save tokens."""
    if not text or len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
)
def _mem0_search(query: str, limit: int = 3, user_id: str = USER_ID) -> list:
    """Mem0 search with retry logic."""
    try:
        result = mem0_client.search(query=query, user_id=user_id, limit=limit)
        if result is None:
            return []
        return result.get("results", []) if isinstance(result, dict) else (result or [])
    except Exception as e:
        logger.error(f"Mem0 search failed: {e}")
        return []


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
)
def _mem0_add(messages: list, user_id: str = USER_ID, metadata: dict = None, agent_id: str = None) -> None:
    """Mem0 add with retry logic."""
    try:
        kwargs = {}
        if user_id:
            kwargs["user_id"] = user_id
        if agent_id:
            kwargs["agent_id"] = agent_id
        if metadata:
            kwargs["metadata"] = metadata
        mem0_client.add(messages, output_format="v1.1", **kwargs)
    except Exception as e:
        logger.error(f"Mem0 add failed: {e}")


# Cache for repeated queries (60 second TTL simulated via lru_cache)
_query_cache = {}
_cache_timestamps = {}
CACHE_TTL_SECONDS = 60


def _get_cached_search(query: str, limit: int = 3, user_id: str = USER_ID) -> list:
    """Search with simple TTL cache."""
    cache_key = f"{user_id}:{query}:{limit}"
    now = datetime.now()

    if cache_key in _query_cache:
        cached_time = _cache_timestamps.get(cache_key)
        if cached_time and (now - cached_time).total_seconds() < CACHE_TTL_SECONDS:
            return _query_cache[cache_key]

    result = _mem0_search(query, limit, user_id=user_id)
    _query_cache[cache_key] = result
    _cache_timestamps[cache_key] = now
    return result


def resolve_temporal_references(query: str) -> str:
    """Resolve temporal words (today, yesterday, this week, day names) to explicit dates for better mem0 search."""
    from temporal_context import get_temporal_context, now_local, resolve_day_name_to_date, today_local

    lower = query.lower()

    # Check for explicit day names (Monday, Tuesday, etc.)
    _day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    has_day_name = any(d in lower for d in _day_names)

    has_temporal = has_day_name or any(kw in lower for kw in [
        "today", "this morning", "this evening", "this afternoon",
        "yesterday", "this week", "last week", "tomorrow",
    ])
    if not has_temporal:
        return query

    ctx = get_temporal_context()
    today = today_local()

    additions = []
    if any(kw in lower for kw in ["today", "this morning", "this evening", "this afternoon"]):
        additions.append(f"[Date: {ctx['date']}]")
    if "yesterday" in lower:
        yesterday = today - timedelta(days=1)
        additions.append(f"[Date: {yesterday.strftime('%A, %B %d, %Y')}]")
    if "tomorrow" in lower:
        tomorrow = today + timedelta(days=1)
        additions.append(f"[Date: {tomorrow.strftime('%A, %B %d, %Y')}]")
    if "this week" in lower:
        week_start = today - timedelta(days=today.weekday())  # Monday
        week_end = week_start + timedelta(days=6)  # Sunday
        additions.append(f"[Week: {week_start.strftime('%B %d')} - {week_end.strftime('%B %d, %Y')}]")
    if "last week" in lower:
        last_week_start = today - timedelta(days=today.weekday() + 7)
        last_week_end = last_week_start + timedelta(days=6)
        additions.append(f"[Week: {last_week_start.strftime('%B %d')} - {last_week_end.strftime('%B %d, %Y')}]")

    # Resolve explicit day names: "how was my Monday?" → closest past Monday
    # Use past for report-like queries, future for planning-like queries
    if has_day_name:
        planning_keywords = ["plan", "schedule", "what's", "whats", "what is", "upcoming", "next"]
        intent = "future" if any(kw in lower for kw in planning_keywords) else "past"
        for day in _day_names:
            if day in lower:
                resolved = resolve_day_name_to_date(day, intent=intent)
                additions.append(f"[{day.capitalize()}: {resolved.strftime('%A, %B %d, %Y')}]")

    if additions:
        return f"{query} {' '.join(additions)}"
    return query


def _resolve_workout_date(message: str) -> str:
    """Resolve temporal references in a workout report to an ISO date string."""
    from temporal_context import resolve_day_name_to_date, today_local

    lower = message.lower()
    today = today_local()
    if "yesterday" in lower:
        return (today - timedelta(days=1)).isoformat()

    # Check for explicit day names: "my Monday run", "Saturday's long run"
    _day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for day in _day_names:
        if day in lower:
            resolved = resolve_day_name_to_date(day, intent="past")
            return resolved.isoformat()

    # Default: today (covers "this morning", "today", or no temporal reference)
    return today.isoformat()


def _detect_workout_report(message: str) -> bool:
    """Detect if a message is reporting a completed workout."""
    lower = message.lower()
    workout_keywords = ["ran", "run ", "miles", "mi ", "workout", "tempo", "intervals",
                        "long run", "easy run", "recovery run", "splits", "pace"]
    return sum(1 for kw in workout_keywords if kw in lower) >= 2


def _detect_weekly_plan(text: str) -> bool:
    """Detect if text contains a weekly training plan (markdown table format)."""
    lower = text.lower()
    # Must have multiple day references in table-like format
    day_pattern = re.compile(r'\b(mon|tue|wed|thu|fri|sat|sun)\b', re.IGNORECASE)
    day_matches = day_pattern.findall(lower)
    has_table = "|" in text
    has_plan_keywords = any(kw in lower for kw in ["week", "plan", "schedule", "target"])
    return len(day_matches) >= 4 and has_table and has_plan_keywords


def store_conversation(user_message: str, assistant_response: str, user_id: str = USER_ID) -> None:
    """Store a conversation exchange with temporal metadata."""
    from temporal_context import get_temporal_context, today_local

    ctx = get_temporal_context()
    today = today_local()

    dated_user_msg = f"[{ctx['date']}] {user_message}"
    dated_assistant_msg = f"[{ctx['date']}] {assistant_response}"

    metadata = {
        "stored_date": today.isoformat(),
        "training_phase": ctx["training_phase"],
        "days_to_race": ctx["days_to_race"],
    }

    # Detect and tag workout reports
    if _detect_workout_report(user_message):
        workout_date = _resolve_workout_date(user_message)
        metadata["type"] = "workout_log"
        metadata["workout_date"] = workout_date
        metadata["day_of_week"] = date.fromisoformat(workout_date).strftime("%A")

    messages = [{"role": "user", "content": dated_user_msg}, {"role": "assistant", "content": dated_assistant_msg}]
    _mem0_add(messages, user_id=user_id, metadata=metadata)

    # Auto-detect and store weekly plans from assistant responses
    if _detect_weekly_plan(assistant_response) and len(assistant_response) > 200:
        store_weekly_plan(assistant_response, user_id=user_id)


def retrieve_context_and_constraints(query: str, limit: int = 3, user_id: str = USER_ID) -> tuple[str, str]:
    """Combined retrieval for context and constraints (reduces API calls from 2 to 1+filtering)."""
    # Enrich temporal references before searching
    enriched_query = resolve_temporal_references(query)
    combined_query = f"{enriched_query} injury limitation constraint recovery"
    memories = _get_cached_search(combined_query, limit=limit + 2, user_id=user_id)

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
            kw in memory_text.lower() for kw in ["injury", "pain", "sore", "limit", "constraint", "recovery"]
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

    _mem0_add([{"role": "system", "content": PRE_PERSONALITY}], user_id=None, agent_id=AGENT_ID)


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

    _mem0_add([{"role": "user", "content": f"My marathon goal is: {goal_description}"}], user_id=USER_ID)


def store_injury(description: str, days_until_recovery: int = 14) -> None:
    """Store injury with automatic expiration."""
    expiration = (datetime.now() + timedelta(days=days_until_recovery)).strftime("%Y-%m-%d")
    _mem0_add(
        [{"role": "user", "content": f"Injury: {description}"}],
        user_id=USER_ID,
        metadata={"type": "injury", "expiration_date": expiration},
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
        metadata={"type": "race_date", "date": race_date},
    )


def get_race_date() -> Optional[str]:
    """Retrieve race date from memory."""
    memories = _get_cached_search("target race date", limit=1)
    if not memories or memories[0] is None:
        return None
    return memories[0].get("memory")


def store_weekly_plan(plan_text: str, week_start: date = None, user_id: str = USER_ID) -> None:
    """Store a weekly training plan with structured metadata for reliable retrieval.

    Includes a created_at timestamp so retrieve_weekly_plan can pick the latest
    version if the plan is updated mid-week.
    """
    from temporal_context import now_local, today_local

    today = today_local()
    if week_start is None:
        week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday

    # Build a structured header so the stored text has explicit date context
    header = (
        f"WEEKLY TRAINING PLAN: {week_start.strftime('%A %B %d')} - "
        f"{week_end.strftime('%A %B %d, %Y')}"
    )
    structured_plan = f"{header}\n{plan_text}"

    _mem0_add(
        [{"role": "assistant", "content": structured_plan}],
        user_id=user_id,
        metadata={
            "type": "weekly_plan",
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "stored_date": today.isoformat(),
            "created_at": now_local().isoformat(),
        },
    )
    logger.info(f"Stored weekly plan for {week_start.isoformat()} - {week_end.isoformat()}")


def retrieve_weekly_plan(user_id: str = USER_ID) -> str:
    """Retrieve the current week's training plan from mem0.

    If multiple versions exist (mid-week update), returns the most recently created one
    based on the created_at metadata timestamp.
    """
    from temporal_context import today_local

    today = today_local()
    week_start = today - timedelta(days=today.weekday())  # Monday
    search_query = f"weekly training plan schedule week of {week_start.strftime('%B %d')}"

    # Try filtered search first (metadata-based)
    try:
        result = mem0_client.search(
            query=search_query,
            user_id=user_id,
            limit=5,
            filters={"type": "weekly_plan"},
        )
        if result is None:
            memories = []
        elif isinstance(result, dict):
            memories = result.get("results", [])
        else:
            memories = result or []
    except Exception as e:
        logger.warning(f"Filtered weekly plan search failed (falling back): {e}")
        memories = []

    # Fallback: unfiltered semantic search
    if not memories:
        memories = _get_cached_search(search_query, limit=3, user_id=user_id)

    if not memories:
        return ""

    # Pick the most recently created plan (handles mid-week updates)
    best_text = ""
    best_created_at = None
    for mem in memories:
        if mem is None:
            continue
        text = mem.get("memory", "")
        if not text:
            continue
        metadata = mem.get("metadata") or {}
        created_at = metadata.get("created_at", metadata.get("stored_date", ""))
        if best_created_at is None or created_at > best_created_at:
            best_created_at = created_at
            best_text = text

    if best_text:
        return best_text[:MAX_PLAN_CHARS] if len(best_text) > MAX_PLAN_CHARS else best_text
    return ""


def clear_cache() -> None:
    """Clear the query cache (useful for testing or forced refresh)."""
    global _query_cache, _cache_timestamps
    _query_cache = {}
    _cache_timestamps = {}
