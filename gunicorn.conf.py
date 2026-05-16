"""Gunicorn configuration.

The Procfile runs ``gunicorn app:app``; gunicorn auto-loads this file. Its one
job is the ``on_starting`` hook: it runs the Phase 1A.2 database cutover once,
in the master process, before any worker forks or serves a request — so new
code never sees the pre-cutover table shape. Doing it here (rather than at
``import app``) keeps the migration off the import path, so importing the app
in tests or tooling has no side effects.
"""

import logging
import os
import sys
from pathlib import Path


def on_starting(server):  # noqa: ARG001 — gunicorn hook signature
    """Migrate an existing DB to the unified `sessions` schema.

    Idempotent: a no-op once a DB is cut over, and for a fresh install (the
    StateManager builds the v4 schema directly on first connect). A failure
    propagates and aborts startup — far safer than serving new code against
    old tables.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from scripts.cutover_to_unified_sessions import cutover

    db_path = Path(os.getenv("DATABASE_PATH") or "state/coach.db")
    log = logging.getLogger("gunicorn.error")
    if not db_path.exists():
        return
    summary = cutover(db_path)
    if summary["already_cut_over"]:
        log.info("Phase 1A.2 cutover: already done")
    else:
        log.info("Phase 1A.2 cutover applied: %s", "; ".join(summary["renamed"]))
