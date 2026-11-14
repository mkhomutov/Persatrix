"""Contract tests for the seam between the Gemini adapter and its SDK.

``test_llm_sdk_boundary.py`` runs the Anthropic and OpenAI adapters against
the real SDKs. The Gemini adapter had no such test: ``google-genai`` comes
with the optional ``providers`` extra, which no CI job installed, so every
test of the adapter put a ``MagicMock`` where the SDK should be (ISSUE-0220).
A renamed keyword in the SDK would have broken the four-vendor demo with
every check green.

These tests import ``google-genai`` for real and let it do its own argument
checking, request building and response parsing. Only the socket is
replaced, and a guard fails any test that tries to open one.

They skip where the SDK is not installed. ``make test-providers``, which
CI's Python job runs after installing the extra, sets
``PERSATRIX_REQUIRE_PROVIDER_SDKS=1``: there a missing SDK is a failure, so
the tests cannot skip in the one run that exists to run them.

What is covered and what is not:

* **The Developer API**: the client the adapter builds, every keyword of
  ``generate_content``, and every response field ``_normalize`` reads.
* **Vertex AI**: building the client only. A request needs Google
  credentials, which the SDK looks for on the network.
"""

from __future__ import annotations

import base64
import importlib
import json
from typing import Any

import pytest

from agents.llm_client import StopReason
from agents.llm_gemini import GeminiProvider
from agents.llm_types import LLMToolResult

from ._sdk_boundary_helpers import TOOLS, Recorder, provider_sdk, refuse_network


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    refuse_network(monkeypatch)


# Bodies in the REST API's own spelling, so the SDK parses them into its
# typed models. A renamed response field stops parsing, which is the point.

_GEMINI_TEXT_BODY = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "hello from the boundary"}]},
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 11,
        "cachedContentTokenCount": 4,
        "candidatesTokenCount": 5,
        "thoughtsTokenCount": 7,
        "totalTokenCount": 23,
    },
}

# base64 of b"sig-boundary": the wire carries a thought signature as base64
# and the SDK hands it to the adapter as bytes.
_SIGNATURE_WIRE = base64.b64encode(b"sig-boundary").decode()

_GEMINI_TOOL_BODY = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "lookup", "args": {"query": "persatrix"}},
                        "thoughtSignature": _SIGNATURE_WIRE,
                    }
                ],
            },
            # Gemini reports STOP even when it asks for a tool.
            "finishReason": "STOP",
            "index": 0,
        }
    ],
    "usageMetadata": {"promptTokenCount": 13, "candidatesTokenCount": 9, "totalTokenCount": 22},
}


def _gemini_against(
    body: dict[str, Any], provider_config: dict[str, Any] | None = None
) -> tuple[GeminiProvider, Recorder]:
    """A provider whose real SDK client answers from a mock transport."""
    provider_sdk("google.genai")
    provider = GeminiProvider(api_key="not-a-real-key", provider_config=provider_config)
    # The client the adapter builds for itself, as in production.
    api = provider._get_client().aio._api_client
    inner = api._async_httpx_client
    flavour = type(inner._transport).__module__.split(".")[0]
    assert flavour.startswith("httpx"), (
        f"expected an httpx-based transport, got {flavour}: the SDK changed "
        "its HTTP layer and this harness needs rechecking"
    )
    recorder = Recorder(body, importlib.import_module(flavour))
    inner._transport = recorder.transport()
    # With aiohttp installed the SDK sends async requests through aiohttp,
    # unless the caller gave it an httpx client. This is that option, set on
    # the client the SDK already built. If a release stops honouring it, the
    # request reaches for a socket and the guard above fails the test.
    api._http_options.httpx_async_client = inner
    return provider, recorder


def _thinking(sent: dict[str, Any], snake: str, camel: str) -> Any:
    """A thinking setting off the wire body, under either spelling the API reads."""
    config = sent["generationConfig"]["thinkingConfig"]
    return config.get(camel, config.get(snake))


