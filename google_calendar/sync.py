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
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from temporal_context import today_local

logger = logging.getLogger("pre_coach.gcal.sync")

# Sync window for prune (centered on today). 60 days each side covers any
# realistic plan horizon while staying well under the 2500-events page cap.
# Pad by ±1 day on the API request so USER_TIMEZONE skew (today_local respects
# the user's TZ but Google's timeMin/timeMax are UTC) can't drop edge events.
PRUNE_WINDOW_DAYS = 60
_PRUNE_TZ_BUFFER_DAYS = 1

# Sync state now lives in the SQLite gcal_sync_state table — see StateManager.
# The helpers `_load_sync_state` / `_write_sync_state` below preserve the
# old dict-shaped API so the rest of this module is unchanged.

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Google caps event description at 8 KB (bytes, not chars). Stay under with a
# safety margin so the extra "(synced by PRE)" footer + truncation marker fit.
# Multi-byte unicode (em-dashes, emoji) is common in coaching prose, so we clamp
# on UTF-8 byte length rather than character count — 7000 chars of em-dashes is
# 21000 bytes and would 400 from the API.
DESCRIPTION_MAX_BYTES = 7000


def sync_plan(state, dry_run: bool = False) -> dict:
    """Sync prescription rows from `sessions` to the PRE Training calendar.

    Returns {inserted, patched, deleted, unchanged, errors, dry_run}.
    """
    today = today_local()
    rows = state.get_prescription_rows(
        today - timedelta(days=PRUNE_WINDOW_DAYS),
        today + timedelta(days=PRUNE_WINDOW_DAYS),
    )
    sync_state = _load_sync_state(state)
    new_sync_state: dict[str, dict] = {}

    counts = {"inserted": 0, "patched": 0, "deleted": 0, "unchanged": 0}
    errors: list[dict] = []
    synced_dates: set[str] = set()

    from . import client

    for srow in rows:
        row = _plan_dict(srow)
        event_id = _event_id(row["date"])
        synced_dates.add(row["date"])
        try:
            payload, payload_hash = _build_event_payload(row, event_id, srow.get("detail_md"))
        except Exception as e:
            errors.append({"date": row["date"], "error": f"payload_build: {e}"})
            continue

        prior = sync_state.get(event_id)
        # Completed events are owned by mark_complete from here on — the local
        # sync_state carries `completed: true` so we don't roll back the ✅
        # prefix, graphite color, or actuals block. The hash may differ if the
        # plan row was edited after completion; we still skip (the event has
        # already happened).
        if prior and prior.get("completed"):
            counts["unchanged"] += 1
            new_sync_state[event_id] = prior
            continue
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
        # Preserve completed events even when their date is no longer in the
        # plan (off-plan precomplete events live on dates that have no plan
        # row, and past prescription events the user later removed from the
        # plan still represent history).
        ev_private = (ev.get("extendedProperties") or {}).get("private") or {}
        if ev_private.get("pre_completed") == "1":
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
            _write_sync_state(state, new_sync_state)
        except Exception as e:
            errors.append({"date": "sync_state", "error": f"write: {type(e).__name__}: {e}"})

    # Self-heal: walk recent plan rows + log entries and re-fire mark_complete
    # wherever the log shows a session but sync state doesn't yet reflect
    # completion. Idempotent — mark_complete short-circuits on already-marked
    # rows. Best-effort: failure here never breaks the sync result.
    reconcile_summary: Optional[dict] = None
    if not dry_run:
        try:
            reconcile_summary = reconcile_completion(state)
        except Exception as e:
            errors.append({"date": "reconcile", "error": f"{type(e).__name__}: {e}"})

    return {
        **counts,
        "errors": errors,
        "dry_run": dry_run,
        "reconcile": reconcile_summary,
    }


# ---------- plan rows ----------


