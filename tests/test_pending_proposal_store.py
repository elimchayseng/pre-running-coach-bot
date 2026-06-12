from pending_proposal_store import (
    clear_pending_plan_proposal,
    get_pending_plan_proposal,
    set_pending_plan_proposal,
)


class TestPendingProposalStore:
    def test_empty(self, fake_redis):
        assert get_pending_plan_proposal() is None

    def test_set_and_get(self, fake_redis):
        payload = {"summary": "shift threshold", "new_plan_md": "...", "reason": "overreach"}
        set_pending_plan_proposal(payload)
        out = get_pending_plan_proposal()
        assert out == payload

    def test_overwrites(self, fake_redis):
        set_pending_plan_proposal({"summary": "first"})
        set_pending_plan_proposal({"summary": "second"})
        assert get_pending_plan_proposal() == {"summary": "second"}

    def test_clear(self, fake_redis):
        set_pending_plan_proposal({"summary": "x"})
        clear_pending_plan_proposal()
        assert get_pending_plan_proposal() is None

    def test_clear_when_empty_is_noop(self, fake_redis):
        clear_pending_plan_proposal()
        assert get_pending_plan_proposal() is None

    def test_set_reraises_non_connection_errors(self, fake_redis, monkeypatch):
        """Swallowing a failed write would report success for a proposal that
        was never stored — the user gets "Reply 'yes' to apply" for nothing.
        Callers catch this and strip the proposal from their message."""
        import pytest

        import pending_proposal_store as store

        class _Broken:
            def setex(self, *a, **k):
                raise RuntimeError("READONLY: read-only replica")

        monkeypatch.setattr(store, "_get_redis", lambda: _Broken())
        with pytest.raises(RuntimeError):
            set_pending_plan_proposal({"summary": "x"})
