"""Tests for scripts/notion_bootstrap.py schema reconciliation (issue #50).

The bootstrap is idempotent: re-running against a workspace whose databases
already exist must still PATCH any property added to the code schema since the
database was created (e.g. Reviews' "Kind"). Without that, the mirror upsert
400s on the missing property and is swallowed per-row in the daemon threads,
so the mirror dies silently. These tests drive _patch_missing_properties with a
fake NotionClient and also assert the two new client methods build the right
requests (issue #56 part 2).
"""

from __future__ import annotations

from scripts.notion_bootstrap import _patch_missing_properties


class _FakeClient:
    """Records retrieve/update calls and serves a fixed live schema."""

    def __init__(self, live_properties: dict):
        self._live = live_properties
        self.retrieved: list[str] = []
        self.updates: list[tuple[str, dict]] = []

    def retrieve_data_source(self, ds_id: str) -> dict:
        self.retrieved.append(ds_id)
        return {"properties": self._live}

    def update_data_source(self, ds_id: str, properties: dict) -> dict:
        self.updates.append((ds_id, properties))
        return {"id": ds_id}


class TestPatchMissingProperties:
    def test_adds_only_missing_properties(self, capsys):
        # Live DB has Title + Date; code schema adds Kind.
        client = _FakeClient({"Title": {"title": {}}, "Date": {"date": {}}})
        code_schema = {"Title": {"title": {}}, "Date": {"date": {}}, "Kind": {"select": {}}}

        _patch_missing_properties(client, "ds-1", "PRE Reviews", code_schema)

        assert client.retrieved == ["ds-1"]
        assert len(client.updates) == 1
        ds_id, patched = client.updates[0]
        assert ds_id == "ds-1"
        assert patched == {"Kind": {"select": {}}}  # ONLY the missing one
        assert "patch  PRE Reviews: added properties ['Kind']" in capsys.readouterr().out

    def test_no_delta_is_noop(self, capsys):
        live = {"Title": {"title": {}}, "Date": {"date": {}}}
        client = _FakeClient(dict(live))

        _patch_missing_properties(client, "ds-2", "PRE Sessions", dict(live))

        assert client.updates == []  # nothing patched
        assert "patch" not in capsys.readouterr().out

    def test_patch_failure_is_swallowed_with_warning(self, capsys):
        class _BrokenClient:
            def retrieve_data_source(self, ds_id):
                raise RuntimeError("network down")

        _patch_missing_properties(_BrokenClient(), "ds-3", "PRE Journal", {"X": {"rich_text": {}}})
        # The bootstrap must stay usable even if one DB can't be reconciled.
        assert "warn   PRE Journal: could not patch properties" in capsys.readouterr().out


class TestNotionClientDataSourceMethods:
    """The bootstrap now goes through named public methods instead of the
    private _request (issue #56 part 2). Verify they target the right verb+path."""

    def _client(self, monkeypatch):
        from notion.client import NotionClient

        c = NotionClient(token="t", version="2026-03-11")
        calls: list[tuple] = []

        def _fake_request(method, path, payload=None):
            calls.append((method, path, payload))
            return {"properties": {}}

        monkeypatch.setattr(c, "_request", _fake_request)
        return c, calls

    def test_retrieve_data_source(self, monkeypatch):
        c, calls = self._client(monkeypatch)
        c.retrieve_data_source("ds-9")
        assert calls == [("GET", "/data_sources/ds-9", None)]

    def test_update_data_source_wraps_properties(self, monkeypatch):
        c, calls = self._client(monkeypatch)
        c.update_data_source("ds-9", {"Kind": {"select": {}}})
        assert calls == [("PATCH", "/data_sources/ds-9", {"properties": {"Kind": {"select": {}}}})]
