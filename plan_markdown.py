"""Plan-markdown parsing and rendering.

After the Phase 1A cutover the plan lives as rows in the ``sessions`` table,
not as a markdown blob. These helpers bridge the two representations:

  - ``parse_plan_rows`` / ``parse_workout_details`` / ``build_plan_meta``
    convert a legacy plan.md blob into rows + prose. Used by the one-shot
    cutover migration and by the ``update_plan`` escape hatch (which still
    accepts a full-markdown plan, e.g. when applying a review proposal).
  - ``render_week_table`` renders planned rows back into the locked
    markdown table for the system prompt and the ``/plan`` command.

The "locked format" is the weekly table the coach has always emitted:
``| Day | Date | Workout | Pace target | Notes |`` with ISO dates.
"""

from __future__ import annotations

import re
from datetime import date

LOCKED_HEADER_TOKENS = ("Day", "Date", "Workout", "Pace target", "Notes")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-day detail anchor: a `#### YYYY-MM-DD` heading on its own line.
_DETAIL_ANCHOR_RE = re.compile(r"^####\s+(\d{4}-\d{2}-\d{2})\s*$")
# Any markdown heading — bounds a detail block (body runs until the next one).
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")


def parse_plan_rows(plan_text: str) -> list[dict]:
    """Return all locked-format rows in a plan blob as dicts.

    Locked format: ``| Day | Date | Workout | Pace target | Notes |`` where
    Date is ISO YYYY-MM-DD. Header / separator / non-ISO rows are skipped, as
    are rows whose Workout cell is empty or a dash.
    """
    out: list[dict] = []
    for line in plan_text.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 5:
            continue
        date_cell = parts[1]
        if not _ISO_DATE_RE.match(date_cell):
            continue
        try:
            date.fromisoformat(date_cell)
        except ValueError:
            continue
        workout = parts[2].strip()
        if workout in {"", "-", "—"}:
            continue
        out.append(
            {
                "day_name": parts[0],
                "date": date_cell,
                "workout": workout,
                "pace_target": parts[3],
                "notes": parts[4],
            }
        )
    return out


def parse_workout_details(plan_text: str) -> dict[str, str]:
    """Return per-day rich-detail bodies keyed by ISO date.

    Looks for ``#### YYYY-MM-DD`` anchor lines anywhere in the plan. The body
    extends until the next heading or EOF. Empty bodies are dropped.
    """
    out: dict[str, str] = {}
    lines = plan_text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        m = _DETAIL_ANCHOR_RE.match(lines[i])
        if not m:
            i += 1
            continue
        date_iso = m.group(1)
        i += 1
        body_lines: list[str] = []
        while i < n and not _HEADING_RE.match(lines[i]):
            body_lines.append(lines[i])
            i += 1
        body = "\n".join(body_lines).strip()
        if body:
            out[date_iso] = body
    return out


def build_plan_meta(plan_text: str) -> str:
    """Return the plan blob with the locked weekly table and every
    ``#### date`` detail block removed — the prose remainder (phases, goals,
    pace zones, adjustment triggers) for plan_meta."""
    lines = plan_text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if "|" in line and len(parts) >= 5 and tuple(parts[:5]) == LOCKED_HEADER_TOKENS:
            i += 1  # skip header
            if i < n and "|" in lines[i]:
                i += 1  # skip separator
            while i < n and "|" in lines[i] and lines[i].strip():
                i += 1
            continue
        if _DETAIL_ANCHOR_RE.match(line.strip()):
            i += 1
            while i < n and not _HEADING_RE.match(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    collapsed: list[str] = []
    blanks = 0
    for line in out:
        if line.strip():
            blanks = 0
        else:
            blanks += 1
            if blanks > 2:
                continue
        collapsed.append(line)
    return "\n".join(collapsed).strip() + "\n"


def infer_workout_type(workout: str) -> str:
    """Best-effort workout-type classification from a workout description.

    Heuristic, not authoritative. Order matters: race, intervals and the
    run-distance check all run before the cross-training keyword bucket, so a
    run that merely *mentions* yoga or strides stays a run, not cross.
    Returns one of: rest / easy / workout / long / race / cross / strength.
    """
    w = (workout or "").strip().lower()
    if not w or w in {"-", "—"}:
        return "rest"
    if "race" in w or "brooklyn half" in w or "sky race" in w:
        return "race"
    if re.search(r"\d+\s*x\s*\d", w) or "tempo" in w or "interval" in w or "repeat" in w:
        return "workout"
    if "long run" in w or w.startswith("long "):
        return "long"
    if w.startswith("rest"):
        return "rest"
    # A run component dominates: "8mi", "3mi shakeout", "easy run". The \b
    # after mi(le|les)? keeps "75min" / "20min" from reading as a run.
    if re.search(r"\d+\s*mi(le|les)?\b", w) or "shakeout" in w or "easy run" in w:
        return "easy"
    if any(k in w for k in ("walk", "mobility", "off day")):
        return "rest"
    if any(k in w for k in ("cycl", "spin", "swim", "bike", "ride", "yoga", "cross")):
        return "cross"
    if "strength" in w or "lift" in w:
        return "strength"
    return "easy"


def render_week_table(rows: list[dict]) -> str:
    """Render planned rows as the locked markdown table.

    Each row dict needs ``date``, ``workout`` (or ``prescribed_workout``),
    ``pace_target`` (or ``prescribed_pace``), ``notes`` (or
    ``prescribed_notes``). The Day column is derived from the date.
    """
    header = "| " + " | ".join(LOCKED_HEADER_TOKENS) + " |"
    sep = "|" + "|".join(["-----"] * len(LOCKED_HEADER_TOKENS)) + "|"
    lines = [header, sep]
    for r in rows:
        iso = r["date"]
        try:
            day = date.fromisoformat(iso).strftime("%a")
        except (ValueError, TypeError):
            day = r.get("day_name", "")
        workout = r.get("workout") or r.get("prescribed_workout") or ""
        pace = r.get("pace_target") or r.get("prescribed_pace") or ""
        notes = r.get("notes") or r.get("prescribed_notes") or ""
        lines.append(f"| {day} | {iso} | {workout} | {pace} | {notes} |")
    return "\n".join(lines)
