# State sync prompt

Use this to extract updates from any other Claude conversation (e.g., a coaching
chat on claude.ai) into the bot's state files.

## Workflow

1. Copy the current state into the other conversation (filling in the
   placeholder blocks below). Dump the live state with
   `python scripts/state_dump.py --all`.
2. Paste the prompt.
3. Save the JSON output it produces as `state_updates.json` in the repo root.
4. Run:

   ```bash
   ./venv/bin/python scripts/apply_state_updates.py state_updates.json
   ```

   Writes go through `StateManager`, so this updates SQLite (`$DATABASE_PATH`
   or `state/coach.db` by default). Verify with
   `python scripts/state_dump.py log` etc. To apply against prod, set
   `DATABASE_PATH=/app/data/coach.db` via a pulled snapshot or run inside
   `railway shell --service web`.

---

## Prompt to paste

````
You are helping me sync coaching context from this conversation into a
structured state app (PRE coach bot) that tracks an athlete profile,
training plan, session log, and journal across four files. Read the
current state below, then extract any UPDATES from our conversation that
aren't already reflected.

=== CURRENT athlete.yaml ===
<paste contents>

=== CURRENT plan.md ===
<paste contents>

=== CURRENT log.jsonl (last 30 days) ===
<paste relevant lines>

=== CURRENT journal.md (last 5 entries) ===
<paste recent entries>

=== END CURRENT STATE ===

Output a single JSON document with this exact structure. No markdown
fences, no commentary outside the JSON.

{
  "athlete_updates": {
    // Partial structure to deep-merge into athlete.yaml.
    // Top-level keys can include: target_races, prs, zones, hr_zones,
    // preferences, training_characteristics, injury_history, race_history,
    // weekly_volume_ceiling_miles, cross_training, strength_training.
    // LISTS REPLACE — to add to a list, include the full new list.
    // Omit this key entirely if no athlete updates.
  },
  "plan_md": "<full new plan.md if it should change, else null>",
  "plan_change_reason": "<short reason if plan_md is set, else null>",
  "log_entries": [
    // New sessions to append. Each entry:
    //   {date: "YYYY-MM-DD",
    //    type: "run|easy|long_run|workout|race|strides|return_test|cross_train|strength|injury_event|milestone|weekly_summary|pt_diagnosis",
    //    miles?: number, pace_avg?: "M:SS", hr_avg?: int, rpe?: 1-10,
    //    notes?: string, details?: object}
    // Skip entries already present above. Use ISO dates.
  ],
  "journal_entries": [
    // Body text only — NO date or section headers (added automatically).
    // One entry per distinct event or note.
  ]
}

Rules:
- Output ONLY valid JSON, parseable by json.loads
- Omit keys entirely if nothing changed for them
- Use null where indicated, not empty strings
- Be conservative — only surface things actually discussed in this conversation
- Preserve the locked
  "| Day | Date | Workout | Pace target | Notes |"
  table format if you regenerate plan.md
````
