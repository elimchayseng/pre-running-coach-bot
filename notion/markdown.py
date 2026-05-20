"""Render Notion page bodies for mirrored rows.

The body is the Markdown content of a Notion page (separate from its
properties). For a Sessions row it carries the coaching prose and, once the
workout is logged, the actuals — notes, laps, splits.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def _session_data(row: dict) -> dict:
    """Parse a session row's ``data`` JSON (the logged actuals). {} if absent."""
    raw = row.get("data")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _render_laps(laps: list) -> Optional[str]:
    lines = []
    for lap in laps:
        if not isinstance(lap, dict):
            lines.append(f"- {lap}")
            continue
        bits: list[str] = []
        if lap.get("name"):
            bits.append(str(lap["name"]))
        if lap.get("distance_mi"):
            bits.append(f"{lap['distance_mi']}mi")
        if lap.get("pace"):
            bits.append(f"@ {lap['pace']}")
        if lap.get("hr_avg"):
            bits.append(f"HR {lap['hr_avg']}")
        lines.append("- " + " ".join(bits) if bits else f"- {lap}")
    return "## Laps\n\n" + "\n".join(lines) if lines else None


def _render_splits(splits: Any) -> Optional[str]:
    if not isinstance(splits, list) or not splits:
        return None
    lines = [f"- {s}" for s in splits]
    return "## Splits\n\n" + "\n".join(lines)


def render_change_body(
    before: Optional[str],
    after: Optional[str],
    *,
    before_heading: str = "Before",
    after_heading: str = "After",
) -> Optional[str]:
    """Render a before/after diff as the Markdown body of a Plan Changes page.

    Each side becomes a ``## heading`` followed by a fenced block so wide
    rows / multi-line plan_meta survive Notion's renderer intact. Returns
    ``None`` when both sides are empty so the caller can skip the body
    patch entirely.
    """
    # `plain text` fence tag prevents Notion from guessing the language
    # ("javascript" by default on bare ``` blocks, which colours the rows
    # in unhelpful syntax tones).
    parts: list[str] = []
    if before is not None and str(before).strip():
        parts.append(f"## {before_heading}\n\n```plain text\n{str(before).strip()}\n```")
    if after is not None and str(after).strip():
        parts.append(f"## {after_heading}\n\n```plain text\n{str(after).strip()}\n```")
    return "\n\n".join(parts) if parts else None


def render_session_body(row: dict) -> Optional[str]:
    """Return the Markdown body for a Sessions page, or None when empty.

    - planned: the per-day coaching detail (``detail_md``), if any.
    - completed / off-plan / missed: the coaching detail plus the logged
      actuals — notes, laps, splits.
    """
    parts: list[str] = []

    detail = (row.get("detail_md") or "").strip()
    if detail:
        parts.append(detail)

    if row.get("status") != "planned":
        data = _session_data(row)
        notes = data.get("notes")
        if notes:
            parts.append(f"## Notes\n\n{notes}")
        details = data.get("details") or {}
        laps = _render_laps(details["laps"]) if isinstance(details.get("laps"), list) else None
        if laps:
            parts.append(laps)
        splits = _render_splits(details.get("splits"))
        if splits:
            parts.append(splits)

    return "\n\n".join(parts) if parts else None
