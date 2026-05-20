"""Coach agent — Heroku Inference + tool use + structured state.

Public API:
    chat(user_message: str) -> str    one user turn -> coach reply
    reset_session() -> None           clear short-term Redis history

Long-term context lives in state/ (see state_manager). Short-term
in-conversation history lives in Redis (see conversation_store).
"""

from __future__ import annotations

import json
import threading
import time
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
from tools.state import flush_pending_calendar_sync

STATE_DIR = Path(__file__).resolve().parent / "state"
MAX_TOOL_LOOPS = 8

_state: Optional[StateManager] = None
_cache_control_supported: Optional[bool] = None  # learned at runtime

# Serializes companion.chat across background threads. /webhook now dispatches
# each Telegram update to a daemon thread (see app.webhook); without this lock,
# two concurrent updates would race on the Redis session:history key (a
# read-modify-write pair) and on SQLite plan writes performed by tools. The
# original single-worker gunicorn config relied on the worker timeout as the
# implicit serialization governor; this lock restores that guarantee.
_chat_lock = threading.Lock()


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
            "- Plan edits — pick the smallest tool that does the job:",
            "  - update_workout(date, ...): default for single-day edits. Patch only the cells",
            "    you want to change (workout, pace_target, notes), and pass detail_body for the",
            "    per-day '#### YYYY-MM-DD' prose if it's a quality session, long run, or race.",
            "    Easy/recovery/rest days don't need detail_body. The Google Calendar event for",
            "    that day uses detail_body verbatim — it's what the user reads on their phone",
            "    the morning of.",
            "  - replace_week_table(rows, ...): use for block / phase transitions when most rows",
            "    in the week change together. Detail sections are preserved; call update_workout",
            "    afterwards for any NEW quality sessions that need detail prose.",
            "  - update_plan(new_plan_markdown, ...): ESCAPE HATCH only. Use when applying a",
            "    pending plan proposal verbatim, restructuring non-table sections, or creating",
            "    a plan from scratch. Preserve the locked",
            "    '| Day | Date | Workout | Pace target | Notes |' table format — update_plan",
            "    parses that table into the plan's workout rows.",
            "- Call update_athlete to record new PRs, resolved injuries, zone updates.",
            "- Call append_journal IN THE SAME TURN whenever the user reports sleep, stress, travel,",
            "  illness, or other life context that should persist beyond this conversation.",
            "  Don't wait to be asked. Body text only — timestamp is added automatically.",
            "",
            "=== ADAPTATION NORMS ===",
            "- One quality session does not justify a zone change. Adjust on trends only.",
            "- When the user reports anything affecting readiness today, reassess today's prescription.",
            "  If you modify it, call update_workout with the change reason (single-day edit).",
            "- Priority A race is Broken Arrow 46K (June 20). Brooklyn is a tune-up — don't over-cook it.",
            "- Surface trade-offs explicitly. Don't silently change the plan; explain reasoning.",
            "- If a pending plan proposal is shown above and the user confirms ('yes', 'apply', 'do it'),"
            " call update_plan with the proposed new_plan_md and the proposal's reason as change_reason"
            " (proposal-apply is the escape-hatch case for full-plan writes). If they decline or ask"
            " to revise, do not apply it; either discard or propose your own revision via update_workout"
            " or update_plan as appropriate.",
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


def _is_cache_control_rejection(exc: BadRequestError) -> bool:
    """True iff the BadRequestError is the specific provider rejection of the
    cache_control field on system messages — distinct from other 400s.

    The previous form (`"content" in str(e).lower()`) over-matched: any
    BadRequestError mentioning "content" (e.g. message-shape validation
    errors) would silently flip _cache_control_supported and disable caching
    permanently. Match on the literal `cache_control` token instead, in
    whichever of body/response carries the structured error.
    """
    body = getattr(exc, "body", None) or {}
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") or ""
        if "cache_control" in msg.lower():
            return True
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            text = resp.text or ""
        except Exception:
            text = ""
        if "cache_control" in text.lower():
            return True
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, RateLimitError, APIStatusError)),
)
def _call_llm(messages: list[dict]):
    """Single LLM call with tools; retry wraps just this call."""
    t_start = time.perf_counter()
    response = llm_client.chat.completions.create(
        model=HEROKU_MODEL,
        messages=messages,
        tools=ALL_TOOLS,
        tool_choice="auto",
        max_tokens=4096,
    )
    if not response.choices:
        # Heroku Inference has been observed returning 200 + empty choices on
        # heavy plan-edit turns (issue #17). Capture the safe diagnostic
        # fields — elapsed time, id, and token counts only. elapsed_ms is
        # the load-bearing field: it tells us whether the failure clusters
        # at a fixed deadline (Heroku/upstream cap) or varies (transient).
        # We deliberately do NOT log the raw response object: some providers
        # echo prompt fragments in error bodies, which would leak athlete
        # content into Railway logs.
        elapsed_ms = int((time.perf_counter() - t_start) * 1000)
        logger.error(
            "LLM returned no response choices. elapsed_ms=%d id=%s usage=%s",
            elapsed_ms,
            getattr(response, "id", None),
            getattr(response, "usage", None),
        )
        raise ValueError("LLM returned no response choices")
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        # Output cap hit — surface it so we can tell apart "model decided to
        # stop" from "we truncated it." Plan edits emit long tool-call args
        # and were the original max_tokens=2000 trigger for issue #17.
        logger.warning(
            "LLM hit max_tokens cap (finish_reason=length). id=%s usage=%s",
            getattr(response, "id", None),
            getattr(response, "usage", None),
        )
    return choice.message


