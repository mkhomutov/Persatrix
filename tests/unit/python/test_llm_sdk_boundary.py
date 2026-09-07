"""
Contract tests for the seam where the providers meet the real vendor SDKs.

Every other LLM test in this tree mocks the SDK client, which means it asserts
that *our* code does what we think — never that the installed library still
accepts what we send it or still returns what we read back. That gap is real
and has bitten: the first pip dependency sweep proposed ``anthropic<2``, where
``messages.create`` no longer takes ``temperature``, and CI went green because
the suite builds the provider (whose constructor did not change) and mocks the
client from there. See ISSUE-0144.

These tests import the vendor SDK for real and let it do its own argument
validation, request building and response parsing. What they replace is only
the socket: a ``MockTransport`` answers with a canned body, so a run needs no
API key and makes no network call, exactly like the rest of the suite.

The transport is swapped on the client the SDK built for itself, and the httpx
flavour is read back off that client rather than imported by name. That is not
defensiveness for its own sake — ``anthropic`` 1.x moved from ``httpx`` to
``httpx2``, and handing it an ``httpx.AsyncClient`` raises during setup. A
harness that hardcoded the import would fail on its own plumbing before
reaching the contract, reporting the wrong defect for the right change.

Each provider is covered from both directions, because a major bump can break
either:

* **Request side** — the kwargs ``create_message`` builds are accepted by the
  installed SDK, and survive into the wire body. A removed or renamed
  parameter fails here.
* **Response side** — the SDK's own parsed model carries the fields
  ``_normalize`` reads. A renamed response field fails here.

Not covered: the Gemini provider. ``google-genai`` is an optional extra that
CI does not install, so a test here would skip in CI and imply coverage that
does not exist. ISSUE-0144 records it.
"""

import importlib
import json
from typing import Any

import pytest

from agents.llm_client import AnthropicProvider, OpenAIProvider, StopReason

# ─── Canned wire bodies ─────────────────────────────────────
#
# Shaped like the real APIs so the SDKs parse them into their own typed
# models. If a vendor changes a response field these stop parsing, which is
# the point.

_ANTHROPIC_TEXT_BODY = {
    "id": "msg_boundary_text",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "hello from the boundary"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 11, "output_tokens": 5},
}

_ANTHROPIC_TOOL_BODY = {
    "id": "msg_boundary_tool",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [
        {
            "type": "tool_use",
            "id": "toolu_boundary",
            "name": "lookup",
            "input": {"query": "persatrix"},
        }
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 13, "output_tokens": 9},
}

_OPENAI_TEXT_BODY = {
    "id": "chatcmpl_boundary_text",
    "object": "chat.completion",
    "created": 1_760_000_000,
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello from the boundary"},
            "finish_reason": "stop",
            "logprobs": None,
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}

_OPENAI_TOOL_BODY = {
    "id": "chatcmpl_boundary_tool",
    "object": "chat.completion",
    "created": 1_760_000_000,
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_boundary",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"query": "persatrix"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "logprobs": None,
        }
    ],
    "usage": {"prompt_tokens": 13, "completion_tokens": 9, "total_tokens": 22},
}

