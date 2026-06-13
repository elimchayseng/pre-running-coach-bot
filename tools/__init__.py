"""Tool definitions for the running coach agent.

Tools are registered as OpenAI-compatible function schemas (which Heroku
Inference's gateway accepts and Claude executes). Each handler takes a dict
of arguments and a StateManager, and returns a JSON-serializable dict.

Usage:
    from tools import ALL_TOOLS, execute_tool
    response = llm_client.chat.completions.create(
        model=..., messages=..., tools=ALL_TOOLS, tool_choice="auto",
    )
    for tool_call in response.choices[0].message.tool_calls or []:
        result = execute_tool(tool_call.function.name, args, state)
"""

from __future__ import annotations

import logging
from typing import Any

from . import calendar, fitness, health, plan, state

logger = logging.getLogger("pre_coach.tools")

ALL_TOOLS: list[dict] = state.SCHEMAS + plan.SCHEMAS + fitness.SCHEMAS + calendar.SCHEMAS + health.SCHEMAS

_HANDLERS: dict[str, Any] = {
    **state.HANDLERS,
    **plan.HANDLERS,
    **fitness.HANDLERS,
    **calendar.HANDLERS,
    **health.HANDLERS,
}


def execute_tool(name: str, args: dict, state_manager) -> dict:
    """Dispatch a tool call by name. Returns a JSON-serializable result.

    Errors are returned as {"error": "..."} rather than raised, so the
    agent can read the error and recover rather than crashing the loop.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        logger.warning("unknown tool: %s", name)
        return {"error": f"unknown tool: {name}"}
    try:
        return handler(args, state_manager)
    except Exception as e:  # noqa: BLE001 — surface to agent
        logger.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


__all__ = ["ALL_TOOLS", "execute_tool"]