async def test_gemini_request_kwargs_survive_the_real_sdk() -> None:
    """Every keyword create_message builds is accepted and reaches the wire."""
    provider, recorder = _gemini_against(_GEMINI_TEXT_BODY, {"thinking_budget": 0})

    await provider.create_message(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        system="be brief",
        tools=provider.format_tool_definitions(TOOLS),
        max_tokens=64,
        temperature=0.4,
    )

    request = recorder.request
    # The key given to genai.Client(api_key=…) is the one the request carries.
    assert request.headers["x-goog-api-key"] == "not-a-real-key"
    assert request.url.path.endswith("/models/gemini-2.5-flash:generateContent")
    sent = recorder.sent
    assert sent["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert sent["systemInstruction"]["parts"] == [{"text": "be brief"}]
    assert sent["generationConfig"]["maxOutputTokens"] == 64
    assert sent["generationConfig"]["temperature"] == 0.4
    assert _thinking(sent, "thinking_budget", "thinkingBudget") == 0
    declaration = sent["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "lookup"
    assert declaration["parameters"]["required"] == ["query"]


async def test_gemini_3x_request_carries_a_thinking_level_and_no_temperature() -> None:
    provider, recorder = _gemini_against(_GEMINI_TEXT_BODY, {"thinking_level": "low"})

    await provider.create_message(
        model="gemini-3.8-flash",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.4,
    )

    sent = recorder.sent
    assert str(_thinking(sent, "thinking_level", "thinkingLevel")).upper() == "LOW"
    assert "temperature" not in sent["generationConfig"]
    assert "systemInstruction" not in sent and "tools" not in sent


async def test_gemini_normalizes_a_real_sdk_response() -> None:
    """_normalize reads fields the SDK's own parsed model still carries."""
    provider, _ = _gemini_against(_GEMINI_TEXT_BODY)

    result = await provider.create_message(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )

    assert result.text == "hello from the boundary"
    assert result.tool_calls == []
    assert result.stop_reason is StopReason.END_TURN
    # Four usage fields, each under the name _map_usage reads: the prompt
    # count less the cached part, and the reply plus the thinking tokens.
    assert result.usage.input_tokens == 7
    assert result.usage.cache_read_tokens == 4
    assert result.usage.output_tokens == 12


async def test_gemini_tool_call_round_trips_through_the_real_sdk() -> None:
    """A function call the SDK parses goes back in the next request, with the
    thought signature on the same part and the result keyed by name."""
    provider, recorder = _gemini_against(_GEMINI_TOOL_BODY)
    tools = provider.format_tool_definitions(TOOLS)
    messages: list[Any] = [{"role": "user", "content": "look it up"}]

    result = await provider.create_message(
        model="gemini-2.5-flash",
        messages=messages,
        system="",
        tools=tools,
        max_tokens=64,
        temperature=0.0,
    )

    assert result.stop_reason is StopReason.TOOL_USE
    (call,) = result.tool_calls
    assert call.name == "lookup"
    assert call.input == {"query": "persatrix"}
    assert call.signature == b"sig-boundary"

    replayed = provider.append_tool_round(
        messages, result, [LLMToolResult(tool_call_id=call.id, content="found", is_error=False)]
    )
    await provider.create_message(
        model="gemini-2.5-flash",
        messages=replayed,
        system="",
        tools=tools,
        max_tokens=64,
        temperature=0.0,
    )

    _, model_turn, user_turn = recorder.sent["contents"]
    assert model_turn["role"] == "model"
    (call_part,) = model_turn["parts"]
    assert call_part["functionCall"]["name"] == "lookup"
    assert call_part["functionCall"]["args"] == {"query": "persatrix"}
    assert call_part["thoughtSignature"] == _SIGNATURE_WIRE
    assert user_turn["role"] == "user"
    assert user_turn["parts"] == [
        {"functionResponse": {"name": "lookup", "response": {"output": "found"}}}
    ]


async def test_gemini_reads_a_truncated_reply_off_the_real_finish_reason() -> None:
    """The SDK hands finish_reason over as its own enum; the adapter reads its name."""
    body = json.loads(json.dumps(_GEMINI_TEXT_BODY))
    body["candidates"][0]["finishReason"] = "MAX_TOKENS"
    provider, _ = _gemini_against(body)

    result = await provider.create_message(
        model="gemini-2.5-flash",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=8,
        temperature=0.0,
    )

    assert result.stop_reason is StopReason.MAX_TOKENS


async def test_gemini_reads_a_blocked_prompt_off_the_real_sdk_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {"promptTokenCount": 3, "totalTokenCount": 3},
    }
    provider, _ = _gemini_against(body)

    with caplog.at_level("WARNING", logger="agents.llm_gemini"):
        result = await provider.create_message(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": "hi"}],
            system="",
            tools=[],
            max_tokens=8,
            temperature=0.0,
        )

    assert result.text is None and result.tool_calls == []
    assert result.usage.input_tokens == 3
    assert "SAFETY" in caplog.text


def test_gemini_vertex_client_is_built_from_project_and_location() -> None:
    """The three keywords of the Vertex path are ones the real constructor
    takes. No request is made: that needs Google credentials."""
    provider_sdk("google.genai")
    provider = GeminiProvider(
        api_key=None, provider_config={"project": "proj-boundary", "location": "us-central1"}
    )

    client = provider._get_client()

    assert client.vertexai is True
    api = client.aio._api_client
    assert (api.project, api.location) == ("proj-boundary", "us-central1")


# ─── The seam itself ────────────────────────────────────────


def test_the_sdk_under_test_is_the_real_one() -> None:
    """Guard against these tests quietly becoming mock tests.

    The other tests of this adapter put a ``MagicMock`` in ``sys.modules``
    while they build a provider. If one ever leaked, every assertion above
    would pass against the mock.
    """
    import importlib.metadata as metadata

    module = provider_sdk("google.genai")
    assert metadata.version("google-genai"), "google-genai is not installed"
    assert type(module).__name__ == "module", (
        f"google.genai resolved to {type(module).__name__}, not a real module: "
        "these tests would be asserting against a mock"
    )
