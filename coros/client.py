"""COROS MCP client: call tools on the official COROS MCP server.

Transport is MCP streamable HTTP (the `mcp` SDK) with a Bearer token from
coros.auth. Each call opens a short-lived session — the nightly pull makes a
handful of tool calls once a day, so connection reuse isn't worth the
complexity of keeping an event loop alive in the scheduler thread.

Tool outputs are human-readable TEXT (often JSON-string-wrapped — see
docs/coros-mcp.md); this module returns them raw. Parsing lives in
coros/translator.py so a future transport swap can't ripple past this file.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from coros.auth import CorosAuthError, get_access_token

logger = logging.getLogger("pre_coach.coros.client")

MCP_URL = os.getenv("COROS_MCP_URL", "https://mcpus.coros.com/mcp")

# The daily-pull tool set. queryRecoveryStatus is point-in-time (no history);
# the rest accept a `days` window. Stress avg rides along in
# queryDailyHealthData, so queryStressLevel isn't pulled nightly.
BUNDLE_TOOLS = (
    "queryDailyHealthData",
    "querySleepData",
    "queryHrvAssessment",
    "queryRestingHeartRate",
    "queryTrainingLoadAssessment",
    "queryRecoveryStatus",
)


def _default_timezone() -> str:
    return os.getenv("USER_TIMEZONE") or "UTC"


def _bundle_args(tool: str, days: int, timezone: str) -> dict:
    """Arguments for each bundle tool. The MCP schemas mark days/timezone
    required even where the server tolerates defaults, so always send them."""
    if tool == "queryRecoveryStatus":
        return {}
    if tool == "queryTrainingLoadAssessment":
        return {"days": days}
    if tool == "querySleepData":
        return {"days": days, "timezone": timezone, "startDate": "", "endDate": ""}
    return {"days": days, "timezone": timezone}


async def _call_tool_async(name: str, arguments: dict, access_token: str) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {access_token}"}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if getattr(result, "isError", False):
                texts = [c.text for c in result.content if getattr(c, "text", None)]
                raise CorosToolError(f"{name} returned an error: {' '.join(texts)[:300]}")
            return "\n".join(c.text for c in result.content if getattr(c, "text", None))


class CorosToolError(RuntimeError):
    """A tool call reached the server but came back as an MCP-level error."""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    reraise=True,
)
def call_tool_text(name: str, arguments: Optional[dict] = None) -> str:
    """Call one MCP tool and return its concatenated text content.

    Sync wrapper: runs a private event loop per call, safe inside the
    scheduler's daemon thread (never on a request path). Raises CorosAuthError
    when no working access token is available (watchdog classifies that as
    needs_auth rather than infra).
    """
    token = get_access_token()
    return asyncio.run(_call_tool_async(name, arguments or {}, token))


def fetch_daily_bundle(days: int = 7, timezone: Optional[str] = None) -> dict[str, str]:
    """Fetch the nightly tool set, returning {tool_name: raw_text}.

    Individual tool failures are logged and skipped so one flaky endpoint
    doesn't kill the whole pull — EXCEPT auth failures, which abort
    immediately (every subsequent call would fail the same way).
    """
    tz = timezone or _default_timezone()
    bundle: dict[str, str] = {}
    for tool in BUNDLE_TOOLS:
        try:
            bundle[tool] = call_tool_text(tool, _bundle_args(tool, days, tz))
        except CorosAuthError:
            raise
        except Exception as e:  # noqa: BLE001 — per-tool isolation is the point
            logger.warning(f"COROS bundle tool {tool} failed: {e}")
    return bundle


def query_user_info() -> str:
    """Cheap authenticated round-trip used by status checks and the watchdog's
    auth classification."""
    return call_tool_text("queryUserInfo", {})