def agent_turn(messages: list[dict], state: StateManager) -> str:
    """Run the tool-use loop for a single user turn.

    Mutates `messages` in place (appends assistant + tool messages).
    Returns the final assistant text content.

    Per-iteration timing is logged at INFO so production logs answer issue
    #17's open question — whether a slow plan-edit turn is one slow LLM
    call or many short ones. That data informs whether the patch-style
    update_plan refactor (PR B) actually targets the bottleneck.
    """
    msg = None
    try:
        for i in range(MAX_TOOL_LOOPS):
            t_llm = time.perf_counter()
            msg = _call_llm(messages)
            llm_ms = int((time.perf_counter() - t_llm) * 1000)
            n_tool_calls = len(msg.tool_calls) if msg.tool_calls else 0
            logger.info("agent_turn iter=%d llm_ms=%d tool_calls=%d", i, llm_ms, n_tool_calls)

            msg_dict = msg.model_dump(exclude_none=True)
            # Heroku rejects null/empty content on assistant messages that carry
            # tool_calls. Force a non-empty placeholder when needed.
            if not msg_dict.get("content"):
                msg_dict["content"] = "(calling tools)"
            messages.append(msg_dict)

            if not msg.tool_calls:
                return msg.content or ""

            for tc in msg.tool_calls:
                t_tool = time.perf_counter()
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    result = execute_tool(tc.function.name, args, state)
                except json.JSONDecodeError as e:
                    result = {"error": f"invalid JSON args: {e}"}
                tool_ms = int((time.perf_counter() - t_tool) * 1000)
                logger.info("agent_turn iter=%d tool=%s tool_ms=%d", i, tc.function.name, tool_ms)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        logger.warning("agent_turn hit tool-use iteration cap")
        return (msg.content if msg else "") or ""
    finally:
        # End-of-turn calendar sync (issue #26): plan-edit tools set a dirty
        # flag; this fires at most one fire-and-forget daemon-thread sync per
        # turn regardless of how many edits happened. Always runs — even on
        # exception — so a mid-turn LLM failure that left edits on disk still
        # propagates to gcal on the next clean turn. Never raises.
        try:
            flush_pending_calendar_sync(state)
        except Exception:
            logger.exception("flush_pending_calendar_sync raised — swallowing")


def chat(user_message: str) -> str:
    """Process a single user message via the tool-use loop.

    Short-term Redis history is persisted ONLY after a successful LLM
    response, so a failure (or, rarely, a Telegram-driven retry of the
    same update_id) doesn't pollute the conversation with duplicate user
    turns. See issue #15.

    NOTE: this only covers conversational history. If the LLM succeeds on
    early tool-use iterations and fails later, any tools that mutated
    state/* or the calendar have already landed. A retry of the same
    update would therefore re-execute those tools. With the webhook now
    ack'ing 200 immediately, Telegram retries should be rare; full
    tool-side idempotency is tracked as a separate follow-up.

    The body runs under _chat_lock so concurrent webhook updates can't
    race on Redis history or SQLite plan writes. The lock spans the whole
    function — including the Redis read-modify-write pair — because the
    history hazard is in get_history()/add_turn(), not just agent_turn().
    """
    global _cache_control_supported
    with _chat_lock:
        try:
            state = _get_state()
            prompt_text = build_system_prompt(state)

            history = get_history()
            # The new user turn rides along in the prompt but isn't written to
            # Redis until agent_turn succeeds. If the LLM raises, history stays
            # at its pre-call shape and the next attempt sees a clean prefix.
            new_user_turn = {"role": "user", "content": user_message}

            # Probe cache_control on first call; fall back to plain string if Heroku rejects.
            if _cache_control_supported is None or _cache_control_supported:
                try:
                    messages = [_build_system_message(prompt_text, True), *history, new_user_turn]
                    assistant = agent_turn(messages, state)
                    _cache_control_supported = True
                    add_turn("user", user_message)
                    add_turn("assistant", assistant)
                    return assistant
                except BadRequestError as e:
                    if _cache_control_supported is None and _is_cache_control_rejection(e):
                        logger.warning("cache_control rejected by provider; falling back to plain string content")
                        _cache_control_supported = False
                    else:
                        raise

            messages = [_build_system_message(prompt_text, False), *history, new_user_turn]
            assistant = agent_turn(messages, state)
            add_turn("user", user_message)
            add_turn("assistant", assistant)
            return assistant

        except Exception as e:
            logger.error(f"companion.chat failed: {e}", exc_info=True)
            return "I apologize, but I'm having trouble processing your request. Please try again."


def reset_session() -> None:
    """Clear short-term Redis history. State files are unchanged."""
    clear_history()
