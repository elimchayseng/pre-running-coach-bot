"""Push the locked-format weekly table from plan.md into the user's
PRE Training Google Calendar.

Pipeline (one-way write, bot → gcal):
    1. Scan plan.md for rows matching the locked format
       (| Day | Date | Workout | Pace target | Notes |). Same parsing
       shape as state_manager._find_workout_row but applied to every
       matching row, not just today's.
    2. For each row, build an all-day event payload + sha1 hash.
    3. Diff against state/.gcal_sync_state.json:
       - hash matches  → unchanged (no API call)
       - new id        → insert (409 → fall through to patch)
       - hash differs  → insert (409 → patch)
    4. Prune any pre_managed events in [today-60d, today+60d] that aren't
       in the set of dates we just synced.
    5. Persist the new sync state.

Decisions:
    * Rest days ARE synced (e.g. "Rest + gentle yoga PM" carries info).
      Only rows with empty / "—" / "-" workouts are skipped.
    * Per-row try/except — never abort the batch on one bad row.
    * Triggered exclusively via explicit agent tool call (or CLI), never
      auto-hooked into update_plan; the agent makes many intermediate
      edits per turn and we don't want to write-amplify gcal patches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from temporal_context import today_local

logger = logging.getLogger("pre_coach.gcal.sync")

# Sync window for prune (centered on today). 60 days each side covers any
# realistic plan horizon while staying well under the 2500-events page cap.
# Pad by ±1 day on the API request so USER_TIMEZONE skew (today_local respects
# the user's TZ but Google's timeMin/timeMax are UTC) can't drop edge events.
PRUNE_WINDOW_DAYS = 60
_PRUNE_TZ_BUFFER_DAYS = 1

# Local sync state file: maps event_id -> {hash, last_synced_at}.
SYNC_STATE_FILE = Path(__file__).resolve().parent.parent / "state" / ".gcal_sync_state.json"

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-day workout-detail anchor: a `#### YYYY-MM-DD` heading on its own line.
# The body extends until the next heading at level <= 4 (####, ###, ##, #) or EOF.
_DETAIL_ANCHOR_RE = re.compile(r"^####\s+(\d{4}-\d{2}-\d{2})\s*$")
_HEADING_RE = re.compile(r"^#{1,4}\s+\S")

# Google caps event description at 8 KB (bytes, not chars). Stay under with a
# safety margin so the extra "(synced by PRE)" footer + truncation marker fit.
# Multi-byte unicode (em-dashes, emoji) is common in coaching prose, so we clamp
# on UTF-8 byte length rather than character count — 7000 chars of em-dashes is
# 21000 bytes and would 400 from the API.
DESCRIPTION_MAX_BYTES = 7000


def sync_plan(state, dry_run: bool = False) -> dict:
    """Sync plan.md table rows to the PRE Training calendar.

    Returns {inserted, patched, deleted, unchanged, errors, dry_run}.
    """
    plan_text = state.load_plan()
    rows = _parse_plan_rows(plan_text)
    details = _parse_workout_details(plan_text)
    sync_state = _load_sync_state()
    new_sync_state: dict[str, dict] = {}

    counts = {"inserted": 0, "patched": 0, "deleted": 0, "unchanged": 0}
    errors: list[dict] = []
    synced_dates: set[str] = set()

    from . import client

    for row in rows:
        event_id = _event_id(row["date"])
        synced_dates.add(row["date"])
        try:
            payload, payload_hash = _build_event_payload(row, event_id, details.get(row["date"]))
        except Exception as e:
            errors.append({"date": row["date"], "error": f"payload_build: {e}"})
            continue

        prior = sync_state.get(event_id)
        if prior and prior.get("hash") == payload_hash:
            counts["unchanged"] += 1
            new_sync_state[event_id] = prior
            continue

        if dry_run:
            action = "would_patch" if prior else "would_insert"
            logger.info("[dry-run] %s %s (%s)", action, event_id, row["date"])
            counts["patched" if prior else "inserted"] += 1
            new_sync_state[event_id] = {"hash": payload_hash, "last_synced_at": _now_iso()}
            continue

        try:
            try:
                client.insert_event(payload)
                counts["inserted"] += 1
            except client.GcalEventExistsError:
                # id collision: patch the existing event instead
                patch_payload = {k: v for k, v in payload.items() if k != "id"}
                client.patch_event(event_id, patch_payload)
                counts["patched"] += 1
            new_sync_state[event_id] = {"hash": payload_hash, "last_synced_at": _now_iso()}
        except Exception as e:
            errors.append({"date": row["date"], "error": f"{type(e).__name__}: {e}"})
            # Preserve any prior state for this id so a transient failure
            # doesn't cause us to forget the hash.
            if prior:
                new_sync_state[event_id] = prior

    # Prune: anything pre_managed in the window that we didn't just sync.
    today = today_local()
    tmin = _iso_z(today - timedelta(days=PRUNE_WINDOW_DAYS + _PRUNE_TZ_BUFFER_DAYS))
    tmax = _iso_z(today + timedelta(days=PRUNE_WINDOW_DAYS + _PRUNE_TZ_BUFFER_DAYS))
    try:
        managed = client.list_managed_events(tmin, tmax)
    except Exception as e:
        errors.append({"date": "prune", "error": f"list_managed: {type(e).__name__}: {e}"})
        managed = []

    for ev in managed:
        ev_date = (ev.get("start") or {}).get("date")
        if not ev_date:
            continue  # not an all-day event we manage
        if ev_date in synced_dates:
            continue
        ev_id = ev.get("id")
        if not ev_id:
            continue
        if dry_run:
            logger.info("[dry-run] would_delete %s (%s)", ev_id, ev_date)
            counts["deleted"] += 1
            continue
        try:
            client.delete_event(ev_id)
            counts["deleted"] += 1
            new_sync_state.pop(ev_id, None)
        except Exception as e:
            errors.append({"date": ev_date, "error": f"delete {ev_id}: {type(e).__name__}: {e}"})

    if not dry_run:
        try:
            _write_sync_state(new_sync_state)
        except Exception as e:
            errors.append({"date": "sync_state", "error": f"write: {type(e).__name__}: {e}"})

    return {
        **counts,
        "errors": errors,
        "dry_run": dry_run,
    }


# ---------- plan parsing ----------


def _parse_plan_rows(plan_text: str) -> list[dict]:
    """Return all locked-format rows in the plan as dicts.

    Locked format: | Day | Date | Workout | Pace target | Notes |
    where Date is an ISO YYYY-MM-DD. Header / separator / phase-2 prose
    rows are silently skipped.
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