_TOOLS = [
    {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


class _Recorder:
    """Answers one canned body and keeps the request the SDK actually built.

    ``hx`` is the httpx module the SDK itself uses — see the module docstring.
    """

    def __init__(self, body: dict[str, Any], hx: Any):
        self._body = body
        self._hx = hx
        self.request: Any = None

    def transport(self) -> Any:
        def handle(request: Any) -> Any:
            self.request = request
            return self._hx.Response(200, json=self._body)

        return self._hx.MockTransport(handle)

    @property
    def sent(self) -> dict[str, Any]:
        assert self.request is not None, "the SDK never issued a request"
        return json.loads(self.request.content)


def _intercept(sdk_client: Any, body: dict[str, Any]) -> _Recorder:
    """Point the SDK's own http client at a canned response.

    ``sdk_client._client`` is the http client the SDK constructed — a subclass
    of its httpx ``AsyncClient``. Its transport names the flavour, so the
    right ``MockTransport`` is built without guessing which httpx is in play.
    """
    inner = sdk_client._client
    flavour = type(inner._transport).__module__.split(".")[0]
    assert flavour.startswith("httpx"), (
        f"expected an httpx-based transport, got {flavour} — the SDK changed "
        "its HTTP layer and this harness needs rechecking"
    )
    recorder = _Recorder(body, importlib.import_module(flavour))
    inner._transport = recorder.transport()
    return recorder


def _anthropic_against(body: dict[str, Any]) -> tuple[AnthropicProvider, _Recorder]:
    """A provider whose real SDK client answers from a mock transport."""
    import anthropic

    provider = AnthropicProvider(api_key="not-a-real-key")
    provider._client = anthropic.AsyncAnthropic(api_key="not-a-real-key")
    return provider, _intercept(provider._client, body)


def _openai_against(body: dict[str, Any]) -> tuple[OpenAIProvider, _Recorder]:
    import openai

    provider = OpenAIProvider(api_key="not-a-real-key")
    provider._client = openai.AsyncOpenAI(api_key="not-a-real-key")
    return provider, _intercept(provider._client, body)


# ─── Anthropic ──────────────────────────────────────────────


async def test_anthropic_request_kwargs_survive_the_real_sdk() -> None:
    """Every kwarg create_message builds reaches the wire.

    This is the regression the ``anthropic<2`` widening would have caused:
    1.x dropped ``temperature`` from ``messages.create``, so the call raises
    ``TypeError`` before any request is built and this fails at the await.
    A parameter the SDK accepts but silently discards fails on the body
    assertions instead.
    """
    provider, recorder = _anthropic_against(_ANTHROPIC_TEXT_BODY)

    await provider.create_message(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        system="be brief",
        tools=provider.format_tool_definitions(_TOOLS),
        max_tokens=64,
        temperature=0.4,
    )

    sent = recorder.sent
    assert sent["model"] == "claude-sonnet-4-5"
    assert sent["max_tokens"] == 64
    assert sent["temperature"] == 0.4
    assert sent["system"] == "be brief"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["tools"][0]["name"] == "lookup"
    # format_tool_definitions renames `parameters` to the Anthropic spelling;
    # if the SDK stopped accepting it the request would have been rejected.
    assert sent["tools"][0]["input_schema"]["required"] == ["query"]


async def test_anthropic_normalizes_a_real_sdk_response() -> None:
    """_normalize reads fields the SDK's own parsed model still carries."""
    provider, _ = _anthropic_against(_ANTHROPIC_TEXT_BODY)

    result = await provider.create_message(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )

    assert result.text == "hello from the boundary"
    assert result.tool_calls == []
    assert result.stop_reason is StopReason.END_TURN
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 5


async def test_anthropic_normalizes_a_real_tool_use_response() -> None:
    """The tool_use branch of _normalize against a real parsed block."""
    provider, _ = _anthropic_against(_ANTHROPIC_TOOL_BODY)

    result = await provider.create_message(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "look it up"}],
        system="",
        tools=provider.format_tool_definitions(_TOOLS),
        max_tokens=64,
        temperature=0.0,
    )

    assert result.text is None
    assert result.stop_reason is StopReason.TOOL_USE
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "toolu_boundary"
    assert call.name == "lookup"
    assert call.input == {"query": "persatrix"}


# ─── OpenAI ─────────────────────────────────────────────────


async def test_openai_request_kwargs_survive_the_real_sdk() -> None:
    """The OpenAI half of the same contract.

    ``max_tokens`` is the one to watch: the vendor has been steering callers
    to ``max_completion_tokens`` for newer models, so a major that finally
    removes it fails here rather than in production.
    """
    provider, recorder = _openai_against(_OPENAI_TEXT_BODY)

    await provider.create_message(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        system="be brief",
        tools=provider.format_tool_definitions(_TOOLS),
        max_tokens=64,
        temperature=0.4,
    )

    sent = recorder.sent
    assert sent["model"] == "gpt-4o"
    assert sent["max_tokens"] == 64
    assert sent["temperature"] == 0.4
    # The provider folds `system` into the message list rather than sending a
    # top-level field, so the contract here is ordering, not a key.
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["messages"][1] == {"role": "user", "content": "hi"}
    assert sent["tools"][0]["function"]["name"] == "lookup"


async def test_openai_normalizes_a_real_sdk_response() -> None:
    provider, _ = _openai_against(_OPENAI_TEXT_BODY)

    result = await provider.create_message(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )

    assert result.text == "hello from the boundary"
    assert result.tool_calls == []
    assert result.stop_reason is StopReason.END_TURN
    # Distinct from the Anthropic spelling — _normalize reads prompt_tokens /
    # completion_tokens here, and a rename would land on these two lines.
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 5


async def test_openai_normalizes_a_real_tool_call_response() -> None:
    """Covers the nested function.arguments JSON string _normalize parses."""
    provider, _ = _openai_against(_OPENAI_TOOL_BODY)

    result = await provider.create_message(
        model="gpt-4o",
        messages=[{"role": "user", "content": "look it up"}],
        system="",
        tools=provider.format_tool_definitions(_TOOLS),
        max_tokens=64,
        temperature=0.0,
    )

    assert result.stop_reason is StopReason.TOOL_USE
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_boundary"
    assert call.name == "lookup"
    assert call.input == {"query": "persatrix"}


# ─── The seam itself ────────────────────────────────────────


@pytest.mark.parametrize("module_name", ["anthropic", "openai"])
def test_the_sdk_under_test_is_the_real_one(module_name: str) -> None:
    """Guard against these tests quietly becoming mock tests.

    If a conftest or an import-order change ever puts a MagicMock in
    ``sys.modules`` for a vendor SDK, every assertion above would still pass
    against the mock and the coverage would be gone without a failure. This
    asserts the module is the installed distribution.
    """
    import importlib
    import importlib.metadata as metadata

    module = importlib.import_module(module_name)
    assert metadata.version(module_name), f"{module_name} is not installed"
    assert type(module).__name__ == "module", (
        f"{module_name} resolved to {type(module).__name__}, not a real module — "
        "these tests would be asserting against a mock"
    )
