"""Daily backup of coach.db to a state-snapshot git branch.

Run from a Railway scheduled job once a day. Uses ``sqlite3 .backup`` (online
backup API — safe to run while the bot is writing) to produce a consistent
snapshot, then commits and pushes the dump to GitHub on a dedicated branch.

Required env vars:
    DATABASE_PATH          path to the live coach.db (e.g. /app/data/coach.db)
    GITHUB_BACKUP_TOKEN    a fine-scoped PAT or deploy token with write access
                           to GITHUB_REPO. We never log this.
    GITHUB_REPO            "owner/repo", e.g. "elimchayseng/pre-running-coach-bot"
    BACKUP_BRANCH          (optional) branch name; default "state-snapshot"
    BACKUP_FORMAT          (optional) "binary" (default, faster restore) or
                           "sql" (text dump, human-diffable)

Restoring: clone the repo, check out the snapshot branch, copy coach.db to the
Railway volume (or apply coach.sql via ``sqlite3 fresh.db < coach.sql``).
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("backup_db")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("missing required env var: %s", name)
        sys.exit(2)
    return value


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, masking GITHUB_BACKUP_TOKEN in any logged output."""
    logger.info("run: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, capture_output=True, text=True)


def _online_backup(src: Path, dst: Path) -> None:
    """Use SQLite's online backup API for a consistent snapshot."""
    with sqlite3.connect(str(src)) as src_conn:
        with sqlite3.connect(str(dst)) as dst_conn:
            src_conn.backup(dst_conn)


def _sql_dump(src: Path, dst: Path) -> None:
    """Produce a text dump for diff-friendly snapshots."""
    with sqlite3.connect(str(src)) as conn:
        with dst.open("w", encoding="utf-8") as f:
            for line in conn.iterdump():
                f.write(f"{line}\n")


def main() -> int:
    db_path = Path(_required("DATABASE_PATH"))
    if not db_path.exists():
        logger.error("DB not found at %s", db_path)
        return 2
    token = _required("GITHUB_BACKUP_TOKEN")
    repo = _required("GITHUB_REPO")
    branch = os.getenv("BACKUP_BRANCH", "state-snapshot")
    fmt = os.getenv("BACKUP_FORMAT", "binary").lower()
    if fmt not in {"binary", "sql"}:
        logger.error("invalid BACKUP_FORMAT=%r (expected binary|sql)", fmt)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="coach-backup-"))
    try:
        # 1. Clone the snapshot branch (shallow). If it doesn't exist yet,
        #    create an orphan branch.
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        try:
            _run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)])
            existing = True
        except subprocess.CalledProcessError:
            logger.info("branch %s not found; creating orphan", branch)
            _run(["git", "clone", "--depth", "1", clone_url, str(workdir)])
            _run(["git", "checkout", "--orphan", branch], cwd=workdir)
            _run(["git", "rm", "-rf", "."], cwd=workdir, check=False)
            existing = False

        # 2. Snapshot.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        if fmt == "binary":
            out_path = workdir / "coach.db"
            _online_backup(db_path, out_path)
        else:
            out_path = workdir / "coach.sql"
            _sql_dump(db_path, out_path)

        # 3. Commit + push. If the snapshot is identical to last time, skip.
        _run(["git", "add", out_path.name], cwd=workdir)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(workdir),
        )
        if diff.returncode == 0 and existing:
            logger.info("no changes since last snapshot; skipping commit")
            return 0
        _run(
            [
                "git",
                "-c",
                "user.email=backup@pre.coach",
                "-c",
                "user.name=PRE backup",
                "commit",
                "-m",
                f"snapshot {ts}",
            ],
            cwd=workdir,
        )
        _run(["git", "push", "origin", f"HEAD:{branch}"], cwd=workdir)
        logger.info("snapshot %s pushed to %s on branch %s", ts, repo, branch)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
