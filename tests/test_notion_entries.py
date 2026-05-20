"""Tests for notion/entries.py — singleton-blob parsers."""

from __future__ import annotations

from notion.entries import parse_changelog_entries, parse_journal_entries


class TestParseJournalEntries:
    def test_empty(self):
        assert parse_journal_entries("") == []
        assert parse_journal_entries(None) == []

    def test_skips_preamble(self):
        text = "# Journal\n\nintro prose\n\n---\n\n## 2026-04-26 — Post\n\nbody\n"
        entries = parse_journal_entries(text)
        assert len(entries) == 1
        assert entries[0]["title"] == "2026-04-26 — Post"
        assert entries[0]["date"] == "2026-04-26"
        assert entries[0]["body"] == "body"

    def test_multiple_entries_in_order(self):
        text = "# J\n\n---\n\n## 2026-04-26 — A\n\nbody A\n\n---\n\n## 2026-05-01 — B\n\nbody B\n"
        entries = parse_journal_entries(text)
        assert [e["title"] for e in entries] == ["2026-04-26 — A", "2026-05-01 — B"]

    def test_date_none_when_header_has_no_date(self):
        text = "# J\n\n---\n\n## Untitled note\n\nbody\n"
        assert parse_journal_entries(text)[0]["date"] is None

    def test_block_without_h2_header_skipped(self):
        text = "# J\n\n---\n\nbare text, no header\n"
        assert parse_journal_entries(text) == []


class TestParseChangelogEntries:
    def test_empty(self):
        assert parse_changelog_entries("") == []
        assert parse_changelog_entries(None) == []

    def test_planned_edit_default(self):
        text = "- 2026-05-19T12:00:00: Added yoga\n"
        [e] = parse_changelog_entries(text)
        assert e == {"timestamp": "2026-05-19T12:00:00", "note": "Added yoga", "action": "planned-edit"}

    def test_completed_classified(self):
        text = "- 2026-05-19T12:00:00: 2026-05-19 completed: easy\n"
        [e] = parse_changelog_entries(text)
        assert e["action"] == "completed"

    def test_multiline_blob(self):
        text = "- 2026-05-19T12:00:00: a\n- 2026-05-19T12:01:00: b\nnot a line\n- 2026-05-19T12:02:00: c\n"
        entries = parse_changelog_entries(text)
        assert [e["note"] for e in entries] == ["a", "b", "c"]

    def test_malformed_line_skipped(self):
        text = "- no colon space here\n- :empty timestamp\n- 2026-05-19T12:00:00: ok\n"
        entries = parse_changelog_entries(text)
        assert len(entries) == 1
        assert entries[0]["note"] == "ok"
