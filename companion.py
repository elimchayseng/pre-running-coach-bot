"""Coach agent — Heroku Inference + tool use + structured state.

Public API:
    chat(user_message: str) -> str    one user turn -> coach reply
    reset_session() -> None           clear short-term Redis history

Long-term context lives in state/ (see state_manager). Short-term
in-conversation history lives in Redis (see conversation_store).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from openai import APIStatusError, BadRequestError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import HEROKU_MODEL, PRE_PERSONALITY, llm_client, logger
from conversation_store import add_turn, clear_history, get_history
from pending_proposal_store import get_pending_plan_proposal
from state_manager import StateManager
from temporal_context import today_local
from tools import ALL_TOOLS, execute_tool

STATE_DIR = Path(__file__).resolve().parent / "state"
MAX_TOOL_LOOPS = 8

_state: Optional[StateManager] = None
_cache_control_supported: Optional[bool] = None  # learned at runtime


def _get_state() -> StateManager:
    global _state
    if _state is None:
        _state = StateManager(STATE_DIR)
    return _state


def build_system_prompt(state: StateManager) -> str:
    """Compose the system prompt: personality + voice rules + today + state blob + tool norms."""
    today = today_local()
    blob = state.load_full_context()
    sections = [
        f"You are PRE, an elite endurance coach. {PRE_PERSONALITY}",
        "",
        "=== VOICE & FORMAT ===",
        "- Match length to the question. 1-line question -> 1-2 line answer when possible.",
        "- Lead with the answer. Reasoning second, sparingly. One follow-up question max.",
        "- Tables ONLY when comparing 3+ items across 3+ columns (a week of daily prescriptions).",
        "  A list of 3 sessions is a list, not a table.",
        "- Bold ONE thing per response — the call to action or the number that matters.",
        "  Don't bold every cell or section header.",
        "- Never repeat the same content in two formats (list -> table -> summary).",
        "- Drop 'Bottom line:' labels. The bottom line is the last sentence.",
        "- Declaratives, not suggestions. 'Run 6:20 for the threshold reps' beats"
        " 'you might consider trying around 6:20'.",
        "",
        "=== TODAY ===",
        f"{today.isoformat()} ({today.strftime('%A')})",
        "This date is from the system clock and is ALWAYS correct. Never override it.",
        "",
        blob,
        "",
    ]

    proposal = _safe_get_pending_proposal()
    if proposal:
        sections.extend(
            [
                "=== PENDING PLAN PROPOSAL (awaiting user confirmation) ===",
                "A post-activity review proposed the following plan change. It has NOT been applied yet.",
                f"Summary: {proposal.get('summary', '')}",
                f"Reason: {proposal.get('reason', '')}",
                "Proposed new plan.md content:",
                "```markdown",
                (proposal.get("new_plan_md") or "").rstrip(),
                "```",
                "",
            ]
        )

    sections.extend(
        [
            "=== HOW TO USE TOOLS ===",
            "- Call get_today early to confirm date, next race, training phase.",
            "- Call get_todays_workout for 'what's my workout' — don't paraphrase the plan.",
            "- Call get_week_status when summarizing weekly progress or when the user asks",
            "  'how am I doing' / 'did I do X'. Render each day with ✅ (completed), ⏳ (today",
            "  or future), or ❌ (past + missed). Mention off_plan_actuals as 'off-plan' — do",
            "  NOT treat them as completing the prescription.",
            "- Call log_session IMMEDIATELY when the user reports a run/workout/race — don't ask first.",
            "- Call get_fitness_summary BEFORE adjusting zones or making non-trivial plan changes.",
            "- Call update_plan to modify a workout/week/block. Preserve the locked",
            "  '| Day | Date | Workout | Pace target | Notes |' table format for the current week.",
            "  For QUALITY sessions and races (workouts, long runs, races) in the current week,",
            "  also write a '#### YYYY-MM-DD' sub-section in plan.md with rationale, structure",
            "  breakdown (WU/work/CD), and execution cues. The Google Calendar event for that day",
            "  uses this prose verbatim — it's what the user reads on their phone the morning of.",
            "  Easy/recovery/rest days don't need a sub-section; they fall back to the table cells.",
            "- Call update_athlete to record new PRs, resolved injuries, zone updates.",
            "- Call append_journal IN THE SAME TURN whenever the user reports sleep, stress, travel,",
            "  illness, or other life context that should persist beyond this conversation.",
            "  Don't wait to be asked. Body text only — timestamp is added automatically.",
            "",
            "=== ADAPTATION NORMS ===",
            "- One quality session does not justify a zone change. Adjust on trends only.",
            "- When the user reports anything affecting readiness today, reassess today's prescription.",
            "  If you modify it, call update_plan with the change reason.",
            "- Priority A race is Broken Arrow 46K (June 20). Brooklyn is a tune-up — don't over-cook it.",
            "- Surface trade-offs explicitly. Don't silently change the plan; explain reasoning.",
            "- If a pending plan proposal is shown above and the user confirms ('yes', 'apply', 'do it'),"
            " call update_plan with the proposed new_plan_md and the proposal's reason as change_reason."
            " If they decline or ask to revise, do not apply it; either discard or propose your own"
            " revision via update_plan.",
        ]
    )

    return "\n".join(sections)


def _safe_get_pending_proposal():
    """Fetch the pending proposal, swallowing any Redis errors so the chat
    flow stays alive when the proposal store is unavailable."""
    try:
        return get_pending_plan_proposal()
    except Exception as e:
        logger.warning(f"Failed to load pending plan proposal: {e}")
        return None


def _build_system_message(prompt_text: str, use_cache_control: bool) -> dict:
    """Wrap the system prompt for the chat completions API.

    With cache_control: structured content list with an ephemeral cache marker
    (~90% cost reduction on hits within the cache TTL, IF Heroku passes the
    field through to Anthropic). Falls back to plain string if Heroku rejects
    the structured form.
    """
    if use_cache_control:
        return {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": prompt_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    return {"role": "system", "content": prompt_text}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, RateLimitError, APIStatusError)),
)
def _call_llm(messages: list[dict]):
    """Single LLM call with tools; retry wraps just this call."""
    response = llm_client.chat.completions.create(
        model=HEROKU_MODEL,
        messages=messages,
        tools=ALL_TOOLS,
        tool_choice="auto",
        max_tokens=2000,
    )
    if not response.choices:
        raise ValueError("LLM returned no response choices")
    return response.choices[0].message


def agent_turn(messages: list[dict], state: StateManager) -> str:
    """Run the tool-use loop for a single user turn.

    Mutates `messages` in place (appends assistant + tool messages).
    Returns the final assistant text content.
    """
    msg = None
    for _ in range(MAX_TOOL_LOOPS):
        msg = _call_llm(messages)

        msg_dict = msg.model_dump(exclude_none=True)
        # Heroku rejects null/empty content on assistant messages that carry
        # tool_calls. Force a non-empty placeholder when needed.
        if not msg_dict.get("content"):
            msg_dict["content"] = "(calling tools)"
        messages.append(msg_dict)

        if not msg.tool_calls:
            return msg.content or ""

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                result = execute_tool(tc.function.name, args, state)
            except json.JSONDecodeError as e:
                result = {"error": f"invalid JSON args: {e}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str),
                }
            )

    logger.warning("agent_turn hit tool-use iteration cap")
    return (msg.content if msg else "") or ""


def chat(user_message: str) -> str:
    """Process a single user message via the tool-use loop. Persists short-term
    history in Redis. Returns the final assistant text."""
    global _cache_control_supported
    try:
        state = _get_state()
        prompt_text = build_system_prompt(state)

        add_turn("user", user_message)
        history = get_history()

        # Probe cache_control on first call; fall back to plain string if Heroku rejects.
        if _cache_control_supported is None or _cache_control_supported:
            try:
                messages = [_build_system_message(prompt_text, True), *history]
                assistant = agent_turn(messages, state)
                _cache_control_supported = True
                add_turn("assistant", assistant)
                return assistant
            except BadRequestError as e:
                if _cache_control_supported is None and "content" in str(e).lower():
                    logger.warning("cache_control rejected by Heroku; falling back to plain string content")
                    _cache_control_supported = False
                else:
                    raise

        messages = [_build_system_message(prompt_text, False), *history]
        assistant = agent_turn(messages, state)
        add_turn("assistant", assistant)
        return assistant

    except Exception as e:
        logger.error(f"companion.chat failed: {e}", exc_info=True)
        return "I apologize, but I'm having trouble processing your request. Please try again."


def reset_session() -> None:
    """Clear short-term Redis history. State files are unchanged."""
    clear_history()
