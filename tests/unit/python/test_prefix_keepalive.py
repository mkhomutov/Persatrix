"""Keeping arm D′'s cached prefix alive through a quiet spell (EXP-001 harness PR 5e).

Arm D′'s advisers share one cache entry: every turn offers the same tools and
carries the same prefix (:mod:`agents.prompt_prefix`). The provider keeps an
entry five minutes after the last call that wrote or read it began, and a
governed discussion goes quiet by design: one that closes by its 600-second
idle window makes no call for ten minutes. In the practice run the memo turn
after each such close wrote the prefix again, and check 3 named it.

``PERSATRIX_PROMPT_PREFIX_KEEPALIVE`` names how long the room may go without
a call that carries the prefix. Past that, the process sends one call that
offers the turn's tools, carries the prefix and asks for no output: it reads
the entry, which keeps it alive, and writes nothing. The room is every
process that writes to the same call log, so the process reads from the log
when the prefix was last used.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agents import call_log, prefix_keepalive
from agents import prompt_prefix as prefix_setting
from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.llm_types import LLMCallPurpose
from agents.observability import metrics
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.server import AgentServer

from ._persona_test_helpers import _PERSONA_CONFIG

_PREFIX = "Transcripts of your earlier meetings with the same members, oldest first.\n"
_QUIET = 240.0
# What a keep-alive gets back: nothing said, the prefix read, nothing written.
_KEPT = LLMResponse(
    text=None, stop_reason=StopReason.MAX_TOKENS,
    usage=Usage(329, 0, cache_write_tokens=0, cache_read_tokens=3862),
)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    prefix_setting.reset_prompt_prefix()
    call_log.reset_call_log()
    yield
    prefix_setting.reset_prompt_prefix()
    call_log.reset_call_log()


@pytest.fixture
def room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A process set up as arm D′'s chair is: the prefix, the shared call log
    and the keep-alive. Returns the log."""
    prefix = tmp_path / "prefix.txt"
    prefix.write_text(_PREFIX)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv(prefix_setting.PROMPT_PREFIX_ENV, str(prefix))
    monkeypatch.setenv(call_log.CALL_LOG_ENV, str(log))
    monkeypatch.setenv(call_log.CALL_TAGS_ENV, json.dumps({"arm": "D-prime"}))
    monkeypatch.setenv(prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV, "240")
    monkeypatch.setattr(metrics, "_AGENT_ID", "lunar-stoat")
    return log


def _used(
    log: Path, seconds_ago: float, *, prefix: str = _PREFIX, error: str | None = None,
) -> None:
    """A line in the shared log: another adviser's call that carried *prefix*
    and began *seconds_ago*."""
    started = dt.datetime.fromtimestamp(time.time() - seconds_ago, dt.UTC).isoformat()
    line = {
        "tags": {"arm": "D-prime"}, "agent_id": "velvet-pika", "purpose": "turn",
        "provider": "anthropic", "model": "claude-sonnet-4-6", "model_alias": "quality",
        "started_at": started, "input_tokens": 2400, "output_tokens": 60,
        "cache_write_tokens": 0, "cache_read_tokens": 3862,
        "cache_prefix_sha256": call_log.prefix_sha256(prefix), "error": error,
    }
    with log.open("a") as out:
        out.write(json.dumps(line) + "\n")