def _plan_dict(session_row: dict) -> dict:
    """Adapt a `sessions` row into the plan-row shape the event-payload
    builders expect (date / workout / pace_target / notes)."""
    return {
        "date": session_row["date"],
        "workout": session_row.get("prescribed_workout") or "",
        "pace_target": session_row.get("prescribed_pace") or "",
        "notes": session_row.get("prescribed_notes") or "",
    }


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


# ---------- completion (mark_complete) ----------

# Log-entry types that satisfy a "run" prescription. Mirrors strava/review.RUN_TYPES
# but kept local so sync doesn't depend on the strava package.
_RUN_LOG_TYPES = {"run", "easy", "long_run", "workout", "race", "strides", "return_test"}

# Gcal "graphite" — neutral gray that visually fades completed events without
# clashing with any user-assigned color. ColorIds 1-11 are the event palette.
_COMPLETED_COLOR_ID = "8"


def _completion_event_id(iso_date: str) -> str:
    """Off-plan completion event id (separate from the prescription event).

    Used when the user logs activity that doesn't satisfy the day's
    prescription — the prescription event stays untouched, the off-plan
    activity gets its own marked-complete event on the same day.
    """
    return "precomplete" + iso_date.replace("-", "")


def _prescription_kind(workout_cell: str) -> Optional[str]:
    """Classify a plan row's Workout cell into a coarse prescription kind.

    Returns one of: "run" | "cross_train" | "strength" | "mobility" | "rest",
    or None if the cell is empty. The rule order matters — "rest" wins over
    everything else so "Rest + gentle yoga" doesn't classify as mobility.
    """
    if not workout_cell:
        return None
    w = workout_cell.lower().strip()
    if w in {"-", "—"}:
        return "rest"
    if w.startswith(("off", "rest", "no run", "no running")):
        return "rest"
    if any(k in w for k in ("cycl", "bike", "spin", "ride", "swim")):
        return "cross_train"
    if any(k in w for k in ("strength", "lift ", "gym ", "lifting")):
        return "strength"
    # Mobility-only days have no mileage / no run keyword. A row like
    # "Easy 4mi + restorative yoga" has a run prescription that yoga is
    # adjunct to — not a mobility-only day.
    if any(k in w for k in ("yoga", "mobility", "stretch", "walk")) and not any(
        k in w for k in ("mi ", "mi,", "mi.", "mi)", "run", "shakeout", "easy", "strides")
    ):
        return "mobility"
    return "run"


def _log_matches_prescription(kind: Optional[str], log_type: str) -> bool:
    """Whether a logged session type satisfies a prescription kind.

    Rest days never auto-mark — they have nothing to "complete." Mobility-only
    days don't auto-mark either (there's no yoga/mobility log type, and using
    strength would be misleading)."""
    if kind == "run":
        return log_type in _RUN_LOG_TYPES
    if kind == "cross_train":
        return log_type == "cross_train"
    if kind == "strength":
        return log_type == "strength"
    return False  # rest / mobility / None never auto-match


def _format_actual(entry: dict) -> str:
    """One-line summary of a log entry for the calendar event description."""
    parts: list[str] = [str(entry.get("type") or "session")]
    if entry.get("miles"):
        parts.append(f"{entry['miles']}mi")
    if entry.get("pace_avg"):
        parts.append(f"@ {entry['pace_avg']}")
    if entry.get("hr_avg"):
        parts.append(f"HR {entry['hr_avg']}")
    if entry.get("rpe"):
        parts.append(f"RPE {entry['rpe']}")
    details = entry.get("details") or {}
    elev = details.get("elevation_gain_ft") or details.get("elevation_ft")
    if elev:
        parts.append(f"{elev}ft")
    duration = details.get("moving_time") or details.get("duration")
    if duration:
        parts.append(str(duration))
    line = " ".join(parts)
    notes = entry.get("notes")
    if notes:
        line += f" — {notes}"
    return line


