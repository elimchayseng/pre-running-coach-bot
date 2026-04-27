"""CLI harness for the running coach agent.

Bypasses Telegram + Redis. Loads state from ./state/, talks to Claude via
the existing Heroku Inference client (config.llm_client), runs the
tool-use loop end-to-end. Use this to iterate on system prompt, tool
schemas, and adaptation behaviour before wiring into companion.py.

Special commands:
  /quit          exit
  /reset         clear in-memory chat history (state files unchanged)
  /state         dump load_full_context() (truncated)
  /tools         list registered tools
  /system        print the current system prompt
  /raw           toggle showing raw tool args/results inline

Run:
    venv/bin/python scripts/test_agent.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

# Allow running as `python scripts/test_agent.py` from repo root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import HEROKU_MODEL, PRE_PERSONALITY, llm_client, logger  # noqa: E402
from state_manager import StateManager  # noqa: E402
from tools import ALL_TOOLS, execute_tool  # noqa: E402

STATE_DIR = ROOT / "state"
MAX_TOOL_LOOPS = 8
HARNESS_BUILD = "v3-content-debug"  # bump on every harness change so we can see what's running


def build_system_prompt(state: StateManager) -> str:
    today = date.today()
    blob = state.load_full_context()
    return "\n".join([
        f"You are PRE, an elite marathon and ultra running coach. {PRE_PERSONALITY}",
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
        "- Declaratives, not suggestions. 'Run 6:20 for the threshold reps' beats 'you might consider trying around 6:20'.",
        "",
        "=== TODAY ===",
        f"{today.isoformat()} ({today.strftime('%A')})",
        "This date is from the system clock and is ALWAYS correct. Never override it.",
        "",
        blob,
        "",
        "=== HOW TO USE TOOLS ===",
        "- Call get_today early to confirm date, next race, training phase.",
        "- Call get_todays_workout for 'what's my workout' — don't paraphrase the plan.",
        "- Call log_session IMMEDIATELY when the user reports a run/workout/race — don't ask first.",
        "- Call get_fitness_summary BEFORE adjusting zones or making non-trivial plan changes.",
        "- Call update_plan to modify a workout/week/block. Preserve the locked",
        "  '| Day | Date | Workout | Pace target | Notes |' table format for the current week.",
        "- Call update_athlete to record new PRs, resolved injuries, zone updates.",
        "- Call append_journal for life context, mood, decision rationale.",
        "",
        "=== ADAPTATION NORMS ===",
        "- One quality session does not justify a zone change. Adjust on trends only.",
        "- When the user reports anything affecting readiness (sleep, travel, illness, soreness,",
        "  weather), reassess today's prescription. If you modify it, call update_plan and",
        "  include the change reason.",
        "- Priority A race is Broken Arrow 46K (June 20). Brooklyn is a tune-up — don't over-cook it.",
        "- Surface trade-offs explicitly. Don't silently change the plan; explain reasoning.",
    ])


def _summarize(value, max_len: int = 120) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= max_len else s[:max_len - 1] + "…"


def chat_loop() -> None:
    state = StateManager(STATE_DIR)
    history: list[dict] = []
    show_raw = False

    print(f"PRE coach harness — model={HEROKU_MODEL}, state_dir={STATE_DIR}")
    print(f"Loaded {len(ALL_TOOLS)} tools. Type /quit to exit, /tools to list, /state to dump state.")

    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not user:
            continue

        if user == "/quit":
            return
        if user == "/reset":
            history = []
            print("(history cleared)")
            continue
        if user == "/state":
            blob = state.load_full_context()
            print(blob[:3000] + ("\n…[truncated]" if len(blob) > 3000 else ""))
            continue
        if user == "/tools":
            for t in ALL_TOOLS:
                fn = t["function"]
                print(f"  {fn['name']}: {fn['description'][:90]}…")
            continue
        if user == "/system":
            print(build_system_prompt(state))
            continue
        if user == "/raw":
            show_raw = not show_raw
            print(f"raw mode: {'ON' if show_raw else 'OFF'}")
            continue

        history.append({"role": "user", "content": user})
        system_msg = {"role": "system", "content": build_system_prompt(state)}
        messages = [system_msg, *history]

        try:
            assistant = run_agent_turn(messages, state, show_raw)
        except Exception as e:
            logger.exception("agent turn failed")
            print(f"\n[error: {type(e).__name__}: {e}]")
            history.pop()  # don't keep the user msg if we crashed
            continue

        history.append({"role": "assistant", "content": assistant})
        print(f"\npre> {assistant}")


def run_agent_turn(messages: list[dict], state: StateManager, show_raw: bool) -> str:
    """Run the tool-use loop for a single user turn. Returns final assistant text."""
    print(f"  [harness build: {HARNESS_BUILD}]")
    msg = None
    for i in range(MAX_TOOL_LOOPS):
        try:
            response = llm_client.chat.completions.create(
                model=HEROKU_MODEL,
                messages=messages,
                tools=ALL_TOOLS,
                tool_choice="auto",
                max_tokens=2000,
            )
        except Exception:
            # Dump the full message list so we can see exactly what Heroku rejected.
            print("\n  [LLM call failed — dumping messages payload]")
            for idx, m in enumerate(messages):
                role = m.get("role")
                keys = list(m.keys())
                content_repr = repr(m.get("content"))[:120]
                tc = m.get("tool_calls")
                tc_summary = f"tool_calls={len(tc)}" if tc else "tool_calls=0"
                print(f"    [{idx}] role={role} keys={keys} content={content_repr} {tc_summary}")
            raise

        msg = response.choices[0].message
        # Heroku Inference rejects null OR empty `content` on assistant messages
        # that carry tool_calls. Force a non-empty placeholder.
        msg_dict = msg.model_dump(exclude_none=True)
        if not msg_dict.get("content"):
            msg_dict["content"] = "(calling tools)"
        messages.append(msg_dict)
        if show_raw:
            print(f"    [appended assistant msg keys: {list(msg_dict.keys())}, "
                  f"content={msg_dict.get('content')!r}, "
                  f"tool_calls={len(msg_dict.get('tool_calls') or [])}]")

        if not msg.tool_calls:
            return msg.content or ""

        names = [tc.function.name for tc in msg.tool_calls]
        print(f"  [iter {i + 1}: tool calls -> {', '.join(names)}]")

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError as e:
                args = {}
                result = {"error": f"invalid JSON args: {e}"}
            else:
                result = execute_tool(tc.function.name, args, state)

            if show_raw:
                print(f"    args:   {_summarize(args, 200)}")
                print(f"    result: {_summarize(result, 300)}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    print("  [hit iteration cap; returning last assistant content]")
    return (msg.content if msg else "") or ""


if __name__ == "__main__":
    if llm_client is None:
        print("llm_client is None — check HEROKU_INFERENCE_URL / HEROKU_INFERENCE_KEY in .env")
        sys.exit(1)
    chat_loop()
