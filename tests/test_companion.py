"""Unit tests for companion.py.

Mocks llm_client (no real LLM calls) and uses fake_redis + a tmp state dir.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import companion
from state_manager import StateManager

ATHLETE_YAML = """\
name: Test
target_races:
  - name: Future Race
    date: 2099-01-01
    priority: A
    goal_pace: "6:10"
zones:
  marathon_pace: "6:40"
"""

PLAN_MD = """\
# Plan

## This Week

| Day | Date | Workout | Pace target | Notes |
|-----|------|---------|-------------|-------|
| Sun | 2099-01-01 | Easy 4mi | 8:30-9:00 | |
"""


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def state(state_dir: Path, monkeypatch) -> StateManager:
    """Inject a tmp StateManager into companion's module-global cache, seeded
    with athlete YAML + plan via the StateManager API (athlete row needs to
    exist; plan goes through update_plan)."""
    s = StateManager(state_dir)
    # Seed athlete directly (singleton row not exposed via a public writer)
    with s._conn() as c:
        c.execute("INSERT INTO athlete (id, yaml_text) VALUES (1, ?)", (ATHLETE_YAML,))
    s.update_plan(PLAN_MD, "seed")
    monkeypatch.setattr(companion, "_state", s)
    return s


def _llm_message(content: str | None = None, tool_calls=None) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []

    def model_dump(exclude_none=False):
        d = {"role": "assistant"}
        if content is not None:
            d["content"] = content
        if tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        return d

    msg.model_dump = model_dump
    return msg


def _mock_llm(monkeypatch, *responses) -> MagicMock:
    """Patch companion.llm_client.chat.completions.create to return canned responses."""
    fake_client = MagicMock()
    completions = []
    for msg in responses:
        completion = MagicMock()
        completion.choices = [MagicMock(message=msg)]
        completions.append(completion)
    fake_client.chat.completions.create.side_effect = completions
    monkeypatch.setattr(companion, "llm_client", fake_client)
    return fake_client


# ---------------- build_system_prompt ----------------


class TestBuildSystemPrompt:
    def test_contains_required_sections(self, state):
        p = companion.build_system_prompt(state)
        for marker in [
            "VOICE & FORMAT",
            "TODAY",
            "ATHLETE PROFILE",
            "TRAINING PLAN",
            "HOW TO USE TOOLS",
            "ADAPTATION NORMS",
        ]:
            assert marker in p, f"missing section: {marker}"

    def test_includes_state_blob(self, state):
        p = companion.build_system_prompt(state)
        assert "Future Race" in p  # athlete.yaml content
        assert "Easy 4mi" in p  # plan.md content

    def test_voice_norms_present(self, state):
        p = companion.build_system_prompt(state)
        assert "Declaratives" in p
        assert "Tables ONLY" in p

    def test_pending_proposal_absent_when_empty(self, state, fake_redis):
        p = companion.build_system_prompt(state)
        assert "PENDING PLAN PROPOSAL" not in p

    def test_pending_proposal_surfaced_when_set(self, state, fake_redis):
        from pending_proposal_store import set_pending_plan_proposal

        set_pending_plan_proposal(
            {
                "summary": "Demote Thursday tempo to easy 5",
                "new_plan_md": "# Plan\n\nrevised body\n",
                "reason": "HR overreach on Tuesday workout",
            }
        )
        p = companion.build_system_prompt(state)
        assert "PENDING PLAN PROPOSAL" in p
        assert "Demote Thursday tempo to easy 5" in p
        assert "HR overreach on Tuesday workout" in p
        assert "revised body" in p
        # Tool norms must instruct the agent how to confirm/decline
        assert "update_plan" in p
        assert "yes" in p.lower()


# ---------------- agent_turn ----------------


class TestAgentTurn:
    def test_simple_response_no_tools(self, state, monkeypatch):
        _mock_llm(monkeypatch, _llm_message(content="hello back"))
        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}]
        out = companion.agent_turn(messages, state)
        assert out == "hello back"

    def test_tool_call_dispatched(self, state, monkeypatch):
        # First response: tool call. Second: final text.
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "get_today"
        tc.function.arguments = "{}"

        _mock_llm(
            monkeypatch,
            _llm_message(content=None, tool_calls=[tc]),
            _llm_message(content="today is X"),
        )
        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "what date"}]
        out = companion.agent_turn(messages, state)
        assert out == "today is X"
        # Tool result should have been appended between the two assistant turns
        roles = [m["role"] for m in messages]
        assert "tool" in roles

    def test_empty_content_gets_placeholder(self, state, monkeypatch):
        tc = MagicMock()
        tc.id = "c1"
        tc.function.name = "get_today"
        tc.function.arguments = "{}"
        _mock_llm(
            monkeypatch,
            _llm_message(content=None, tool_calls=[tc]),
            _llm_message(content="done"),
        )
        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "go"}]
        companion.agent_turn(messages, state)
        # First assistant msg (index 2) should have non-empty content
        assistant_msg = messages[2]
        assert assistant_msg["content"]
        assert assistant_msg["content"] != ""

    def test_iteration_cap(self, state, monkeypatch):
        # Always return tool_calls — should stop after MAX_TOOL_LOOPS
        tc = MagicMock()
        tc.id = "c"
        tc.function.name = "get_today"
        tc.function.arguments = "{}"

        msgs = [_llm_message(content=None, tool_calls=[tc]) for _ in range(companion.MAX_TOOL_LOOPS + 2)]
        _mock_llm(monkeypatch, *msgs)

        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "loop"}]
        out = companion.agent_turn(messages, state)
        # Exits cleanly even when capped; last assistant content placeholder returned
        assert isinstance(out, str)


# ---------------- chat ----------------


class TestChat:
    def test_persists_history(self, state, monkeypatch, fake_redis):
        _mock_llm(monkeypatch, _llm_message(content="ok"))
        # Avoid the cache_control probe path complicating mock count
        monkeypatch.setattr(companion, "_cache_control_supported", False)

        out = companion.chat("hello")
        assert out == "ok"
        from conversation_store import get_history

        history = get_history()
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1] == {"role": "assistant", "content": "ok"}

    def test_returns_apology_on_error(self, state, monkeypatch, fake_redis):
        fake = MagicMock()
        fake.chat.completions.create.side_effect = RuntimeError("boom")
        monkeypatch.setattr(companion, "llm_client", fake)
        monkeypatch.setattr(companion, "_cache_control_supported", False)

        out = companion.chat("hi")
        assert "apologize" in out.lower() or "trouble" in out.lower()

    def test_history_not_polluted_on_llm_failure(self, state, monkeypatch, fake_redis):
        """Regression for issue #15: a failed LLM call must not leave the user
        message stranded in Redis history. Otherwise Telegram-driven retries
        of the same update accumulate duplicate user turns each pass."""
        fake = MagicMock()
        fake.chat.completions.create.side_effect = RuntimeError("boom")
        monkeypatch.setattr(companion, "llm_client", fake)
        monkeypatch.setattr(companion, "_cache_control_supported", False)

        companion.chat("hello")

        from conversation_store import get_history

        assert get_history() == []

    def test_history_after_failure_then_success(self, state, monkeypatch, fake_redis):
        """A failed turn followed by a successful turn should yield exactly
        the success pair in history — no ghost user message from the failed
        attempt."""
        from conversation_store import get_history

        fake = MagicMock()
        fake.chat.completions.create.side_effect = RuntimeError("boom")
        monkeypatch.setattr(companion, "llm_client", fake)
        monkeypatch.setattr(companion, "_cache_control_supported", False)
        companion.chat("first attempt fails")
        assert get_history() == []

        _mock_llm(monkeypatch, _llm_message(content="here"))
        companion.chat("second attempt works")
        history = get_history()
        assert len(history) == 2
        assert history[0] == {"role": "user", "content": "second attempt works"}
        assert history[1] == {"role": "assistant", "content": "here"}
