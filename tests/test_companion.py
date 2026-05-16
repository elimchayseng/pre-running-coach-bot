"""Unit tests for companion.py.

Mocks llm_client (no real LLM calls) and uses fake_redis + a tmp state dir.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date
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

# Row dated to today so it lands inside the system prompt's "this week"
# table (render_plan only renders the current week).
_TODAY = date.today()
PLAN_MD = (
    "# Plan\n\n## This Week\n\n"
    "| Day | Date | Workout | Pace target | Notes |\n"
    "|-----|------|---------|-------------|-------|\n"
    f"| {_TODAY.strftime('%a')} | {_TODAY.isoformat()} | Easy 4mi | 8:30-9:00 | |\n"
)


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

    def test_chat_serializes_concurrent_threads(self, state, monkeypatch, fake_redis):
        """Regression for the threading hazard introduced by PR #18.

        With background-thread webhook dispatch, two updates can land in
        companion.chat concurrently. Without _chat_lock, both would read
        the same session:history snapshot and write back their own version
        — losing one of the user/assistant pairs. The lock must serialize
        the whole chat() body so the Redis read-modify-write is atomic
        relative to other turns.

        We force the race window by holding both LLM calls inside the
        client mock long enough that, without the lock, they would
        overlap. The assertions verify (a) only one chat() body is inside
        the critical section at a time, and (b) the two complete pairs
        end up in history in order."""
        from conversation_store import get_history

        active = {"count": 0, "peak": 0}
        active_lock = threading.Lock()
        # Hold each LLM call for long enough that without serialization
        # the threads would observably overlap in the mock.
        hold_seconds = 0.1

        def gated_create(*_args, **_kwargs):
            with active_lock:
                active["count"] += 1
                active["peak"] = max(active["peak"], active["count"])
            try:
                time.sleep(hold_seconds)
            finally:
                with active_lock:
                    active["count"] -= 1
            completion = MagicMock()
            completion.choices = [MagicMock(message=_llm_message(content="ok"))]
            return completion

        fake = MagicMock()
        fake.chat.completions.create.side_effect = gated_create
        monkeypatch.setattr(companion, "llm_client", fake)
        monkeypatch.setattr(companion, "_cache_control_supported", False)

        results: list[str] = []
        results_lock = threading.Lock()

        def run(msg):
            out = companion.chat(msg)
            with results_lock:
                results.append(out)

        t1 = threading.Thread(target=run, args=("first message",))
        t2 = threading.Thread(target=run, args=("second message",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive() and not t2.is_alive(), "chat() threads hung"

        # The load-bearing assertion: at no point were both chat() bodies
        # inside the critical section. Without _chat_lock the peak would
        # be 2 (both threads inside the mocked create() simultaneously).
        assert active["peak"] == 1, f"_chat_lock failed to serialize: peak concurrent LLM calls={active['peak']}"

        # Both turns succeeded and history contains both pairs in order.
        assert results == ["ok", "ok"]
        history = get_history()
        assert len(history) == 4
        assert [h["role"] for h in history] == ["user", "assistant", "user", "assistant"]
        # The user messages preserve their content; which order depends on
        # which thread won the lock first, but they must not be interleaved.
        user_messages = [history[0]["content"], history[2]["content"]]
        assert set(user_messages) == {"first message", "second message"}


# ---------------- _is_cache_control_rejection ----------------


class TestCacheControlDetection:
    """The previous form (`"content" in str(e).lower()`) over-matched any
    BadRequestError mentioning 'content' — including ordinary message-shape
    validation errors — and would silently disable caching permanently.
    These tests pin down the new behavior: match only on the literal
    cache_control token, in either body or response text."""

    def _exc(self, body=None, response_text=None):
        exc = MagicMock()
        exc.body = body
        if response_text is None:
            exc.response = None
        else:
            resp = MagicMock()
            resp.text = response_text
            exc.response = resp
        return exc

    def test_body_with_cache_control_in_message(self):
        exc = self._exc(body={"error": {"message": "cache_control is not supported"}})
        assert companion._is_cache_control_rejection(exc) is True

    def test_body_with_content_only_does_not_match(self):
        """The bug case: a generic content-validation error must NOT trigger
        the fallback. Previously this returned True."""
        exc = self._exc(body={"error": {"message": "messages.0.content must be a string"}})
        assert companion._is_cache_control_rejection(exc) is False

    def test_response_text_fallback(self):
        """When body is missing or malformed, fall back to the raw response
        text. Some providers return non-JSON error bodies."""
        exc = self._exc(body=None, response_text="Error: cache_control field rejected")
        assert companion._is_cache_control_rejection(exc) is True

    def test_no_body_no_response_returns_false(self):
        exc = self._exc(body=None, response_text=None)
        assert companion._is_cache_control_rejection(exc) is False

    def test_body_missing_error_key(self):
        exc = self._exc(body={"detail": "something else"})
        assert companion._is_cache_control_rejection(exc) is False


# ---------------- agent_turn timing logs ----------------


class TestAgentTurnTiming:
    """PR A adds per-iteration timing so production logs can answer issue
    #17's open question (one slow LLM call vs many). These tests verify
    the log lines are emitted with the expected shape."""

    def test_logs_iter_and_tool_lines(self, state, monkeypatch, caplog):
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

        with caplog.at_level(logging.INFO, logger="pre_coach"):
            companion.agent_turn(messages, state)

        all_msgs = [r.getMessage() for r in caplog.records]
        iter_lines = [m for m in all_msgs if "agent_turn iter=" in m and "llm_ms=" in m]
        tool_lines = [m for m in all_msgs if "agent_turn iter=" in m and "tool=" in m]
        # Two iterations: one for the tool call, one for the final text.
        assert len(iter_lines) == 2, f"expected 2 iter log lines, got {len(iter_lines)}: {all_msgs}"
        # One tool call across the loop.
        assert len(tool_lines) == 1, f"expected 1 tool log line, got {len(tool_lines)}: {all_msgs}"
        assert "tool=get_today" in tool_lines[0]
        # Both iter lines mention tool_calls= so we can tell whether the
        # iteration was a tool dispatch or the final text.
        assert all("tool_calls=" in m for m in iter_lines)
