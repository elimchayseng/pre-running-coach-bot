"""Google Calendar tools: push the weekly plan table to the user's
PRE Training calendar.

Use sparingly: call sync_plan_to_calendar at most ONCE per turn, at the very
end, after all plan edits are final. update_plan can fire many times during a
single turn while the agent narrows in on the right plan; calling sync after
each one would write-amplify gcal patches the user notices on their phone.
"""

from __future__ import annotations

import os

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "sync_plan_to_calendar",
            "description": (
                "Push the current plan.md weekly table to the user's PRE "
                "Training Google Calendar as all-day events. Call ONCE per "
                "turn, at the END, after any update_plan edits are final — "
                "not after every intermediate edit. Returns "
                "{inserted, patched, deleted, unchanged, errors}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, report planned ops without making any API writes",
                        "default": False,
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_status",
            "description": (
                "Whether the calendar integration is authorized and what the "
                "last sync looked like. Use when the user asks about calendar "
                "sync state."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _sync(args: dict, state) -> dict:
    from google_calendar import sync

    return sync.sync_plan(state, dry_run=bool(args.get("dry_run", False)))


def _status(args: dict, state) -> dict:
    from google_calendar import auth, sync

    out: dict = {
        "backend": auth._backend(),
        "calendar_id_set": bool(os.getenv("CALENDAR_ID")),
        "authorized": False,
    }
    try:
        tokens = auth._read_tokens()
        if tokens and "refresh_token" in tokens:
            out["authorized"] = True
    except auth.TokenStorageUnavailable as e:
        out["storage_error"] = str(e)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    summary = sync.get_last_sync_summary()
    if summary:
        out["last_sync"] = summary
    return out


HANDLERS = {
    "sync_plan_to_calendar": _sync,
    "get_calendar_status": _status,
}
