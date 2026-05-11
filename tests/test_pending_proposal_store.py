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