def _build_completed_payload(
    event_id: str,
    plan_row: Optional[dict],
    plan_detail_body: Optional[str],
    entries: list[dict],
    log_date: date,
) -> dict:
    """Build an insert/patch payload representing a completed event.

    Layout: optional prescription text on top (preserved when patching a
    prescribed event), then `--- Completed ---`, then one bullet per logged
    session. The full block is rebuilt from `entries` each call — idempotent
    under repeated mark_complete invocations.
    """
    if plan_row:
        workout_text = plan_row["workout"]
        body = (plan_detail_body or "").strip()
        if body:
            prescription_desc = body
        else:
            parts = []
            if plan_row.get("pace_target") and plan_row["pace_target"] not in {"-", "—"}:
                parts.append(f"Pace: {plan_row['pace_target']}")
            if plan_row.get("notes") and plan_row["notes"] not in {"-", "—"}:
                parts.append(f"Notes: {plan_row['notes']}")
            prescription_desc = "\n".join(parts)
    else:
        workout_text = "Off-plan activity"
        prescription_desc = ""

    actuals_lines = ["--- Completed ---"]
    for e in entries:
        actuals_lines.append("✓ " + _format_actual(e))
    actuals_block = "\n".join(actuals_lines)

    full_desc = prescription_desc + "\n\n" + actuals_block if prescription_desc else actuals_block
    full_desc = _clamp_description(full_desc) + "\n\n(marked complete by PRE)"

    summary = "✅ " + _strip_bold(workout_text)
    end = log_date + timedelta(days=1)
    return {
        "id": event_id,
        "summary": summary,
        "description": full_desc,
        "start": {"date": log_date.isoformat()},
        "end": {"date": end.isoformat()},
        "colorId": _COMPLETED_COLOR_ID,
        "extendedProperties": {
            "private": {
                "pre_managed": "1",
                "pre_completed": "1",
            },
        },
    }


def mark_complete(state, log_date) -> dict:
    """Reflect the day's logged sessions onto the calendar.

    Idempotent. Called from the log-write paths (Strava webhook +
    log_session tool) after each session is appended to log.jsonl.

    - Entries whose type satisfies the day's prescription patch the prescribed
      event (`pretrain<YYYYMMDD>`) with a ✅ summary, graphite color, and an
      aggregated actuals block.
    - Entries that don't satisfy the prescription (or any entries when no plan
      row exists for the date) are aggregated into a separate
      `precomplete<YYYYMMDD>` event so the prescribed row's completion state
      is never falsified.

    Returns a small dict summarizing what was updated. Errors are captured
    inline rather than raised — callers run this best-effort from background
    threads where a gcal hiccup shouldn't break the log-write path.
    """
    if isinstance(log_date, str):
        log_date = date.fromisoformat(log_date)

    entries = state.sessions_on_date(log_date)
    result: dict = {"ok": True, "log_date": log_date.isoformat()}
    if not entries:
        result["noop"] = True
        result["reason"] = "no log entries for date"
        return result

    srow = state.get_workout_row(log_date)
    plan_row = _plan_dict(srow) if srow else None
    plan_detail = srow.get("detail_md") if srow else None
    kind = _prescription_kind(plan_row["workout"]) if plan_row else None
    result["prescription_kind"] = kind

    matching: list[dict] = []
    off_plan: list[dict] = []
    for e in entries:
        log_type = str(e.get("type", ""))
        if plan_row and _log_matches_prescription(kind, log_type):
            matching.append(e)
        else:
            off_plan.append(e)

    logger.info(
        "mark_complete %s: %d matching, %d off_plan (prescription_kind=%s)",
        log_date.isoformat(),
        len(matching),
        len(off_plan),
        kind,
    )

    from . import client

    sync_state = _load_sync_state(state)
    prescribed_id = _event_id(log_date.isoformat())
    offplan_id = _completion_event_id(log_date.isoformat())

    if matching and plan_row:
        payload = _build_completed_payload(prescribed_id, plan_row, plan_detail, matching, log_date)
        outcome = _apply_completed_event(client, prescribed_id, payload)
        result["prescribed"] = outcome
        # Only stamp the completion sentinel when gcal actually accepted the
        # write — see #31. Setting completed=True on an error poisoned sync
        # state and made reconcile permanently skip the date even after the
        # underlying gcal issue (e.g. expired token) was fixed.
        if outcome.get("action") in {"inserted", "patched"}:
            sync_state[prescribed_id] = {
                **(sync_state.get(prescribed_id) or {}),
                "completed": True,
                "last_completed_at": _now_iso(),
            }

    if off_plan:
        payload = _build_completed_payload(offplan_id, None, None, off_plan, log_date)
        outcome = _apply_completed_event(client, offplan_id, payload)
        result["off_plan"] = outcome
        if outcome.get("action") in {"inserted", "patched"}:
            sync_state[offplan_id] = {
                **(sync_state.get(offplan_id) or {}),
                "completed": True,
                "off_plan": True,
                "last_completed_at": _now_iso(),
            }
    else:
        # No off-plan entries this call. If a precomplete event lingers from a
        # previous call that mis-classified the same activity (Strava commonly
        # fires create with a generic sport then update with the proper type
        # — the first call partitions to off_plan, the second flips matching),
        # clean it up so the user doesn't end up with a duplicate ✅ event on
        # the same day.
        if offplan_id in sync_state:
            try:
                client.delete_event(offplan_id)
                sync_state.pop(offplan_id, None)
                result["off_plan_cleanup"] = "deleted"
                logger.info("Deleted stale off-plan event %s", offplan_id)
            except Exception as e:
                result["off_plan_cleanup_error"] = f"{type(e).__name__}: {e}"
                logger.warning("Failed to delete stale off-plan event %s: %s", offplan_id, e)

    try:
        _write_sync_state(state, sync_state)
    except Exception as e:
        result["sync_state_error"] = f"{type(e).__name__}: {e}"

    return result


