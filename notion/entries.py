"""Parse SQLite singleton blobs into the per-entry shape the mirror needs.

Journal and plan_changelog are still stored as single TEXT columns
(append-only blobs) rather than row-shaped tables. The Notion mirror wants
one page per entry, so this module splits the blobs into entry dicts. The
SQLite storage stays untouched — row-ifying those two tables is a later
schema migration.
"""

from __future__ import annotations

import re
from typing import Optional

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_journal_entries(content: str) -> list[dict]:
    """Split a journal singleton into entries.

    Each entry block starts with a ``## <header>`` line and runs until the
    next ``\\n---\\n`` separator. Returns
    ``[{"title": str, "date": Optional[str], "body": str}, ...]`` in
    document order. The preamble before the first separator is skipped.
    """
    out: list[dict] = []
    sections = (content or "").split("\n---\n")[1:]
    for section in sections:
        text = section.strip()
        if not text:
            continue
        lines = text.splitlines()
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            continue
        header = lines[i].strip()
        if not header.startswith("## "):
            continue
        title = header[3:].strip()
        body = "\n".join(lines[i + 1 :]).strip()
        out.append({"title": title, "date": _extract_date(title), "body": body})
    return out


def parse_changelog_entries(content: str) -> list[dict]:
    """Split the plan_changelog blob into entries.

    Each line follows ``- <timestamp>: <note>``. Returns
    ``[{"timestamp": str, "note": str, "action": "completed"|"planned-edit"},
      ...]``. Action is inferred from the note prefix written by
    ``reconcile_strava_activity`` (``"<date> completed: <type>"``); everything
    else is a planned edit.
    """
    out: list[dict] = []
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        rest = line[2:]
        if ": " not in rest:
            continue
        ts, note = rest.split(": ", 1)
        ts = ts.strip()
        note = note.strip()
        if not ts or not note:
            continue
        out.append({"timestamp": ts, "note": note, "action": _classify_action(note)})
    return out


def _classify_action(note: str) -> str:
    # reconcile_strava_activity writes "<YYYY-MM-DD> completed: <type>"
    if " completed:" in note or note.startswith("completed:"):
        return "completed"
    return "planned-edit"


def _extract_date(text: str) -> Optional[str]:
    m = _DATE_RE.search(text or "")
    return m.group(0) if m else None
