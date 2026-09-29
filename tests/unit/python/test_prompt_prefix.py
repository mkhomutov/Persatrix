"""The cached prompt prefix: text every persona turn carries first (EXP-001 harness PR 5d).

Arm D′ has no memory. Wherever arm D's prompts receive recalled memory,
D′'s receive the full transcripts of the series' earlier meetings, placed
before anything that changes between calls and marked for the provider's
cache (pre-registration §2). Recalled memory reaches one prompt in the
runtime: the persona's turn, every call of its tool loop. The salience bid,
the reflexion critic and revise passes, the close summary and working-memory
compression each build a prompt of their own that memory never reaches.

So when ``PERSATRIX_PROMPT_PREFIX`` names a file, every turn call hands its
text to the model client as the cache prefix, and no other call does. A
setting that names a missing or empty file stops the agent at startup.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents import prompt_prefix as prefix_setting
from agents.llm_client import LLMResponse, StopReason, ToolCall, Usage
from agents.llm_types import LLMCallPurpose
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

_AGENTS = Path(__file__).resolve().parents[3] / "agents"
_TEXT = "Transcripts of your earlier meetings with the same members, oldest first.\n"


@pytest.fixture(autouse=True)
def _fresh_setting() -> Iterator[None]:
    prefix_setting.reset_prompt_prefix()
    yield
    prefix_setting.reset_prompt_prefix()


def _set(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv(prefix_setting.PROMPT_PREFIX_ENV, str(path))


class TestTheSetting:
    def test_without_it_there_is_no_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(prefix_setting.PROMPT_PREFIX_ENV, raising=False)
        assert prefix_setting.prompt_prefix() == ""

    def test_the_prefix_is_the_file_word_for_word_read_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "prefix.txt"
        path.write_bytes(b"Line one.\r\nLine two, with a lone \r in it.\n")
        _set(monkeypatch, path)
        assert prefix_setting.prompt_prefix() == "Line one.\r\nLine two, with a lone \r in it.\n"
        path.write_bytes(b"Something else.")
        assert prefix_setting.prompt_prefix().startswith("Line one.")

    @pytest.mark.parametrize("content", [None, b"", b"  \n\t\n"])
    def test_a_missing_or_empty_file_is_refused(
        self, content: bytes | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "prefix.txt"
        if content is not None:
            path.write_bytes(content)
        _set(monkeypatch, path)
        with pytest.raises(ValueError, match=prefix_setting.PROMPT_PREFIX_ENV):
            prefix_setting.prompt_prefix()

    def test_a_bad_prefix_stops_the_agent_at_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents import server_cli
        from agents.model_aliases import use_alias_map

        priced = {"quality": {"provider": "mock", "model": "mock", "input_per_1m_tokens": 0.0,
                              "output_per_1m_tokens": 0.0}}
        _set(monkeypatch, tmp_path / "missing.txt")
        with use_alias_map(priced), pytest.raises(
            SystemExit, match=prefix_setting.PROMPT_PREFIX_ENV,
        ):
            server_cli._validate_startup_config()


async def _turn(responses: list[LLMResponse]) -> AsyncMock:
    """Run one persona turn on a channel message; the client's calls, as awaited."""
    client = _make_client(responses)
    spy = AsyncMock(wraps=client.create_message)
    client.create_message = spy  # type: ignore[method-assign]
    agent = create_persona_agent(
        agent_id=_PERSONA_CONFIG["id"], config={**_PERSONA_CONFIG}, llm_client=client,
    )
    await agent.initialize_memory()
    try:
        await agent.on_event(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "What do you make of the plan?"},
            sender_id="grey-heron",
        ))
    finally:
        await agent.close_memory()
    return spy


_TOOL_ROUND = [
    LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="tc1", name="recall_notes", input={"query": "plan"})],
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(100, 50),
    ),
    LLMResponse(text="It leaves the lease out.", stop_reason=StopReason.END_TURN,
                usage=Usage(200, 100)),
]


class TestTheTurnCarriesIt:
    async def test_every_call_of_a_turn_carries_the_prefix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "prefix.txt"
        path.write_bytes(_TEXT.encode())
        _set(monkeypatch, path)
        spy = await _turn(list(_TOOL_ROUND))
        calls = [c.kwargs for c in spy.await_args_list]
        assert [c["purpose"] for c in calls] == [LLMCallPurpose.TURN, LLMCallPurpose.TURN]
        assert [c["cache_prefix"] for c in calls] == [_TEXT, _TEXT]

    async def test_without_the_setting_a_turn_carries_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(prefix_setting.PROMPT_PREFIX_ENV, raising=False)
        spy = await _turn(list(_TOOL_ROUND))
        assert [c.kwargs.get("cache_prefix", "") for c in spy.await_args_list] == ["", ""]


def test_only_the_persona_turn_hands_the_client_a_prefix() -> None:
    """Every other call site builds a prompt recalled memory never reaches,
    so none may carry the prefix: a bid that did would write a cache entry
    of its own, with no tools, and check 3 would fail."""
    found: list[tuple[str, str]] = []
    for path in sorted(_AGENTS.rglob("*.py")):
        if "tests" in path.parts or "generated" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_message"
            ):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords}
            if "cache_prefix" in keywords:
                purpose = keywords.get("purpose")
                found.append((
                    path.relative_to(_AGENTS).as_posix(),
                    ast.unparse(purpose) if purpose is not None else "",
                ))
    assert found == [("persona_runtime/action_loop.py", "LLMCallPurpose.TURN")]
