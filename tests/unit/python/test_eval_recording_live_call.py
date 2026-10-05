"""What the eval recorder sends the live provider (ISSUE-0169).

``RecordingProvider`` keys its cassette on the six request inputs replay will
hash, and passes the call on to the live provider. Two things belong to the
live call alone, and must reach it without changing that key:

* **The calling alias's request settings**, which ``LLMClient`` hands over
  per call to a provider that takes them. Without them the live provider
  falls back to the settings it was built with, the seat alias's, on every
  call: a ``fast`` bid would go out with ``quality``'s effort, which Claude
  Haiku rejects.
* **A Claude turn's thinking blocks**, which the next request of a tool
  round must send back unchanged. The tool round the recorder builds stays
  in the shared canonical shape (text, then tool calls), so the recorded key
  is the one replay computes; only the live call carries the turn as the
  provider returned it.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from agents.llm_client import LLMClient
from agents.llm_factory import create_provider
from agents.llm_types import LLMResponse, LLMToolResult, StopReason, ToolCall
from agents.model_aliases import use_alias_map
from evaluators.replay_llm_client import RecordingProvider, ReplayProvider, hash_request

_USER = {"role": "user", "content": "look it up"}
_RESULTS = [LLMToolResult(tool_call_id="toolu_1", content="found", is_error=False)]
# What the provider returned for a turn that thought before its tool call.
_THOUGHT_TURN: list[dict[str, Any]] = [
    {"type": "thinking", "thinking": "I should look.", "signature": "sig-1"},
    {"type": "text", "text": "Looking."},
    {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
]

_ALIASES: dict[str, dict[str, Any]] = {
    "quality": {
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "input_per_1m_tokens": 2.0,
        "output_per_1m_tokens": 10.0,
        "provider_config": {"effort": "low", "prompt_cache": True},
    },
    "fast": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "input_per_1m_tokens": 1.0,
        "output_per_1m_tokens": 5.0,
    },
}


def _request(messages: list[Any] | None = None) -> dict[str, Any]:
    return {
        "model": "claude-sonnet-5-5",
        "messages": [_USER] if messages is None else messages,
        "system": "You look things up.",
        "tools": [],
        "max_tokens": 256,
        "temperature": 0.0,
    }


class _Live:
    """A live provider's stand-in: records each call, and answers the first
    with *first* and every later one with plain text."""

    name = "anthropic"

    def __init__(self, first: LLMResponse | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._first = first

    async def create_message(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if self._first is not None and len(self.calls) == 1:
            return self._first
        return LLMResponse(text="done")

    def format_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(tools)


class _SettingsLive(_Live):
    """A live provider that takes per-call settings, as the vendor ones do."""

    accepts_provider_config = True


def _tool_turn(provider_content: list[dict[str, Any]] | None) -> LLMResponse:
    return LLMResponse(
        text="Looking.",
        tool_calls=[ToolCall(id="toolu_1", name="lookup", input={"q": "x"})],
        stop_reason=StopReason.TOOL_USE,
        provider_content=provider_content,
    )


class TestRequestSettingsReachTheLiveProvider:
    async def test_the_calls_settings_are_passed_on(self) -> None:
        live = _SettingsLive()
        await RecordingProvider(live).create_message(
            **_request(), provider_config={"effort": "low"},
        )
        assert live.calls[0]["provider_config"] == {"effort": "low"}

    async def test_a_call_without_settings_passes_none_on(self) -> None:
        live = _SettingsLive()
        await RecordingProvider(live).create_message(**_request())
        assert "provider_config" not in live.calls[0]

    def test_the_recorder_takes_settings_only_when_the_live_provider_does(self) -> None:
        assert RecordingProvider(_SettingsLive()).accepts_provider_config is True
        assert RecordingProvider(_Live()).accepts_provider_config is False

    async def test_settings_are_not_part_of_the_cassette_key(self) -> None:
        recorder = RecordingProvider(_SettingsLive())
        await recorder.create_message(**_request(), provider_config={"effort": "low"})
        assert list(recorder.cassette) == [hash_request(**_request())]

    async def test_a_lane_call_goes_out_with_its_own_alias_settings(self) -> None:
        # The recipe's provider is built from the `quality` seat alias. A
        # `fast` bid through the recorder must not carry quality's effort or
        # cache markers: Claude Haiku rejects effort.
        sdk = MagicMock()
        client: Any = AsyncMock()
        client.messages.create = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="0.2")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
        )
        sdk.AsyncAnthropic.return_value = client
        with use_alias_map(_ALIASES), patch.dict(sys.modules, {"anthropic": sdk}):
            live, _ = create_provider({"id": "ember-owl", "model": "quality"})
            await LLMClient(RecordingProvider(live)).create_message(
                model="claude-haiku-4-5", model_alias="fast",
                messages=[{"role": "user", "content": "bid"}], system="Bid.",
                tools=[], max_tokens=64, temperature=0.2,
            )
        sent = client.messages.create.call_args.kwargs
        assert "output_config" not in sent
        assert "cache_control" not in sent
        assert sent["system"] == "Bid."


class TestAThinkingTurnGoesBackWholeOnTheLiveCall:
    async def _second_call(self, first: LLMResponse) -> tuple[_Live, RecordingProvider]:
        live = _Live(first)
        recorder = RecordingProvider(live)
        response = await recorder.create_message(**_request())
        messages = recorder.append_tool_round([_USER], response, _RESULTS)
        await recorder.create_message(**_request(messages))
        return live, recorder

    async def test_the_live_call_carries_the_turn_as_the_provider_returned_it(self) -> None:
        live, _ = await self._second_call(_tool_turn(_THOUGHT_TURN))
        assert live.calls[1]["messages"][1] == {"role": "assistant", "content": _THOUGHT_TURN}

    async def test_the_rest_of_the_round_is_sent_unchanged(self) -> None:
        live, _ = await self._second_call(_tool_turn(_THOUGHT_TURN))
        assert live.calls[1]["messages"][0] == _USER
        assert live.calls[1]["messages"][2] == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "found"}],
        }

    async def test_a_turn_without_thinking_is_sent_in_the_canonical_shape(self) -> None:
        live, _ = await self._second_call(_tool_turn(None))
        assert live.calls[1]["messages"][1] == {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
            ],
        }

    async def test_the_recorded_round_replays(self) -> None:
        # Replay never sees a thinking block: its cassette holds text and tool
        # calls only. The follow-up request must still hit the recorded key.
        _, recorder = await self._second_call(_tool_turn(_THOUGHT_TURN))
        replay = ReplayProvider(recorder.cassette)
        first = await replay.create_message(**_request())
        assert first.provider_content is None
        messages = replay.append_tool_round([_USER], first, _RESULTS)
        assert (await replay.create_message(**_request(messages))).text == "done"
