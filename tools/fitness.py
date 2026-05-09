"""Soft-touch fitness summary tool.

Surfaces patterns from the training log as English observations. Does NOT
prescribe — the agent interprets signals and decides what to do.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from temporal_context import today_local

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_fitness_summary",
            "description": (
                "Pull a soft-touch trailing fitness snapshot: recent quality "
                "sessions with pace-vs-zone and HR context, weekly volume, "
                "and notable signals as English observations. Call BEFORE "
                "adjusting pace zones or making non-trivial plan changes. "
                "Adjust on TRENDS across multiple sessions, not single ones. "
                "The tool surfaces patterns; the agent interprets and decides."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "window_days": {
                        "type": "integer",
                        "description": "Trailing window in days (default 21)",
                        "default": 21,
                    }
                },
            },
        },
    }
]


RUN_TYPES = {"run", "easy", "long_run", "workout", "race", "strides", "return_test"}
QUALITY_TYPES = {"workout", "race", "long_run", "strides"}


def _get_fitness_summary(args: dict, state) -> dict:
    window = int(args.get("window_days", 21))
    today = today_local()
    sessions = state.get_recent_sessions(days=window, today=today)
    athlete = state.load_athlete()
    zones = athlete.get("zones", {}) or {}
    hr_zones = athlete.get("hr_zones", {}) or {}

    runs = [s for s in sessions if s.get("type") in RUN_TYPES]
    weekly_summaries = [s for s in sessions if s.get("type") == "weekly_summary"]
    quality = [s for s in runs if s.get("type") in QUALITY_TYPES and s.get("pace_avg")]

    weekly_volume = _weekly_volume(weekly_summaries, runs, today, window)
    quality_with_context = [_session_context(s, zones, hr_zones) for s in quality]
    total_miles = sum(w["miles"] for w in weekly_volume if w["miles"])

    signals: list[str] = []
    signals.extend(_volume_signals(weekly_volume))
    signals.extend(_quality_signals(quality_with_context))
    signals.extend(_frequency_signals(quality, window))

    gaps = _data_gaps(quality)

    return {
        "window_days": window,
        "session_count": len(runs),
        "total_miles": round(total_miles, 1),
        "weekly_volume": weekly_volume,
        "quality_sessions": quality_with_context,
        "signals": signals,
        "data_gaps": gaps,
    }


# ---------- volume ----------


def _weekly_volume(summaries: list[dict], runs: list[dict], today: date, window: int) -> list[dict]:
    """Build {week_start, miles, source} buckets covering the trailing window.

    Prefers weekly_summary entries when present; falls back to summing
    individual session miles otherwise.
    """
    by_week_summary: dict[str, dict] = {}
    for s in summaries:
        d = _parse_date(s.get("date"))
        if d is None:
            continue
        wk = (d - timedelta(days=d.weekday())).isoformat()
        by_week_summary[wk] = s

    by_week_runs: dict[str, float] = defaultdict(float)
    for r in runs:
        d = _parse_date(r.get("date"))
        if d is None:
            continue
        wk = (d - timedelta(days=d.weekday())).isoformat()
        by_week_runs[wk] += r.get("miles") or 0

    start = today - timedelta(days=window)
    cur = start - timedelta(days=start.weekday())
    end_monday = today - timedelta(days=today.weekday())
    out = []
    while cur <= end_monday:
        wk = cur.isoformat()
        if wk in by_week_summary:
            miles = by_week_summary[wk].get("miles") or 0
            source = "weekly_summary"
        else:
            miles = round(by_week_runs.get(wk, 0), 1)
            source = "computed_from_sessions" if miles > 0 else "no_data"
        out.append({"week_start": wk, "miles": miles, "source": source})
        cur += timedelta(days=7)
    return out


# ---------- per-session decoration ----------


def _session_context(session: dict, zones: dict, hr_zones: dict) -> dict:
    out = {
        "date": session.get("date"),
        "type": session.get("type"),
        "miles": session.get("miles"),
        "pace_avg": session.get("pace_avg"),
        "hr_avg": session.get("hr_avg"),
        "rpe": session.get("rpe"),
        "notes": session.get("notes", ""),
    }
    pace_sec = _pace_to_sec(session.get("pace_avg"))
    if pace_sec is not None:
        zone_match = _closest_zone(pace_sec, zones)
        if zone_match:
            name, lo, hi = zone_match
            if pace_sec < lo:
                out["vs_zone"] = f"{lo - pace_sec} sec/mi faster than {name} ({_sec_to_pace(lo)})"
            elif pace_sec > hi:
                out["vs_zone"] = f"{pace_sec - hi} sec/mi slower than {name} ({_sec_to_pace(hi)})"
            else:
                out["vs_zone"] = f"within {name} zone ({_sec_to_pace(lo)}-{_sec_to_pace(hi)})"
    hr = session.get("hr_avg")
    if hr is not None and hr_zones:
        out["hr_context"] = _hr_context(hr, hr_zones)
    return out


# ---------- signal extraction ----------


def _volume_signals(weekly_volume: list[dict]) -> list[str]:
    signals: list[str] = []
    miles = [w["miles"] or 0 for w in weekly_volume]
    if len(miles) >= 2 and miles[-2] > 0:
        pct = round((miles[-1] - miles[-2]) / miles[-2] * 100)
        if abs(pct) >= 30:
            direction = "increase" if pct > 0 else "decrease"
            signals.append(
                f"Weekly volume {direction} of {abs(pct)}% week-over-week ({miles[-2]:.0f} -> {miles[-1]:.0f} mi)"
            )
    if len(miles) >= 3 and all(m > 0 for m in miles[-3:]):
        if miles[-1] > miles[-2] > miles[-3]:
            signals.append("Weekly volume rising 3 weeks in a row")
        elif miles[-1] < miles[-2] < miles[-3]:
            signals.append("Weekly volume falling 3 weeks in a row")
    return signals


def _quality_signals(quality: list[dict]) -> list[str]:
    signals: list[str] = []
    faster = sum(1 for q in quality if "faster than" in q.get("vs_zone", ""))
    slower = sum(1 for q in quality if "slower than" in q.get("vs_zone", ""))
    if faster >= 2:
        signals.append(
            f"{faster} quality sessions came in faster than current zones — "
            f"possible fitness gain; verify HR/RPE before tightening zones"
        )
    if slower >= 2:
        signals.append(
            f"{slower} quality sessions came in slower than current zones — "
            f"investigate fatigue, environment, or whether zones need loosening"
        )
    low_hr_fast = [q for q in quality if "faster than" in q.get("vs_zone", "") and "below" in q.get("hr_context", "")]
    if low_hr_fast:
        signals.append(
            f"{len(low_hr_fast)} session(s) hit faster than zone at HR below "
            f"expected effort range — strong fitness signal; consider raising "
            f"intensity or tightening zones"
        )
    return signals


def _frequency_signals(quality: list[dict], window: int) -> list[str]:
    signals: list[str] = []
    if window >= 14 and len(quality) == 0:
        signals.append(
            f"No quality sessions logged in the last {window} days — confirm "
            f"this matches the plan (taper, recovery) or schedule one"
        )
    return signals


def _data_gaps(quality: list[dict]) -> list[str]:
    if not quality:
        return []
    n = len(quality)
    gaps = []
    if sum(1 for q in quality if q.get("hr_avg") is None) / n >= 0.5:
        gaps.append("HR missing on majority of quality sessions — prompt for HR next time")
    if sum(1 for q in quality if q.get("rpe") is None) / n >= 0.5:
        gaps.append("RPE missing on majority of quality sessions — prompt for RPE next time")
    return gaps


# ---------- pace / HR parsing ----------


def _pace_to_sec(pace: Optional[str]) -> Optional[int]:
    if not pace:
        return None
    pace = pace.strip()
    if "-" in pace:
        parts = pace.split("-")
        secs = [_pace_to_sec(p) for p in parts]
        secs = [s for s in secs if s is not None]
        if not secs:
            return None
        return sum(secs) // len(secs)
    if ":" not in pace:
        return None
    try:
        m, s = pace.split(":")
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def _sec_to_pace(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _closest_zone(pace_sec: int, zones: dict) -> Optional[tuple[str, int, int]]:
    candidates = []
    for name, val in zones.items():
        lo, hi = _zone_range(val)
        if lo is None:
            continue
        mid = (lo + hi) // 2
        candidates.append((abs(pace_sec - mid), name, lo, hi))
    if not candidates:
        return None
    candidates.sort()
    _, name, lo, hi = candidates[0]
    return name, lo, hi


def _zone_range(value) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(value, str):
        return None, None
    value = value.strip()
    if "-" in value:
        parts = value.split("-")
        if len(parts) == 2:
            lo = _pace_to_sec(parts[0])
            hi = _pace_to_sec(parts[1])
            if lo is not None and hi is not None:
                return (min(lo, hi), max(lo, hi))
    p = _pace_to_sec(value)
    if p is not None:
        return p, p
    return None, None


def _hr_context(hr: int, hr_zones: dict) -> str:
    easy_ceiling = hr_zones.get("easy_ceiling")
    threshold_lo, threshold_hi = _hr_range(hr_zones.get("threshold"))
    if isinstance(easy_ceiling, int) and hr <= easy_ceiling:
        return f"in easy range (≤{easy_ceiling})"
    if threshold_lo is not None:
        if hr < threshold_lo:
            return f"below threshold ({hr} vs {threshold_lo}-{threshold_hi})"
        if hr <= threshold_hi:
            return f"in threshold range ({threshold_lo}-{threshold_hi})"
        return f"above threshold ({hr} vs {threshold_lo}-{threshold_hi})"
    return f"hr {hr}"


def _hr_range(value) -> tuple[Optional[int], Optional[int]]:
    if isinstance(value, int):
        return value, value
    if not isinstance(value, str):
        return None, None
    v = value.strip().strip("\"'")
    if "-" in v:
        parts = v.split("-")
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None, None
    try:
        return int(v), int(v)
    except ValueError:
        return None, None


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


HANDLERS = {"get_fitness_summary": _get_fitness_summary}
