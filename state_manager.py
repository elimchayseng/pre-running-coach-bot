"""State management for the running coach.

Reads and writes the four state files under state_dir/:
  athlete.yaml — identity, goals, zones, preferences (round-trip preserved)
  plan.md      — current training plan (freeform markdown)
  log.jsonl    — append-only session log
  journal.md   — freeform timestamped notes

Every coach turn calls load_full_context() to inject state into the system
prompt. Tools call the writers (append_session, update_plan, ...) to persist
changes back to disk.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from ruamel.yaml import YAML

# Round-trip YAML — preserves comments, key order, quotes. update_athlete()
# depends on this; PyYAML would silently drop them.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


class StateManager:
    """Reads and writes the four state files for the running coach."""

    def __init__(self, state_dir: Path | str = Path("state")) -> None:
        self.state_dir = Path(state_dir)
        self.athlete_path = self.state_dir / "athlete.yaml"
        self.plan_path = self.state_dir / "plan.md"
        self.log_path = self.state_dir / "log.jsonl"
        self.journal_path = self.state_dir / "journal.md"
        self.changelog_path = self.state_dir / "plan_changelog.md"

    # ---------- Readers ----------

    def load_athlete(self) -> dict:
        """Parse athlete.yaml into a dict. Returns {} if file missing."""
        if not self.athlete_path.exists():
            return {}
        with self.athlete_path.open("r", encoding="utf-8") as f:
            data = _yaml.load(f)
        return data or {}

    def load_plan(self) -> str:
        if not self.plan_path.exists():
            return ""
        return self.plan_path.read_text(encoding="utf-8")

    def load_journal(self, max_entries: Optional[int] = None) -> str:
        """Return journal.md, optionally truncated to last N entries.

        Entries are separated by horizontal-rule blocks (\\n---\\n). The
        preamble (everything before the first separator) is preserved.
        """
        if not self.journal_path.exists():
            return ""
        text = self.journal_path.read_text(encoding="utf-8")
        if max_entries is None:
            return text
        sections = text.split("\n---\n")
        if len(sections) <= 1 or max_entries < 0:
            return text
        head, *entries = sections
        kept = entries[-max_entries:] if max_entries > 0 else []
        return "\n---\n".join([head, *kept])

    def get_recent_sessions(self, days: int = 14, today: Optional[date] = None) -> list[dict]:
        ref = today or date.today()
        cutoff = ref - timedelta(days=days)
        return self._load_log_entries(lambda d: cutoff <= d <= ref)

    def get_sessions_in_range(self, start: date, end: date) -> list[dict]:
        return self._load_log_entries(lambda d: start <= d <= end)

    def sessions_on_date(self, target: date) -> list[dict]:
        return self._load_log_entries(lambda d: d == target)

    def existing_strava_ids(self) -> set[int]:
        """Return all `details.strava_id` values from log.jsonl. Used by
        Strava backfill + webhook handler for idempotency."""
        if not self.log_path.exists():
            return set()
        out: set[int] = set()
        with self.log_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                sid = (entry.get("details") or {}).get("strava_id")
                if isinstance(sid, int):
                    out.add(sid)
                elif isinstance(sid, str) and sid.isdigit():
                    out.add(int(sid))
        return out

    def _load_log_entries(self, date_pred: Callable[[date], bool]) -> list[dict]:
        if not self.log_path.exists():
            return []
        out: list[dict] = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # skip malformed lines, keep loading
                d = _parse_entry_date(entry.get("date"))
                if d is None:
                    continue
                if date_pred(d):
                    out.append(entry)
        return out

    # ---------- Composite read for system prompt ----------

    def load_full_context(self, recent_days: int = 21, journal_entries: int = 5) -> str:
        """Format all state into a single markdown blob for the system prompt."""
        athlete_yaml = self.athlete_path.read_text(encoding="utf-8") if self.athlete_path.exists() else ""
        plan_text = self.load_plan()
        recent = self.get_recent_sessions(days=recent_days)
        journal = self.load_journal(max_entries=journal_entries)

        parts = [
            "=== ATHLETE PROFILE ===",
            "```yaml",
            athlete_yaml.rstrip(),
            "```",
            "",
            "=== TRAINING PLAN ===",
            plan_text.rstrip(),
            "",
            f"=== RECENT SESSIONS (last {recent_days} days) ===",
            *(json.dumps(e, ensure_ascii=False) for e in recent),
            "",
            f"=== JOURNAL (last {journal_entries} entries) ===",
            journal.rstrip(),
        ]
        return "\n".join(parts)

    # ---------- Writers ----------

    def append_session(self, session: dict) -> None:
        """Append a JSON entry to log.jsonl. Requires a 'date' field."""
        if "date" not in session:
            raise ValueError("session entry must include a 'date' field")
        line = json.dumps(session, ensure_ascii=False) + "\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line)

    def update_session_by_strava_id(self, activity_id: int, new_entry: dict) -> bool:
        """Find the log.jsonl line with details.strava_id == activity_id and
        replace it with new_entry. Atomic via tempfile + rename.

        Returns True if the entry was found and replaced; False if no matching
        entry exists (caller decides whether to append instead).

        Used by the Strava webhook handler when an aspect_type=update event
        fires (e.g., the user retags a Run as a Workout after upload).
        """
        if "date" not in new_entry:
            raise ValueError("new_entry must include a 'date' field")
        if not self.log_path.exists():
            return False

        replaced = False
        out_lines: list[str] = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    out_lines.append(raw)
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    # Preserve malformed lines untouched
                    out_lines.append(raw)
                    continue
                sid = (entry.get("details") or {}).get("strava_id")
                if sid == activity_id and not replaced:
                    out_lines.append(json.dumps(new_entry, ensure_ascii=False) + "\n")
                    replaced = True
                else:
                    out_lines.append(raw if raw.endswith("\n") else raw + "\n")

        if not replaced:
            return False
        _atomic_write(self.log_path, "".join(out_lines))
        return True

    def update_plan(self, new_plan_md: str, change_note: str) -> None:
        """Replace plan.md atomically and append change_note to plan_changelog.md."""
        _atomic_write(self.plan_path, new_plan_md)
        ts = datetime.now().isoformat(timespec="seconds")
        entry = f"- {ts}: {change_note}\n"
        self.changelog_path.parent.mkdir(parents=True, exist_ok=True)
        with self.changelog_path.open("a", encoding="utf-8") as f:
            f.write(entry)

    def append_journal(self, entry: str, when: Optional[datetime] = None) -> None:
        """Append a timestamped entry to journal.md."""
        ts = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
        block = f"\n---\n\n## {ts}\n\n{entry.rstrip()}\n"
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.journal_path.exists():
            block = "# Journal\n\nAppend-only freeform notes. Newest entries at the bottom.\n" + block
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(block)

    def update_athlete(self, updates: dict) -> None:
        """Patch fields in athlete.yaml. Preserves comments and key order."""
        if not self.athlete_path.exists():
            raise FileNotFoundError(f"athlete file not found: {self.athlete_path}")
        with self.athlete_path.open("r", encoding="utf-8") as f:
            data = _yaml.load(f)
        if data is None:
            data = {}
        _deep_merge(data, updates)
        buf = io.StringIO()
        _yaml.dump(data, buf)
        _atomic_write(self.athlete_path, buf.getvalue())

    # ---------- Today's workout ----------

    def get_todays_workout(self, target_date: Optional[date] = None) -> dict:
        """Parse the locked-format 'This Week' table from plan.md.

        Locked format: | Day | Date | Workout | Pace target | Notes |
        Date column may be ISO (2026-04-28) or M/D (4/28).

        Returns a dict with keys: date, day_name, workout, pace_target,
        notes, is_rest_day, found.
        """
        if target_date is None:
            target_date = date.today()
        result = {
            "date": target_date.isoformat(),
            "day_name": target_date.strftime("%A"),
            "workout": "",
            "pace_target": "",
            "notes": "",
            "is_rest_day": False,
            "found": False,
        }
        plan_text = self.load_plan()
        if not plan_text:
            return result

        row = _find_workout_row(plan_text, target_date)
        if row is None:
            return result
        result.update(row)
        result["found"] = True
        result["is_rest_day"] = _is_rest_day(result["workout"])
        return result


# ---------- module helpers ----------


def _parse_entry_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursive dict merge; src overrides dst, lists replace not extend."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


_REST_PATTERNS = ("off", "rest", "no run", "no running")


def _is_rest_day(workout: str) -> bool:
    w = workout.strip().lower()
    if not w or w in {"-", "—"}:
        return True
    return any(w.startswith(p) for p in _REST_PATTERNS)


def _find_workout_row(plan_text: str, target_date: date) -> Optional[dict]:
    iso = target_date.isoformat()
    md = f"{target_date.month}/{target_date.day}"
    for line in plan_text.splitlines():
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 5:
            continue
        cell = parts[1]
        if cell == iso or cell == md or iso in cell or md in cell:
            return {
                "day_name": parts[0],
                "date": iso,
                "workout": parts[2],
                "pace_target": parts[3],
                "notes": parts[4],
            }
    return None


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via tempfile + rename (crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
