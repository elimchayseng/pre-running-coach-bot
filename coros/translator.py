"""Deterministic parsers: COROS MCP tool text -> daily_health row dicts.

The COROS MCP returns human-readable text (usually a JSON-encoded string —
quotes around the whole payload, \\n escapes). Every parser here:

- unwraps the JSON-string layer when present (`_unwrap`),
- tolerates missing/mangled lines by returning None for that field (a COROS
  copy change degrades to NULL columns, never an exception),
- is fixture-tested against captured real outputs in tests/fixtures/coros/.

Quirks worth knowing (verified live; see docs/coros-mcp.md):
- queryDailyHealthData section headers use yyyyMMdd; other tools use ISO.
- queryDailyHealthData's header "Resting HR / HRV Baseline" values disagree
  with the dedicated tools (e.g. baseline 42 vs queryHrvAssessment's 82), so
  the header is deliberately NOT parsed — per-day tools win.
- The same night's sleep score can differ between queryDailyHealthData and
  querySleepData; querySleepData (the dedicated tool) takes precedence.
- Today's rows often lag: resting HR is "No data" until tomorrow, the HRV
  list has no entry for today yet.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Optional

logger = logging.getLogger("pre_coach.coros.translator")


def _unwrap(text: str) -> str:
    """Strip the JSON-string envelope if present ('"...\\n..."' -> real text)."""
    stripped = (text or "").strip()
    if stripped.startswith('"'):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return text or ""


def _to_int(value: str) -> Optional[int]:
    """'12,377' -> 12377; returns None on anything non-numeric."""
    try:
        return int(value.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _duration_min(text: str) -> Optional[int]:
    """'7h 32min' / '49 min' / '3h 34min' / '0 min' -> total minutes."""
    if not text:
        return None
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*min", text)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def _iso(yyyymmdd: str) -> Optional[str]:
    """'20260611' -> '2026-06-11' (None on malformed input)."""
    s = yyyymmdd.strip()
    if not re.fullmatch(r"\d{8}", s):
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")


def parse_daily_health(text: str) -> dict[str, dict]:
    """queryDailyHealthData -> {iso_date: {steps, exercise_min, stress_avg,
    sleep_score, sleep_total_min, sleep_awake_min, sleep_deep_min,
    sleep_light_min, sleep_rem_min}}.

    Sleep fields here are the fallback; parse_sleep's values (the dedicated
    tool) override them in merge_daily_rows. The point-in-time header
    (Resting HR / HRV Baseline) is intentionally ignored — see module doc.
    """
    out: dict[str, dict] = {}
    current: Optional[dict] = None
    for line in _unwrap(text).splitlines():
        line = line.strip()
        section = re.match(r"^-+\s*(\d{8})\s*-+$", line)
        if section:
            iso = _iso(section.group(1))
            current = out.setdefault(iso, {}) if iso else None
            continue
        if current is None:
            continue
        m = re.match(r"^Steps:\s*([\d,]+)", line)
        if m:
            current["steps"] = _to_int(m.group(1))
        m = re.search(r"Exercise:\s*([^|]+)", line)
        if m:
            current["exercise_min"] = _duration_min(m.group(1))
        m = re.match(r"^Stress:\s*Avg\s*(\d+)", line)
        if m:
            current["stress_avg"] = _to_int(m.group(1))
        m = re.match(r"^Sleep Summary:.*Score:\s*(\d+)", line)
        if m:
            current["sleep_score"] = _to_int(m.group(1))
        m = re.match(r"^Total:\s*(.+?)\s*\|\s*Awake:\s*(.+)$", line)
        if m:
            current["sleep_total_min"] = _duration_min(m.group(1))
            current["sleep_awake_min"] = _duration_min(m.group(2))
        m = re.match(r"^Deep:\s*(.+?)\s*\|\s*Light:\s*(.+?)\s*\|\s*REM:\s*(.+)$", line)
        if m:
            current["sleep_deep_min"] = _duration_min(m.group(1))
            current["sleep_light_min"] = _duration_min(m.group(2))
            current["sleep_rem_min"] = _duration_min(m.group(3))
    return out


def parse_sleep(text: str) -> dict[str, dict]:
    """querySleepData -> {iso_date: {sleep_score, sleep_duration_min,
    sleep_nap_min, sleep_awake_min}}.

    Stage breakdown here is ratios (%), not durations — merge keeps the
    minute-granular stages from parse_daily_health instead.
    """
    out: dict[str, dict] = {}
    current: Optional[dict] = None
    for line in _unwrap(text).splitlines():
        line = line.strip()
        m = _ISO_RE.match(line)
        if m and ":" not in line:
            current = out.setdefault(m.group(1), {})
            continue
        if current is None:
            continue
        m = re.match(r"^Sleep Score:\s*(\d+)", line)
        if m:
            current["sleep_score"] = _to_int(m.group(1))
        m = re.match(r"^Main Sleep:\s*(.+)$", line)
        if m:
            current["sleep_duration_min"] = _duration_min(m.group(1))
        m = re.match(r"^Awake Time:\s*(.+)$", line)
        if m:
            current["sleep_awake_min"] = _duration_min(m.group(1))
        m = re.match(r"^Naps Total:\s*(.+)$", line)
        if m:
            current["sleep_nap_min"] = _duration_min(m.group(1))
    return out


def parse_hrv(text: str) -> dict:
    """queryHrvAssessment -> {"baseline", "range_low", "range_high",
    "days": {iso_date: {"hrv_avg", "evaluation"}}}."""
    baseline = range_low = range_high = None
    days: dict[str, dict] = {}
    current_date: Optional[str] = None
    for line in _unwrap(text).splitlines():
        line = line.strip()
        m = re.match(r"^Normal Range:\s*(\d+)\s*-\s*(\d+)", line)
        if m:
            range_low, range_high = _to_int(m.group(1)), _to_int(m.group(2))
        m = re.match(r"^Baseline:\s*(\d+)", line)
        if m:
            baseline = _to_int(m.group(1))
        m = _ISO_RE.match(line)
        if m:
            current_date = m.group(1)
            continue
        m = re.match(r"^HRV Avg:\s*(\d+)\s*ms(?:\s*—\s*(.+))?$", line)
        if m and current_date:
            days[current_date] = {
                "hrv_avg": _to_int(m.group(1)),
                "evaluation": (m.group(2) or "").strip() or None,
            }
    return {"baseline": baseline, "range_low": range_low, "range_high": range_high, "days": days}


def parse_resting_hr(text: str) -> dict[str, int]:
    """queryRestingHeartRate -> {iso_date: bpm}. 'No data' days are omitted."""
    out: dict[str, int] = {}
    for line in _unwrap(text).splitlines():
        m = re.match(r"^(\d{4}-\d{2}-\d{2}):\s*(\d+)\s*bpm", line.strip())
        if m:
            bpm = _to_int(m.group(2))
            if bpm is not None:
                out[m.group(1)] = bpm
    return out


def parse_training_load(text: str) -> dict[str, dict]:
    """queryTrainingLoadAssessment -> {iso_date: {load_comment,
    load_short_term, load_long_term, load_ratio}}."""
    out: dict[str, dict] = {}
    current: Optional[dict] = None
    for line in _unwrap(text).splitlines():
        line = line.strip()
        m = _ISO_RE.match(line)
        if m and ":" not in line:
            current = out.setdefault(m.group(1), {})
            continue
        if current is None:
            continue
        m = re.match(r"^Comment:\s*(.+)$", line)
        if m:
            current["load_comment"] = m.group(1).strip()
        m = re.match(r"^Short-Term Load:\s*([\d.,]+)", line)
        if m:
            current["load_short_term"] = _to_float(m.group(1))
        m = re.match(r"^Long-Term Load:\s*([\d.,]+)", line)
        if m:
            current["load_long_term"] = _to_float(m.group(1))
        m = re.match(r"^Load Ratio:\s*([\d.,]+)", line)
        if m:
            current["load_ratio"] = _to_float(m.group(1))
    return out


def parse_recovery(text: str) -> dict:
    """queryRecoveryStatus -> {"recovery_pct", "recovery_level"} (point-in-time)."""
    pct = level = None
    for line in _unwrap(text).splitlines():
        line = line.strip()
        m = re.match(r"^Recovery:\s*(\d+)\s*%", line)
        if m:
            pct = _to_int(m.group(1))
        m = re.match(r"^Level:\s*(.+)$", line)
        if m:
            level = m.group(1).strip()
    return {"recovery_pct": pct, "recovery_level": level}


# Columns a merged row may carry (besides date/raw). Used by the ingest
# format-change detector to count parsed fields.
METRIC_KEYS = (
    "sleep_score",
    "sleep_duration_min",
    "sleep_nap_min",
    "sleep_deep_min",
    "sleep_light_min",
    "sleep_rem_min",
    "sleep_awake_min",
    "hrv_avg",
    "hrv_baseline",
    "hrv_range_low",
    "hrv_range_high",
    "hrv_evaluation",
    "resting_hr",
    "stress_avg",
    "steps",
    "exercise_min",
    "recovery_pct",
    "recovery_level",
    "load_short_term",
    "load_long_term",
    "load_ratio",
    "load_comment",
)


def merge_daily_rows(bundle: dict[str, str], today: date) -> list[dict]:
    """Merge per-tool parses into one row dict per ISO date.

    - querySleepData beats queryDailyHealthData for the shared sleep fields
      (dedicated tool; scores occasionally disagree).
    - The point-in-time recovery snapshot and the full raw bundle attach to
      today's row ONLY (older rows keep whatever was captured the night they
      were "today" — upsert COALESCE preserves it).
    - HRV baseline/range are denormalized onto every row that has an HRV avg.
    """
    today_iso = today.isoformat()
    rows: dict[str, dict] = {}

    def _row(d: str) -> dict:
        return rows.setdefault(d, {"date": d})

    daily = parse_daily_health(bundle.get("queryDailyHealthData", ""))
    for d, fields in daily.items():
        row = _row(d)
        for key in ("steps", "exercise_min", "stress_avg", "sleep_score",
                    "sleep_awake_min", "sleep_deep_min", "sleep_light_min", "sleep_rem_min"):
            if fields.get(key) is not None:
                row[key] = fields[key]
        # Fallback main-sleep duration: total minus naps isn't derivable here
        # (no nap data in this tool), so use total as-is; parse_sleep overrides.
        if fields.get("sleep_total_min") is not None:
            row.setdefault("sleep_duration_min", fields["sleep_total_min"])

    sleep = parse_sleep(bundle.get("querySleepData", ""))
    for d, fields in sleep.items():
        row = _row(d)
        row.update({k: v for k, v in fields.items() if v is not None})

    hrv = parse_hrv(bundle.get("queryHrvAssessment", ""))
    for d, fields in hrv["days"].items():
        row = _row(d)
        if fields.get("hrv_avg") is not None:
            row["hrv_avg"] = fields["hrv_avg"]
            row["hrv_evaluation"] = fields.get("evaluation")
            for key, val in (
                ("hrv_baseline", hrv["baseline"]),
                ("hrv_range_low", hrv["range_low"]),
                ("hrv_range_high", hrv["range_high"]),
            ):
                if val is not None:
                    row[key] = val

    for d, bpm in parse_resting_hr(bundle.get("queryRestingHeartRate", "")).items():
        _row(d)["resting_hr"] = bpm

    for d, fields in parse_training_load(bundle.get("queryTrainingLoadAssessment", "")).items():
        _row(d).update({k: v for k, v in fields.items() if v is not None})

    recovery = parse_recovery(bundle.get("queryRecoveryStatus", ""))
    if recovery.get("recovery_pct") is not None:
        row = _row(today_iso)
        row["recovery_pct"] = recovery["recovery_pct"]
        row["recovery_level"] = recovery.get("recovery_level")

    if bundle and today_iso in rows:
        rows[today_iso]["raw"] = json.dumps(bundle, ensure_ascii=False)

    return [rows[d] for d in sorted(rows)]