def _parse_workout_details(plan_text: str) -> dict[str, str]:
    """Return per-day rich-detail bodies keyed by ISO date.

    Looks for `#### YYYY-MM-DD` anchor lines anywhere in the plan. The body
    extends until the next heading (level 1-4) or EOF. Empty / whitespace
    bodies are dropped from the map so the caller falls back to the table
    cells.
    """
    out: dict[str, str] = {}
    lines = plan_text.splitlines()
    i = 0
    n = len(lines)
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


def _clamp_description(body: str) -> str:
    """Truncate a description body if it would exceed Google's 8 KB byte cap."""
    encoded = body.encode("utf-8")
    if len(encoded) <= DESCRIPTION_MAX_BYTES:
        return body
    logger.warning(
        "workout detail body %d bytes exceeds %d cap; truncating",
        len(encoded),
        DESCRIPTION_MAX_BYTES,
    )
    # Decode with errors='ignore' to drop any partial multi-byte sequence at
    # the cut point, then strip trailing whitespace before appending the marker.
    truncated = encoded[:DESCRIPTION_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
    return truncated + "\n…[truncated]"


# ---------- event payload ----------


def _event_id(iso_date: str) -> str:
    """Deterministic id derived from the date.

    Google requires `[a-v0-9]+` and 5–1024 chars. "pretrain" + YYYYMMDD
    yields a stable 16-char id that's trivially valid.
    """
    return "pretrain" + iso_date.replace("-", "")


def _strip_bold(s: str) -> str:
    """Gcal renders literal asterisks in event summaries — strip **bold**."""
    return _BOLD_RE.sub(r"\1", s)


def _build_event_payload(
    row: dict,
    event_id: str,
    detail_body: Optional[str] = None,
) -> tuple[dict, str]:
    start = date.fromisoformat(row["date"])
    end = start + timedelta(days=1)  # all-day events: end is exclusive

    body = (detail_body or "").strip()
    if body:
        # Rich coaching prose from the per-day #### YYYY-MM-DD block.
        description = _clamp_description(body) + "\n\n(synced by PRE)"
    else:
        # Fallback: derive a sparse description from the table cells.
        parts = []
        if row["pace_target"] and row["pace_target"] not in {"-", "—"}:
            parts.append(f"Pace: {row['pace_target']}")
        if row["notes"] and row["notes"] not in {"-", "—"}:
            parts.append(f"Notes: {row['notes']}")
        parts.append("")
        parts.append("(synced by PRE)")
        description = "\n".join(parts)

    payload = {
        "id": event_id,
        "summary": _strip_bold(row["workout"]),
        "description": description,
        "start": {"date": start.isoformat()},
        "end": {"date": end.isoformat()},
        "extendedProperties": {
            "private": {"pre_managed": "1"},
        },
    }

    # Hash everything except the hash itself, so we can store the hash on
    # the event and detect drift later. Sort keys for stability.
    payload_hash = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    payload["extendedProperties"]["private"]["pre_plan_hash"] = payload_hash
    return payload, payload_hash


# ---------- sync-state file ----------


def _load_sync_state() -> dict[str, dict]:
    if not SYNC_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(SYNC_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        logger.warning("Sync state file %s is not a dict; ignoring", SYNC_STATE_FILE)
        return {}
    except (OSError, json.JSONDecodeError) as e:
        # Corruption is self-healing — next sync re-checks everything via 409.
        logger.warning("Could not read sync state %s: %s", SYNC_STATE_FILE, e)
        return {}


def _write_sync_state(state: dict[str, dict]) -> None:
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=SYNC_STATE_FILE.parent,
        prefix=f".{SYNC_STATE_FILE.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp_path, SYNC_STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    # datetime.utcnow() is deprecated in 3.12 and slated for removal; use a
    # tz-aware UTC datetime and emit the trailing 'Z' shape gcal expects.
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _iso_z(d: date) -> str:
    """Format a date as RFC3339 UTC midnight — what gcal's timeMin/timeMax want."""
    return f"{d.isoformat()}T00:00:00Z"


def get_last_sync_summary() -> Optional[dict]:
    """Return a small summary of the on-disk sync state (for status tooling).

    {count: N, last_synced_at: <max iso>, file: <path>} or None if absent.
    """
    state = _load_sync_state()
    if not state:
        return None
    last_at = max(
        (v.get("last_synced_at") for v in state.values() if isinstance(v, dict)),
        default=None,
    )
    return {
        "count": len(state),
        "last_synced_at": last_at,
        "file": str(SYNC_STATE_FILE),
    }