def _lines(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


class _Provider:
    """Anthropic as the adapter shows it: it caches, and it records each request."""

    name = "anthropic"
    supports_prompt_cache = True

    def __init__(self, *answers: LLMResponse | BaseException | float) -> None:
        # Each answer is a response, an error to raise, or seconds to hang for.
        self._answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        self.requests.append(kwargs)
        answer = self._answers.pop(0) if self._answers else _KEPT
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, float):
            await asyncio.sleep(answer)
            return _KEPT
        return answer

    def format_tool_definitions(self, tools: list[dict]) -> list[dict]:
        return [
            {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
            for t in tools
        ]

    def append_tool_round(self, messages: list, response: Any, results: list) -> list:
        return [*messages, {"role": "assistant", "content": "tool round"}]


@pytest.fixture
async def adviser() -> AsyncIterator[Callable[[_Provider], Awaitable[Any]]]:
    """Makes persona advisers on a provider, and closes their memory after."""
    made: list[Any] = []

    async def make(provider: _Provider) -> Any:
        agent = create_persona_agent(
            agent_id=_PERSONA_CONFIG["id"], config={**_PERSONA_CONFIG},
            llm_client=LLMClient(provider),
        )
        await agent.initialize_memory()
        made.append(agent)
        return agent

    yield make
    for agent in made:
        await agent.close_memory()


class _StoppedError(Exception):
    """Ends a loop the test let look enough times."""


class _Looks:
    """Stands in for the loop's sleep: lets it look *times* times, then stops it."""

    def __init__(self, times: int) -> None:
        self._left = times
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self._left == 0:
            raise _StoppedError
        self._left -= 1


async def _keep(agent: Any, log: Path, *, looks: int, **options: Any) -> _Looks:
    sleep = _Looks(looks)
    with pytest.raises(_StoppedError):
        await prefix_keepalive.keep_prefix_alive(
            agent, quiet=_QUIET, prefix=_PREFIX, log=str(log), sleep=sleep, **options,
        )
    return sleep


class TestTheSetting:
    def test_without_it_there_is_no_keepalive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV, raising=False)
        assert prefix_setting.prefix_keepalive_seconds() is None

    def test_it_names_the_seconds_of_quiet(self, room: Path) -> None:
        assert prefix_setting.prefix_keepalive_seconds() == _QUIET

    @pytest.mark.parametrize("raw", ["soon", "0", "-30", "300", "301", "nan", "inf"])
    def test_seconds_that_do_not_fit_inside_an_entrys_life_are_refused(
        self, raw: str, room: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV, raw)
        with pytest.raises(ValueError, match=prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV):
            prefix_setting.prefix_keepalive_seconds()

    @pytest.mark.parametrize(
        "missing", [prefix_setting.PROMPT_PREFIX_ENV, call_log.CALL_LOG_ENV],
    )
    def test_it_needs_a_prefix_to_keep_and_a_log_to_read_the_room_from(
        self, missing: str, room: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(missing)
        with pytest.raises(ValueError, match=f"{missing} names no"):
            prefix_setting.prefix_keepalive_seconds()

    def test_a_bad_setting_stops_the_agent_at_startup(
        self, room: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agents import server_cli
        from agents.model_aliases import use_alias_map

        priced = {"quality": {"provider": "mock", "model": "mock", "input_per_1m_tokens": 0.0,
                              "output_per_1m_tokens": 0.0}}
        monkeypatch.setenv(prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV, "600")
        with use_alias_map(priced), pytest.raises(
            SystemExit, match=prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV,
        ):
            server_cli._validate_startup_config()


class TestWhenItSends:
    async def test_nothing_before_a_call_has_carried_the_prefix(
        self, room: Path, adviser: Any,
    ) -> None:
        """The first turn writes the entry; a keep-alive must not."""
        _used(room, 250, prefix="Another meeting's transcripts.\n")
        _used(room, 250, error="RateLimitError")
        provider = _Provider()
        await _keep(await adviser(provider), room, looks=3)
        assert provider.requests == []

    async def test_nothing_while_the_room_is_using_the_prefix(
        self, room: Path, adviser: Any,
    ) -> None:
        _used(room, _QUIET - 30)
        provider = _Provider()
        sleep = await _keep(await adviser(provider), room, looks=2)
        assert provider.requests == []
        assert sleep.waits == [prefix_keepalive.POLL_SECONDS] * 3

    async def test_after_the_quiet_one_call_keeps_the_entry_alive(
        self, room: Path, adviser: Any,
    ) -> None:
        _used(room, _QUIET + 10)
        provider = _Provider()
        await _keep(await adviser(provider), room, looks=3)
        [request] = provider.requests
        assert request["cache_prefix"] == _PREFIX
        assert request["max_tokens"] == 0
        assert request["system"] == ""
        assert request["messages"] == [{"role": "user", "content": prefix_keepalive.PLACEHOLDER}]
        kept = _lines(room)[-1]
        assert kept["purpose"] == LLMCallPurpose.KEEPALIVE.value
        assert kept["agent_id"] == "lunar-stoat"
        assert kept["cache_prefix_sha256"] == call_log.prefix_sha256(_PREFIX)
        assert (kept["error"], kept["cache_read_tokens"], kept["output_tokens"]) == (None, 3862, 0)

    async def test_nothing_once_the_entry_has_gone(self, room: Path, adviser: Any) -> None:
        """The next turn writes the prefix again in any case; a keep-alive
        would write it too, and pay for it twice."""
        _used(room, 301)
        provider = _Provider()
        await _keep(await adviser(provider), room, looks=2)
        assert provider.requests == []

    async def test_one_that_fails_is_tried_again_at_the_next_look(
        self, room: Path, adviser: Any,
    ) -> None:
        _used(room, _QUIET + 10)
        provider = _Provider(ConnectionResetError("reset by peer"), _KEPT)
        await _keep(await adviser(provider), room, looks=3)
        assert len(provider.requests) == 2
        assert [line["error"] for line in _lines(room)[1:]] == ["ConnectionResetError", None]

    async def test_one_that_does_not_answer_is_cut_off_and_tried_again(
        self, room: Path, adviser: Any,
    ) -> None:
        _used(room, _QUIET + 10)
        provider = _Provider(60.0, _KEPT)
        await _keep(await adviser(provider), room, looks=3, timeout=0.01)
        assert len(provider.requests) == 2
        assert [line["error"] for line in _lines(room)[1:]] == ["CancelledError", None]


async def test_it_offers_what_a_turn_offers_and_carries_its_prefix(
    room: Path, adviser: Any,
) -> None:
    """The provider keys an entry on the tools and then the prefix, so the
    keep-alive reads the turn's entry only if both are the turn's, sent to
    the same model. The rest of the turn's prompt comes after the prefix and
    is no part of the entry."""
    provider = _Provider(LLMResponse(text="It leaves the lease out.", usage=Usage(200, 100)))
    agent = await adviser(provider)
    await agent.on_event(AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "What do you make of the plan?"},
        sender_id="grey-heron",
    ))
    # Seen from a moment past the quiet, the turn is the last use.
    await _keep(agent, room, looks=1, clock=lambda: time.time() + _QUIET + 10)
    turn, kept = provider.requests
    assert turn["tools"], "a turn offers the note tools"
    shared = ("model", "tools", "temperature", "cache_prefix")
    assert {k: kept[k] for k in shared} == {k: turn[k] for k in shared}


