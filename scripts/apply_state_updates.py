"""Apply a state-update JSON document to the local state/ files.

Input format (see docs/sync_prompt.md for the full schema):
{
  "athlete_updates": { ... partial structure ... },
  "plan_md": "<full new plan.md or null>",
  "plan_change_reason": "<reason or null>",
  "log_entries": [ {date, type, ...}, ... ],
  "journal_entries": [ "body text", ... ]
}

Usage:
    ./venv/bin/python scripts/apply_state_updates.py state_updates.json [--dry-run]

Always commit `git diff state/` before trusting the run; this script writes
to disk eagerly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from state_manager import StateManager  # noqa: E402


def apply(updates: dict, state: StateManager, dry_run: bool = False) -> None:
    summary: list[str] = []

    athlete = updates.get("athlete_updates") or {}
    if athlete:
        summary.append(f"athlete.yaml: patch top-level keys {sorted(athlete.keys())}")
        if not dry_run:
            state.update_athlete(athlete)

    plan = updates.get("plan_md")
    reason = updates.get("plan_change_reason")
    if plan:
        if not reason:
            raise ValueError("plan_md provided but plan_change_reason is null")
        summary.append(f"plan.md: replace ({len(plan)} chars) — reason: {reason}")
        if not dry_run:
            state.update_plan(plan, reason)

    log_entries = updates.get("log_entries") or []
    if log_entries:
        summary.append(f"log.jsonl: append {len(log_entries)} session(s)")
        if not dry_run:
            for entry in log_entries:
                if "date" not in entry or "type" not in entry:
                    raise ValueError(f"log entry missing date/type: {entry}")
                state.append_session(entry)

    journal_entries = updates.get("journal_entries") or []
    if journal_entries:
        summary.append(f"journal.md: append {len(journal_entries)} entry(ies)")
        if not dry_run:
            for entry in journal_entries:
                if not isinstance(entry, str) or not entry.strip():
                    continue
                state.append_journal(entry)

    if not summary:
        print("No updates to apply.")
        return

    prefix = "[dry-run] would apply:" if dry_run else "applied:"
    print(prefix)
    for line in summary:
        print(f"  - {line}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a state-update JSON to state/")
    parser.add_argument("path", help="Path to the JSON file produced by the sync prompt")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing")
    parser.add_argument("--state-dir", default=str(ROOT / "state"))
    args = parser.parse_args()

    text = Path(args.path).read_text()
    try:
        updates = json.loads(text)
    except json.JSONDecodeError as e:
        # Handle the case where the model wrapped JSON in ```json fences
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("```", 2)[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.rsplit("```", 1)[0]
            try:
                updates = json.loads(stripped)
            except json.JSONDecodeError:
                print(f"Could not parse JSON: {e}", file=sys.stderr)
                return 1
        else:
            print(f"Could not parse JSON: {e}", file=sys.stderr)
            return 1

    state = StateManager(Path(args.state_dir))
    apply(updates, state, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
