"""CLI harness for the running coach agent.

Bypasses Telegram and Redis. Loads state from ./state/, talks to Claude
via the existing Heroku Inference client, runs the same tool-use loop
companion.py uses in production. Use this to iterate on system prompt,
tool schemas, and adaptation behaviour without touching the bot.

Special commands:
  /quit     exit
  /reset    clear in-memory chat history (state files unchanged)
  /state    dump load_full_context() (truncated)
  /tools    list registered tools
  /system   print the current system prompt
  /raw      toggle showing raw tool args/results inline

Run:
    venv/bin/python scripts/test_agent.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python scripts/test_agent.py` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion import agent_turn, build_system_prompt  # noqa: E402
from config import HEROKU_MODEL, llm_client, logger  # noqa: E402
from state_manager import StateManager  # noqa: E402
from tools import ALL_TOOLS  # noqa: E402

STATE_DIR = ROOT / "state"


def _summarize(value, max_len: int = 120) -> str:
    s = json.dumps(value, default=str) if not isinstance(value, str) else value
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def chat_loop() -> None:
    state = StateManager(STATE_DIR)
    history: list[dict] = []  # in-memory only — no Redis dependency for the harness
    show_raw = False

    print(f"PRE coach harness — model={HEROKU_MODEL}, state_dir={STATE_DIR}")
    print(f"Loaded {len(ALL_TOOLS)} tools. /quit to exit, /tools, /state, /system, /raw, /reset.")

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
            assistant = agent_turn(messages, state)
        except Exception as e:
            logger.exception("agent_turn failed")
            print(f"\n[error: {type(e).__name__}: {e}]")
            history.pop()  # don't keep the user msg if we crashed
            continue

        if show_raw:
            for idx in range(len(history), len(messages)):
                m = messages[idx]
                role = m.get("role")
                keys = list(m.keys())
                content_repr = repr(m.get("content"))[:160]
                tc = m.get("tool_calls")
                tc_summary = f"tool_calls={len(tc)}" if tc else ""
                print(f"  [{idx}] role={role} keys={keys} content={content_repr} {tc_summary}")

        history.append({"role": "assistant", "content": assistant})
        print(f"\npre> {assistant}")


if __name__ == "__main__":
    if llm_client is None:
        print("llm_client is None — check HEROKU_INFERENCE_URL / HEROKU_INFERENCE_KEY in .env")
        sys.exit(1)
    chat_loop()