class _StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="stub")


class TestTheServer:
    async def test_a_process_keeps_its_prefix_alive_only_when_asked(
        self, room: Path, adviser: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = await adviser(_Provider())
        task = prefix_keepalive.start_prefix_keepalive({agent.agent_id: agent})
        assert task is not None
        task.cancel()
        monkeypatch.delenv(prefix_setting.PROMPT_PREFIX_KEEPALIVE_ENV)
        prefix_setting.reset_prompt_prefix()
        assert prefix_keepalive.start_prefix_keepalive({agent.agent_id: agent}) is None

    async def test_one_without_a_persona_keeps_nothing_alive(self, room: Path) -> None:
        stub = _StubAgent(agent_id="test-agent", config={})
        assert prefix_keepalive.start_prefix_keepalive({"test-agent": stub}) is None

    async def test_the_server_starts_it_and_its_stop_cancels_it(self) -> None:
        server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1)
        server.register_agent(_StubAgent(agent_id="test-agent", config={}))
        with (
            patch.object(server, "_self_register", new_callable=AsyncMock),
            patch.object(server, "_start_prefix_keepalive") as start,
        ):
            await server.start()
        start.assert_called_once_with()
        kept = asyncio.ensure_future(asyncio.sleep(3600))
        server._prefix_keepalive = kept
        with patch.object(server, "_self_deregister", new_callable=AsyncMock):
            await server.stop()
        assert kept.cancelled()
        assert server._prefix_keepalive is None