def _apply_completed_event(client_mod, event_id: str, payload: dict) -> dict:
    """Insert if missing, otherwise patch. Returns a small action summary.

    Tries insert first because the typical case for a prescribed-event update
    is "event exists, we're toggling completion fields" — `insert` 409s, we
    patch. For off-plan, the first call inserts cleanly; subsequent calls on
    the same day 409 and patch (aggregation).
    """
    try:
        client_mod.insert_event(payload)
        return {"action": "inserted", "event_id": event_id}
    except client_mod.GcalEventExistsError:
        patch_payload = {k: v for k, v in payload.items() if k != "id"}
        try:
            client_mod.patch_event(event_id, patch_payload)
            return {"action": "patched", "event_id": event_id}
        except Exception as e:
            return {"action": "error", "event_id": event_id, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"action": "error", "event_id": event_id, "error": f"{type(e).__name__}: {e}"}


# ---------- sync-state file ----------


def _load_sync_state(state) -> dict[str, dict]:
    """Load the per-event sync state via StateManager.

    Corruption / DB errors are self-healing: return ``{}`` and let the next
    sync re-check everything via the 409 path. We log the failure so it's
    visible without breaking the call.
    """
    try:
        return state.load_gcal_sync_state()
    except Exception as e:  # noqa: BLE001 — never let storage hiccup break sync
        logger.warning("Could not read gcal sync state: %s", e)
        return {}


def _write_sync_state(state, sync_state: dict[str, dict]) -> None:
    state.save_gcal_sync_state(sync_state)


def _now_iso() -> str:
    # datetime.utcnow() is deprecated in 3.12 and slated for removal; use a
    # tz-aware UTC datetime and emit the trailing 'Z' shape gcal expects.
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _iso_z(d: date) -> str:
    """Format a date as RFC3339 UTC midnight — what gcal's timeMin/timeMax want."""
    return f"{d.isoformat()}T00:00:00Z"


