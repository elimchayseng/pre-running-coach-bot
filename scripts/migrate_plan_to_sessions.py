"""Phase 1A migration: plan.md blob + sessions rows → unified sessions_v2.

This populates the dormant ``sessions_v2`` and ``plan_meta`` tables (schema v3)
from the live ``plan`` blob and the completed-only ``sessions`` table. It does
NOT rename or drop anything — the old tables stay live until the Phase 1A
cutover PR. After this runs, ``sessions_v2`` holds:

  - one ``status='planned'`` row per locked-table row in plan.md (slot NULL),
    carrying the prescription and any ``#### YYYY-MM-DD`` detail body;
  - one ``status='completed'`` row per old ``sessions`` row, merged onto the
    matching planned row for that date when one exists (prescription kept,
    actuals filled), else inserted standalone.

``plan_meta.content`` gets everything in plan.md *outside* the locked weekly
table and the per-day detail blocks — phases, goals, pace zones, adjustment
triggers. The runner can hand-edit it afterwards.

Idempotent: if ``sessions_v2`` already has rows, the script reports a no-op and
exits 0. Pass ``--force`` to wipe ``sessions_v2`` + ``plan_meta`` and re-run.

Usage:
    ./venv/bin/python scripts/migrate_plan_to_sessions.py
    ./venv/bin/python scripts/migrate_plan_to_sessions.py --db /path/to/copy.db
    ./venv/bin/python scripts/migrate_plan_to_sessions.py --force
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_calendar.sync import _parse_plan_rows, _parse_workout_details  # noqa: E402
from state_manager import StateManager  # noqa: E402

# Locked weekly-table header — same contract enforced everywhere else.
_LOCKED_HEADER_TOKENS = ("Day", "Date", "Workout", "Pace target", "Notes")
_DETAIL_HEADING_RE = re.compile(r"^####\s+\d{4}-\d{2}-\d{2}\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")

# Old sessions.type → Phase 1A type vocab. Anything not listed passes through
# verbatim (return_test, weekly_summary, injury_event, milestone, etc.).
_TYPE_REMAP = {"cross_train": "cross", "long_run": "long"}

# Run-shaped session types. Used to bucket a day's actuals when matching them
# to a planned row — keeps a strength/cross actual off a run prescription.
_RUN_LIKE = {"run", "easy", "workout", "long", "race", "strides", "return_test", "tempo"}


def _norm_type(t: str | None) -> str:
    return _TYPE_REMAP.get((t or "").strip().lower(), (t or "").strip().lower())


def _pick_actual(planned_type: str, pool: list):
    """Choose which completed session merges onto a planned row.

    Prefers an exact type match, then a same-bucket match (run-like vs other),
    then the first remaining session. Stops a strength actual from landing on
    a cycling prescription, or a cross actual on a run prescription.
    """
    for s in pool:
        if _norm_type(s["type"]) == planned_type:
            return s
    planned_run = planned_type in _RUN_LIKE
    for s in pool:
        if (_norm_type(s["type"]) in _RUN_LIKE) == planned_run:
            return s
    return pool[0]


def _infer_planned_type(workout: str) -> str:
    """Best-effort workout-type classification for a planned table row.

    Heuristic, not authoritative — the runner can correct plan_meta / rows
    afterwards. Order matters: race, intervals and the run-distance check all
    run before the cross-training keyword bucket, so a run that merely
    *mentions* yoga or strides is still classified as a run, not cross.
    """
    w = workout.strip().lower()
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
    # A run component dominates: "8mi", "3mi shakeout", "easy run". The
    # \b after mi(le|les)? keeps "75min" / "20min" from reading as a run.
    if re.search(r"\d+\s*mi(le|les)?\b", w) or "shakeout" in w or "easy run" in w:
        return "easy"
    if any(k in w for k in ("walk", "mobility", "off day")):
        return "rest"
    if any(k in w for k in ("cycl", "spin", "swim", "bike", "ride", "yoga", "cross")):
        return "cross"
    if "strength" in w or "lift" in w:
        return "strength"
    return "easy"


def _build_plan_meta(plan_text: str) -> str:
    """Return plan.md with the locked weekly table and every ``#### date``
    detail block removed — the prose remainder for plan_meta."""
    lines = plan_text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        # Strip a locked-format table: header + separator + pipe data rows.
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if "|" in line and len(parts) >= 5 and tuple(parts[:5]) == _LOCKED_HEADER_TOKENS:
            i += 1  # skip header
            if i < n and "|" in lines[i]:
                i += 1  # skip separator
            while i < n and "|" in lines[i] and lines[i].strip():
                i += 1
            continue
        # Strip a per-day detail block: heading until the next heading / EOF.
        if _DETAIL_HEADING_RE.match(line.strip()):
            i += 1
            while i < n and not _HEADING_RE.match(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    # Collapse runs of 3+ blank lines left behind by the stripping.
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


def migrate(db_path: Path, force: bool = False) -> dict:
    sm = StateManager()
    sm.db_path = db_path
    sm.state_dir = db_path.parent
    sm._schema_applied = False

    summary = {
        "planned_inserted": 0,
        "completed_merged": 0,
        "completed_inserted": 0,
        "plan_meta_chars": 0,
        "no_op": False,
    }

    with sm._conn() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM sessions_v2").fetchone()[0]
        if existing and not force:
            summary["no_op"] = True
            return summary
        if existing and force:
            conn.execute("DELETE FROM sessions_v2")
            conn.execute("DELETE FROM plan_meta")

        plan_row = conn.execute("SELECT content FROM plan WHERE id = 1").fetchone()
        plan_text = plan_row["content"] if plan_row else ""

        # --- 1. planned rows from the locked weekly table ---
        rows = _parse_plan_rows(plan_text)
        details = _parse_workout_details(plan_text)
        planned_by_date: dict[str, list[dict]] = {}
        for r in rows:
            ptype = _infer_planned_type(r["workout"])
            cur = conn.execute(
                "INSERT INTO sessions_v2 "
                "(date, slot, status, type, prescribed_workout, prescribed_pace, "
                " prescribed_notes, detail_md) "
                "VALUES (?, NULL, 'planned', ?, ?, ?, ?, ?)",
                (
                    r["date"],
                    ptype,
                    r["workout"],
                    r["pace_target"],
                    r["notes"],
                    details.get(r["date"]),
                ),
            )
            planned_by_date.setdefault(r["date"], []).append(
                {"id": cur.lastrowid, "type": ptype}
            )
            summary["planned_inserted"] += 1

        # --- 2. completed rows from the old sessions table ---
        # Group a day's actuals so each can be matched (by type) to its
        # planned row; leftover actuals become standalone completed rows.
        old_sessions = conn.execute(
            "SELECT date, type, data FROM sessions ORDER BY date, id"
        ).fetchall()
        sessions_by_date: dict[str, list] = defaultdict(list)
        for s in old_sessions:
            sessions_by_date[s["date"]].append(s)

        for sdate, day_sessions in sessions_by_date.items():
            pool = list(day_sessions)
            for prow in planned_by_date.get(sdate, []):
                if not pool:
                    break
                chosen = _pick_actual(prow["type"], pool)
                pool.remove(chosen)
                # Keep the planned row's type + prescription; fill the actuals.
                conn.execute(
                    "UPDATE sessions_v2 SET status='completed', data=?, "
                    "completed_at=?, updated_at=datetime('now') WHERE id=?",
                    (chosen["data"], sdate, prow["id"]),
                )
                summary["completed_merged"] += 1
            for s in pool:
                conn.execute(
                    "INSERT INTO sessions_v2 "
                    "(date, slot, status, type, data, completed_at) "
                    "VALUES (?, NULL, 'completed', ?, ?, ?)",
                    (sdate, _norm_type(s["type"]), s["data"], sdate),
                )
                summary["completed_inserted"] += 1

        # --- 3. plan_meta from the prose remainder ---
        meta = _build_plan_meta(plan_text)
        conn.execute(
            "INSERT INTO plan_meta (id, content) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET content = excluded.content, "
            "updated_at = datetime('now')",
            (meta,),
        )
        summary["plan_meta_chars"] = len(meta)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: $DATABASE_PATH or state/coach.db)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe sessions_v2 + plan_meta and re-run (otherwise a populated "
        "sessions_v2 makes this a no-op).",
    )
    args = parser.parse_args()

    if args.db:
        db_path = Path(args.db)
    else:
        import os

        db_path = Path(os.getenv("DATABASE_PATH") or (ROOT / "state" / "coach.db"))

    print(f"migrating: {db_path}")
    summary = migrate(db_path, force=args.force)
    if summary["no_op"]:
        print("sessions_v2 already populated — no-op. Pass --force to re-run.")
        return 0
    print("done:")
    for k, v in summary.items():
        if k == "no_op":
            continue
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
