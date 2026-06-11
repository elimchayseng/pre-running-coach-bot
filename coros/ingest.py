"""Nightly COROS pull orchestration: fetch bundle -> merge -> upsert.

Idempotent: re-running for the same window upserts the same rows, and the
per-column COALESCE in upsert_daily_health means partial data never erases
previously captured values. The default window backfills a few days so a
missed night heals on the next successful run.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from coros import client, translator
from temporal_context import today_local

logger = logging.getLogger("pre_coach.coros.ingest")

DEFAULT_BACKFILL_DAYS = 4


def _backfill_days() -> int:
    try:
        return int(os.getenv("COROS_BACKFILL_DAYS", str(DEFAULT_BACKFILL_DAYS)))
    except ValueError:
        return DEFAULT_BACKFILL_DAYS


def run_nightly_pull(state, days: Optional[int] = None, dry_run: bool = False) -> dict:
    """Pull the daily tool bundle and upsert one daily_health row per date.

    Returns {"dates": [...], "fields_parsed": n, "errors": [...]}.
    `fields_parsed == 0` with a non-empty bundle means COROS changed its
    output format — surfaced as an error so the scheduler can alert.
    Raises CorosAuthError straight through (the scheduler's watchdog
    classifies that as needs_auth and Telegram-alerts).
    """
    window = days or _backfill_days()
    today = today_local()
    errors: list[str] = []

    bundle = client.fetch_daily_bundle(days=window)
    missing = [t for t in client.BUNDLE_TOOLS if t not in bundle]
    if missing:
        errors.append(f"tools failed/skipped: {', '.join(missing)}")
    if not bundle:
        return {"dates": [], "fields_parsed": 0, "errors": errors + ["empty bundle — nothing fetched"]}

    rows = translator.merge_daily_rows(bundle, today=today)
    fields_parsed = sum(
        1 for row in rows for key in translator.METRIC_KEYS if row.get(key) is not None
    )
    if rows and fields_parsed == 0:
        errors.append(
            "0 fields parsed from a non-empty bundle — COROS output format "
            "may have changed (raw payloads are stored; see docs/coros-mcp.md)"
        )

    if not dry_run and rows:
        state.upsert_daily_health(rows)

    result = {
        "dates": [row["date"] for row in rows],
        "fields_parsed": fields_parsed,
        "errors": errors,
    }
    logger.info(
        "COROS pull: %d dates, %d fields parsed%s%s",
        len(rows),
        fields_parsed,
        " (dry-run)" if dry_run else "",
        f", errors: {errors}" if errors else "",
    )
    return result