def get_last_sync_summary(state=None) -> Optional[dict]:
    """Return a small summary of the gcal sync state (for status tooling).

    {count: N, last_synced_at: <max iso>, source: 'sqlite'} or None if absent.
    Constructs a default ``StateManager`` if one isn't passed (status endpoints
    don't always have one wired through).
    """
    if state is None:
        from state_manager import StateManager

        state = StateManager()
    sync_state = _load_sync_state(state)
    if not sync_state:
        return None
    last_at = max(
        (v.get("last_synced_at") for v in sync_state.values() if isinstance(v, dict) and v.get("last_synced_at")),
        default=None,
    )
    return {
        "count": len(sync_state),
        "last_synced_at": last_at,
        "source": "sqlite",
    }


# ---------- reconcile_completion ----------


def reconcile_completion(state, days_back: int = 14) -> dict:
    """Walk recent plan rows + log entries; ensure gcal completion matches reality.

    For every plan-row date inside the ``days_back`` window that has at least
    one logged session and isn't already marked complete in sync state, fire
    ``mark_complete``. Skipping already-completed dates is the API-idempotency
    win — mark_complete is safe to re-fire (the sentinel preserves state) but
    each call costs a gcal insert+patch round-trip.

    Orphan detection (gcal says completed but the log has no matching session)
    is surfaced as a warning. We never uncomplete an event by default — that
    would erase history if a log entry was accidentally deleted.
    """
    today = today_local()
    cutoff = today - timedelta(days=days_back)
    rows = state.get_prescription_rows(cutoff, today)
    sync_state = _load_sync_state(state)

    corrected: list[dict] = []
    skipped: list[str] = []
    already_complete: list[str] = []
    errors: list[dict] = []

    for row in rows:
        try:
            d = date.fromisoformat(row["date"])
        except ValueError:
            continue
        if not (cutoff <= d <= today):
            continue
        if not state.sessions_on_date(d):
            skipped.append(row["date"])
            continue
        prescribed_id = _event_id(row["date"])
        if sync_state.get(prescribed_id, {}).get("completed"):
            already_complete.append(row["date"])
            continue
        try:
            outcome = mark_complete(state, d)
            corrected.append(
                {
                    "date": row["date"],
                    "kind": outcome.get("prescription_kind"),
                    "prescribed": bool(outcome.get("prescribed")),
                    "off_plan": bool(outcome.get("off_plan")),
                }
            )
        except Exception as e:  # noqa: BLE001 — individual date failure shouldn't kill reconcile
            errors.append({"date": row["date"], "error": f"{type(e).__name__}: {e}"})

    orphans = _find_orphan_completions(state, cutoff, today)

    summary = {
        "days_back": days_back,
        "corrected": corrected,
        "skipped": skipped,
        "already_complete": already_complete,
        "orphans": orphans,
        "errors": errors,
    }
    logger.info(
        "reconcile_completion: corrected=%d already=%d skipped=%d orphans=%d errors=%d",
        len(corrected),
        len(already_complete),
        len(skipped),
        len(orphans),
        len(errors),
    )
    return summary


def _find_orphan_completions(state, cutoff: date, today: date) -> list[dict]:
    """Return events marked ``completed: True`` in sync state whose date has
    no matching log entry. Caller treats as warnings — no mutation here."""
    out: list[dict] = []
    sync_state = _load_sync_state(state)
    for event_id, entry in sync_state.items():
        if not isinstance(entry, dict) or not entry.get("completed"):
            continue
        d = _date_from_event_id(event_id)
        if d is None or not (cutoff <= d <= today):
            continue
        if state.sessions_on_date(d):
            continue
        out.append({"event_id": event_id, "date": d.isoformat()})
    return out


def _date_from_event_id(event_id: str) -> Optional[date]:
    """Parse YYYYMMDD suffix from pretrain/precomplete event IDs."""
    for prefix in ("pretrain", "precomplete"):
        if event_id.startswith(prefix):
            digits = event_id[len(prefix) :]
            if len(digits) == 8 and digits.isdigit():
                try:
                    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
                except ValueError:
                    return None
    return None
