"""Offline smoke tests for scripts/backup_db.py.

These exercise the pieces that don't require network — the SQLite online-backup
and SQL-dump helpers, the token redactor, and the env-var validation in main().
The clone/commit/push path is intentionally not covered here.
"""

import sqlite3

import pytest

from scripts.backup_db import _online_backup, _redact, _sql_dump, main


def _seed_db(path):
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        conn.executemany("INSERT INTO t (label) VALUES (?)", [("alpha",), ("beta",), ("gamma",)])
        conn.commit()


def test_online_backup_round_trip(tmp_path):
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    _seed_db(src)

    _online_backup(src, dst)

    with sqlite3.connect(str(dst)) as conn:
        rows = conn.execute("SELECT label FROM t ORDER BY id").fetchall()
    assert rows == [("alpha",), ("beta",), ("gamma",)]


def test_sql_dump_produces_runnable_text(tmp_path):
    src = tmp_path / "src.db"
    dump = tmp_path / "dump.sql"
    rebuilt = tmp_path / "rebuilt.db"
    _seed_db(src)

    _sql_dump(src, dump)

    text = dump.read_text(encoding="utf-8")
    assert "CREATE TABLE" in text
    assert "alpha" in text and "beta" in text and "gamma" in text

    with sqlite3.connect(str(rebuilt)) as conn:
        conn.executescript(text)
        rows = conn.execute("SELECT label FROM t ORDER BY id").fetchall()
    assert rows == [("alpha",), ("beta",), ("gamma",)]


def test_redact_strips_github_pat_from_clone_urls():
    raw = "fatal: unable to access 'https://x-access-token:ghp_sekrit123@github.com/owner/repo.git/'"
    redacted = _redact(raw)
    assert "ghp_sekrit123" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_is_noop_when_no_token_present():
    msg = "fatal: refusing to merge unrelated histories"
    assert _redact(msg) == msg


def test_main_exits_2_when_database_path_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("GITHUB_BACKUP_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_main_exits_2_when_db_file_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "does-not-exist.db"))
    monkeypatch.setenv("GITHUB_BACKUP_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    assert main() == 2


def test_main_exits_2_on_invalid_backup_format(monkeypatch, tmp_path):
    db = tmp_path / "coach.db"
    _seed_db(db)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("GITHUB_BACKUP_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("BACKUP_FORMAT", "parquet")
    assert main() == 2
