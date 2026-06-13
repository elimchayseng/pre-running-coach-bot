"""Soft-touch wearable-health summary tool.

Surfaces the athlete's COROS daily readiness metrics (sleep, HRV, resting HR,
stress, recovery, training load) on request, over an arbitrary trailing window.
The data already lands in the system prompt as a fixed 7-day READINESS table;
this tool advertises the capability to the agent and supports any window
('this week', 'last month') plus a graceful 'last synced X' empty state so the
bot never falsely denies having a COROS integration.

Reuses StateManager read functions — no new query logic. Does NOT prescribe;
the agent interprets the numbers.
"""

from __future__ import annotations

from temporal_context import today_local

MIN_WINDOW = 1
MAX_WINDOW = 90  # cap so "this month" works without unbounded scans

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_health_summary",
            "description": (
                "Pull the athlete's COROS wearable readiness metrics (sleep, "
                "HRV vs baseline, resting HR, stress, recovery, training load) "
                "over a trailing window. Call this when the user asks about "
                "sleep, HRV, recovery, resting HR, stress, training load, or "
                "'how's my health/readiness'. The READINESS table already in "
                "context is a fixed 7-day snapshot — use this tool for any other "
                "window. If it returns has_data=false, tell the user when the "
                "last sync was; never claim there is no COROS integration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {
                        "type": "integer",
                        "description": "Trailing window in days (default 7, max 90)",
                        "default": 7,
                    }
                },
            },
        },
    }
]


def _get_health_summary(args: dict, state) -> dict:
    try:
        window = int(args.get("window_days", 7) or 7)
    except (TypeError, ValueError):
        window = 7  # malformed input from the model — fall back, don't error
    window = max(MIN_WINDOW, min(MAX_WINDOW, window))
    today = today_local()

    rows = state.get_daily_health(days=window, today=today)

    if not rows:
        latest = state.latest_metric_date()
        return {
            "window_days": window,
            "has_data": False,
            "latest_sync_date": latest,
            "note": (f"No COROS health data in the last {window} days; last successful sync was {latest or 'never'}."),
        }

    weeks = max(1, -(-window // 7))  # ceil(window / 7)
    return {
        "window_days": window,
        "has_data": True,
        "latest_sync_date": rows[-1]["date"],  # rows are ascending by date
        "readiness_table": state.render_readiness_block(days=window, today=today),
        "load_trend": state.get_load_trend(weeks=weeks, today=today),
        "signals": _signals(rows),
    }


# ---------- signal extraction ----------


def _signals(rows: list[dict]) -> list[str]:
    """A few soft-touch English observations over the window. The agent
    interprets; this only surfaces patterns."""
    signals: list[str] = []

    sleeps = [r["sleep_duration_min"] for r in rows if r.get("sleep_duration_min") is not None]
    if sleeps:
        avg_min = round(sum(sleeps) / len(sleeps))
        h, m = divmod(avg_min, 60)
        signals.append(f"Average sleep {h}h{m:02d} over {len(sleeps)} night(s)")

    hrv_row = next((r for r in reversed(rows) if r.get("hrv_avg") is not None), None)
    baseline_row = next((r for r in reversed(rows) if r.get("hrv_baseline") is not None), None)
    if hrv_row and baseline_row and baseline_row.get("hrv_baseline"):
        hrv = hrv_row["hrv_avg"]
        base = baseline_row["hrv_baseline"]
        if hrv >= base:
            signals.append(f"Latest HRV {hrv}ms at or above baseline {base}ms")
        else:
            signals.append(f"Latest HRV {hrv}ms below baseline {base}ms — watch recovery")

    flagged = [r for r in rows if r.get("load_comment") and str(r["load_comment"]).lower() != "optimized"]
    if flagged:
        signals.append(f"{len(flagged)} day(s) with non-optimized training load in window")

    return signals


HANDLERS = {"get_health_summary": _get_health_summary}
