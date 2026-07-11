"""Tests for the Google Gemini provider (``agents.llm_gemini``).

RFC 0053 PR 1 — a first-class ``GeminiProvider`` on the native
``google-genai`` SDK, the second concrete dogfood of the RFC 0033 §H
multi-provider extensibility seam (after Ollama). Unlike Ollama (a thin
OpenAI-compatible subclass) Gemini is a **native** class: it owns its own
request build (``contents`` / ``config``), its ``function_declarations``
tool mapping, and its ``candidates`` response normalisation.

No network is touched: the ``google-genai`` SDK is mocked via ``sys.modules``
exactly the way :mod:`tests.unit.python.test_llm_client` /
:mod:`tests.unit.python.test_llm_ollama` mock ``anthropic`` / ``openai`` — so
these tests run whether or not the optional ``google-genai`` extra is
installed.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.llm_client import GeminiProvider as GeminiProviderReexport
from agents.llm_client import (
    LLMResponse,
    LLMToolResult,
    StopReason,
    ToolCall,
)
from agents.llm_gemini import GeminiProvider

from ._gemini_test_helpers import (
    _gemini_part,
    _gemini_response,
    _make_gemini_provider,
    _mock_genai_modules,
)

# The ``create_provider`` gemini-branch routing tests (key fallback, missing-key
# warning, missing-SDK SystemExit, provider-conflict) live in
# ``test_llm_factory.py`` alongside the other providers' factory tests; the
# thinking-budget lever + prompt-block / truncation edge cases live in
# ``test_llm_gemini_edge.py`` — this file covers the core translation logic. The
# ``google-genai`` doubles are shared via ``_gemini_test_helpers``.


# ─── GeminiProvider — identity ──────────────────────────────


def test_provider_name_is_gemini() -> None:
    """The OTel gen_ai.system attribute must read 'gemini', not 'openai'."""
    assert GeminiProvider.name == "gemini"


def test_gemini_provider_reexported_from_llm_client() -> None:
    assert GeminiProviderReexport is GeminiProvider


def test_init_imports_native_sdk_and_builds_client() -> None:
    """__init__ imports the native SDK; the Gemini-API path passes the key.

    The client is built lazily (so a missing key warns at startup and fails on
    the first request, not at construction — S-09), but the default Gemini-API
    path threads the api_key to ``genai.Client``.
    """
    google_mod, genai_mod, _client = _mock_genai_modules()
    with patch.dict(
        sys.modules, {"google": google_mod, "google.genai": genai_mod}
    ):
        p = GeminiProvider(api_key="secret-key")
        # Force the lazy client build.
        p._get_client()
    genai_mod.Client.assert_called_once_with(api_key="secret-key")


def test_init_vertex_path_when_project_and_location_present() -> None:
    """A provider_config with project+location routes through Vertex, not the key."""
    google_mod, genai_mod, _client = _mock_genai_modules()
    with patch.dict(
        sys.modules, {"google": google_mod, "google.genai": genai_mod}
    ):
        p = GeminiProvider(
            api_key="ignored",
            provider_config={"project": "proj-1", "location": "us-central1"},
        )
        p._get_client()
    genai_mod.Client.assert_called_once_with(
        vertexai=True, project="proj-1", location="us-central1"
    )


# ─── format_tool_definitions ────────────────────────────────


def test_format_tool_definitions_maps_to_function_declarations() -> None:
    provider = _make_gemini_provider()
    tools = [
        {
            "name": "file_read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {"name": "noop", "description": "Nothing", "parameters": {"type": "object"}},
    ]
    result = provider.format_tool_definitions(tools)
    # A single Gemini Tool wrapping all function declarations.
    assert result == [
        {
            "function_declarations": [
                {
                    "name": "file_read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
                {
                    "name": "noop",
                    "description": "Nothing",
                    "parameters": {"type": "object"},
                },
            ]
        }
    ]


def test_format_tool_definitions_empty() -> None:
    provider = _make_gemini_provider()
    assert provider.format_tool_definitions([]) == []


# ─── _normalize (create_message response mapping) ───────────


async def test_text_response() -> None:
    provider = _make_gemini_provider()
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response()
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "hi"}],
        system="You are helpful",
        tools=[],
        max_tokens=1024,
        temperature=0.3,
    )
    assert resp.text == "Hello from Gemini"
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.usage.input_tokens == 100
    assert resp.usage.output_tokens == 50
    assert resp.tool_calls == []


async def test_tool_call_response() -> None:
    provider = _make_gemini_provider()
    fc = SimpleNamespace(id="fc_1", name="file_read", args={"path": "main.py"})
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(parts=[_gemini_part(function_call=fc)])
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro",
        messages=[],
        system="",
        tools=[{"function_declarations": [{"name": "file_read"}]}],
        max_tokens=1024,
        temperature=0.3,
    )
    # A function-call part means TOOL_USE regardless of the STOP finish_reason
    # (Gemini reports STOP even when it emits a function call).
    assert resp.stop_reason == StopReason.TOOL_USE
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "fc_1"
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].input == {"path": "main.py"}


async def test_tool_call_without_id_falls_back_to_name() -> None:
    """Gemini function calls may carry no id; downstream keys on id, so use name."""
    provider = _make_gemini_provider()
    fc = SimpleNamespace(id=None, name="search", args={"q": "x"})
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(parts=[_gemini_part(function_call=fc)])
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.tool_calls[0].id == "search"
    assert resp.tool_calls[0].name == "search"


async def test_mixed_text_and_tool() -> None:
    provider = _make_gemini_provider()
    parts = [
        _gemini_part(text="Let me read that."),
        _gemini_part(
            function_call=SimpleNamespace(id="t1", name="file_read", args={"path": "x.py"})
        ),
    ]
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(parts=parts)
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=1024, temperature=0.3,
    )
    assert resp.text == "Let me read that."
    assert len(resp.tool_calls) == 1
    assert resp.stop_reason == StopReason.TOOL_USE


async def test_max_tokens_stop() -> None:
    provider = _make_gemini_provider()
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(finish_reason="MAX_TOKENS")
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=10, temperature=0.3,
    )
    assert resp.stop_reason == StopReason.MAX_TOKENS


async def test_unmapped_finish_reason_defaults_to_end_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _make_gemini_provider()
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(finish_reason="SAFETY")
    )
    with caplog.at_level(logging.WARNING):
        resp = await provider.create_message(
            model="gemini-2.5-pro", messages=[], system="", tools=[],
            max_tokens=64, temperature=0.0,
        )
    assert resp.stop_reason == StopReason.END_TURN
    assert any("SAFETY" in r.message for r in caplog.records)


async def test_missing_finish_reason_defaults_to_end_turn() -> None:
    provider = _make_gemini_provider()
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(finish_reason=None)
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.stop_reason == StopReason.END_TURN


async def test_no_usage_metadata() -> None:
    provider = _make_gemini_provider()
    resp_obj = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[_gemini_part(text="hi")]),
                finish_reason=SimpleNamespace(name="STOP"),
            )
        ],
        usage_metadata=None,
    )
    provider._client.aio.models.generate_content = AsyncMock(return_value=resp_obj)
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.usage.input_tokens == 0
    assert resp.usage.output_tokens == 0


async def test_thoughts_tokens_count_as_output() -> None:
    """Gemini 2.5 reasoning ("thoughts") tokens are billed at the output rate
    and reported in a *separate* ``thoughts_token_count`` field, so output
    tokens = candidates + thoughts (counting candidates alone under-charges
    every 2.5 call — the cost/RFC-0023-budget accuracy fix)."""
    provider = _make_gemini_provider()
    resp_obj = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[_gemini_part(text="hi")]),
                finish_reason=SimpleNamespace(name="STOP"),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=50,
            thoughts_token_count=200,
        ),
    )
    provider._client.aio.models.generate_content = AsyncMock(return_value=resp_obj)
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.usage.input_tokens == 100
    # 50 visible + 200 thinking, not 50.
    assert resp.usage.output_tokens == 250


# ─── create_message request wiring ──────────────────────────


async def test_create_message_threads_config_and_contents() -> None:
    provider = _make_gemini_provider()
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "hi"}],
        system="You are helpful",
        tools=[{"function_declarations": [{"name": "t"}]}],
        max_tokens=1024,
        temperature=0.3,
    )
    kwargs = gc.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-pro"
    # The role/content dicts map to Gemini contents with a text part.
    assert kwargs["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    config = kwargs["config"]
    assert config["temperature"] == 0.3
    assert config["max_output_tokens"] == 1024
    assert config["system_instruction"] == "You are helpful"
    assert config["tools"] == [{"function_declarations": [{"name": "t"}]}]


async def test_create_message_omits_system_and_tools_when_empty() -> None:
    provider = _make_gemini_provider()
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )
    config = gc.call_args.kwargs["config"]
    assert "system_instruction" not in config
    assert "tools" not in config


async def test_create_message_maps_assistant_role_to_model() -> None:
    provider = _make_gemini_provider()
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model="gemini-2.5-pro",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )
    contents = gc.call_args.kwargs["contents"]
    assert contents[1]["role"] == "model"


# ─── append_tool_round (multi-round tool loop) ──────────────


def test_append_tool_round() -> None:
    provider = _make_gemini_provider()
    messages = [{"role": "user", "content": "read file"}]
    response = LLMResponse(
        text="Reading...",
        tool_calls=[ToolCall(id="t1", name="file_read", input={"path": "x.py"})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="t1", content="file content", is_error=False)]
    new_msgs = provider.append_tool_round(messages, response, results)

    assert len(new_msgs) == 3
    assert new_msgs[0] == messages[0]
    # model turn: optional text + a function_call part
    assert new_msgs[1]["role"] == "model"
    assert {"text": "Reading..."} in new_msgs[1]["parts"]
    fc_parts = [p for p in new_msgs[1]["parts"] if "function_call" in p]
    assert fc_parts == [
        {"function_call": {"name": "file_read", "args": {"path": "x.py"}, "id": "t1"}}
    ]
    # user turn: a function_response keyed by the tool NAME (Gemini matches by name)
    assert new_msgs[2]["role"] == "user"
    fr = new_msgs[2]["parts"][0]["function_response"]
    assert fr["name"] == "file_read"
    assert fr["response"] == {"output": "file content"}


def test_append_tool_round_error_result() -> None:
    provider = _make_gemini_provider()
    response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="t1", name="bad_tool", input={})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="t1", content="Permission denied", is_error=True)]
    new_msgs = provider.append_tool_round([], response, results)
    # No text part when response.text is None.
    assert all("text" not in p for p in new_msgs[0]["parts"])
    fr = new_msgs[1]["parts"][0]["function_response"]
    assert fr["response"] == {"error": "Permission denied"}


async def test_append_tool_round_output_roundtrips_through_create_message() -> None:
    """The Gemini contents append_tool_round produces are accepted as-is by
    the next create_message call (the multi-round tool loop stays in one
    provider's native format)."""
    provider = _make_gemini_provider()
    response = LLMResponse(
        text="calling",
        tool_calls=[ToolCall(id="t1", name="file_read", input={"path": "x.py"})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="t1", content="data", is_error=False)]
    next_messages = provider.append_tool_round(
        [{"role": "user", "content": "go"}], response, results
    )
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model="gemini-2.5-pro", messages=next_messages, system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    contents = gc.call_args.kwargs["contents"]
    # The already-shaped model/user turns (carrying "parts") pass through intact;
    # the leading plain text turn is converted to a text part.
    assert contents[0] == {"role": "user", "parts": [{"text": "go"}]}
    assert contents[1]["role"] == "model"
    assert contents[2]["role"] == "user"
    assert "function_response" in contents[2]["parts"][0]
