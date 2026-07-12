"""Tests for the IBM watsonx.ai provider (``agents.llm_watsonx``).

RFC 0053 PR 2 — a first-class ``WatsonxProvider`` on the native
``ibm-watsonx-ai`` SDK. watsonx has no broad OpenAI-compatible *endpoint*, so a
native class is required — but its chat wire format IS OpenAI-shaped (a
``choices[].message`` dict with ``tool_calls`` whose ``function.arguments`` is a
JSON string, plus an OpenAI-style ``usage`` block), so the translation mirrors
``OpenAIProvider`` while the transport is the native ``ModelInference`` bound to
one ``model_id`` (built + cached lazily). The synchronous ``chat`` call is
offloaded via ``asyncio.to_thread`` so it never blocks the event loop.

No network is touched: the ``ibm-watsonx-ai`` SDK is mocked via ``sys.modules``
exactly the way :mod:`tests.unit.python.test_llm_client` mocks ``anthropic`` /
``openai`` — so these tests run whether or not the optional ``ibm-watsonx-ai``
extra is installed. The factory-branch routing tests (required ``project_id``
fail-closed, missing-key warning, missing-SDK SystemExit, provider-conflict, env
fallback) live in ``test_llm_factory_watsonx.py``, and the ``resolve_watsonx_config``
precedence/default rules in ``test_llm_watsonx_resolve.py``; this file covers the
core translation logic. The ``ibm-watsonx-ai`` doubles are shared via
``_watsonx_test_helpers``.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import patch

import pytest

from agents.llm_client import LLMResponse, LLMToolResult, StopReason, ToolCall
from agents.llm_client import WatsonxProvider as WatsonxProviderReexport
from agents.llm_watsonx import WatsonxProvider

from ._watsonx_test_helpers import (
    _make_watsonx_provider,
    _mock_watsonx_modules,
    _watsonx_response,
    _watsonx_tool_call,
)

# ─── WatsonxProvider — identity ─────────────────────────────


def test_provider_name_is_watsonx() -> None:
    """The OTel gen_ai.system attribute must read 'watsonx', not 'openai'."""
    assert WatsonxProvider.name == "watsonx"


def test_watsonx_provider_reexported_from_llm_client() -> None:
    assert WatsonxProviderReexport is WatsonxProvider


# ─── construction / lazy per-model ModelInference ───────────


def test_init_builds_model_inference_with_project_id_and_credentials() -> None:
    """A model is built lazily on first use: Credentials(url, api_key) +
    ModelInference(model_id, credentials, project_id)."""
    ibm_mod, fm_mod, _model = _mock_watsonx_modules()
    with patch.dict(
        sys.modules,
        {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
    ):
        p = WatsonxProvider(
            api_key="secret-key",
            url="https://us-south.ml.cloud.ibm.com",
            project_id="proj-1",
        )
        p._get_model("meta-llama/llama-3-3-70b-instruct")
    ibm_mod.Credentials.assert_called_once_with(
        url="https://us-south.ml.cloud.ibm.com", api_key="secret-key"
    )
    kwargs = fm_mod.ModelInference.call_args.kwargs
    assert kwargs["model_id"] == "meta-llama/llama-3-3-70b-instruct"
    assert kwargs["project_id"] == "proj-1"
    assert "space_id" not in kwargs
    assert kwargs["credentials"] is ibm_mod.Credentials.return_value


def test_init_uses_space_id_when_no_project_id() -> None:
    """space_id is the documented alternative to project_id (RFC 0053 §C)."""
    ibm_mod, fm_mod, _model = _mock_watsonx_modules()
    with patch.dict(
        sys.modules,
        {"ibm_watsonx_ai": ibm_mod, "ibm_watsonx_ai.foundation_models": fm_mod},
    ):
        p = WatsonxProvider(
            api_key="k", url="https://eu-de.ml.cloud.ibm.com", space_id="space-9"
        )
        p._get_model("ibm/granite-3-8b-instruct")
    kwargs = fm_mod.ModelInference.call_args.kwargs
    assert kwargs["space_id"] == "space-9"
    assert "project_id" not in kwargs


def test_get_model_caches_per_model_id() -> None:
    """ModelInference is bound to one model_id, so it is built once per id and
    reused (a chat turn should not reconstruct the client each call)."""
    provider, _model = _make_watsonx_provider()
    m1 = provider._get_model("meta-llama/llama-3-3-70b-instruct")
    m2 = provider._get_model("meta-llama/llama-3-3-70b-instruct")
    m3 = provider._get_model("ibm/granite-3-8b-instruct")
    assert m1 is m2
    # Two distinct model ids → two ModelInference constructions.
    assert provider._ModelInference.call_count == 2
    assert m3 is not None


# ─── format_tool_definitions (OpenAI-shaped) ────────────────


def test_format_tool_definitions_maps_to_openai_function_tools() -> None:
    provider, _model = _make_watsonx_provider()
    tools = [
        {
            "name": "file_read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    ]
    assert provider.format_tool_definitions(tools) == [
        {
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]


def test_format_tool_definitions_empty() -> None:
    provider, _model = _make_watsonx_provider()
    assert provider.format_tool_definitions([]) == []


# ─── _normalize (create_message response mapping) ───────────


async def test_text_response() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response()
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        system="You are helpful",
        tools=[],
        max_tokens=1024,
        temperature=0.3,
    )
    assert resp.text == "Hello from watsonx"
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.usage.input_tokens == 100
    assert resp.usage.output_tokens == 50
    assert resp.tool_calls == []


async def test_tool_call_response_parses_json_arguments() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(
        content=None,
        tool_calls=[_watsonx_tool_call(arguments='{"path": "main.py"}')],
        finish_reason="tool_calls",
    )
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct",
        messages=[],
        system="",
        tools=[{"type": "function", "function": {"name": "file_read"}}],
        max_tokens=1024,
        temperature=0.3,
    )
    assert resp.stop_reason == StopReason.TOOL_USE
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "call_1"
    assert resp.tool_calls[0].name == "file_read"
    # The JSON-string arguments are parsed to a dict (watsonx returns a string).
    assert resp.tool_calls[0].input == {"path": "main.py"}


async def test_tool_call_presence_overrides_stop_finish_reason() -> None:
    """A tool_calls part means TOOL_USE even if the model reports finish_reason
    'stop' (robustness — mirrors the Gemini provider's presence-derived rule)."""
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(
        content=None, tool_calls=[_watsonx_tool_call()], finish_reason="stop"
    )
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.stop_reason == StopReason.TOOL_USE


async def test_tool_call_invalid_json_falls_back_to_empty_input() -> None:
    """Invalid JSON in function.arguments falls back to an empty dict (keeps the
    agent loop running — the OpenAI provider's M2 fix)."""
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(
        content=None,
        tool_calls=[_watsonx_tool_call(arguments="{not json")],
        finish_reason="tool_calls",
    )
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.tool_calls[0].input == {}


async def test_tool_call_dict_arguments_passed_through() -> None:
    """Some models/SDK versions hand back arguments already parsed to a dict."""
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(
        content=None,
        tool_calls=[_watsonx_tool_call(arguments={"path": "x.py"})],
        finish_reason="tool_calls",
    )
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.tool_calls[0].input == {"path": "x.py"}


async def test_tool_call_without_id_falls_back_to_name() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(
        content=None,
        tool_calls=[_watsonx_tool_call(call_id="", name="search", arguments="{}")],
        finish_reason="tool_calls",
    )
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.tool_calls[0].id == "search"
    assert resp.tool_calls[0].name == "search"


async def test_max_tokens_stop() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(finish_reason="length")
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=10, temperature=0.3,
    )
    assert resp.stop_reason == StopReason.MAX_TOKENS


async def test_eos_token_maps_to_end_turn() -> None:
    """watsonx foundation models sometimes report 'eos_token' for a natural
    completion — map it to END_TURN, not the unmapped warn-default."""
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(finish_reason="eos_token")
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.stop_reason == StopReason.END_TURN


async def test_time_limit_maps_to_max_tokens() -> None:
    """watsonx chat reports 'time_limit' when the reply is truncated by the
    per-request time cap — a cut-short turn, so it maps to MAX_TOKENS (not the
    warn-defaulted END_TURN, which would tell the agent loop the turn is
    naturally complete)."""
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(finish_reason="time_limit")
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.stop_reason == StopReason.MAX_TOKENS


async def test_unmapped_finish_reason_defaults_to_end_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(finish_reason="content_filter")
    with caplog.at_level(logging.WARNING):
        resp = await provider.create_message(
            model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
            tools=[], max_tokens=64, temperature=0.0,
        )
    assert resp.stop_reason == StopReason.END_TURN
    assert any("content_filter" in r.message for r in caplog.records)


async def test_empty_content_maps_to_none() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(content="")
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.text is None


async def test_no_usage_block() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response(include_usage=False)
    resp = await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.usage.input_tokens == 0
    assert resp.usage.output_tokens == 0


async def test_no_choices_warns_and_defaults_to_end_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 0}}
    with caplog.at_level(logging.WARNING):
        resp = await provider.create_message(
            model="meta-llama/llama-3-3-70b-instruct", messages=[], system="",
            tools=[], max_tokens=64, temperature=0.0,
        )
    assert resp.text is None
    assert resp.stop_reason == StopReason.END_TURN
    assert resp.usage.input_tokens == 3
    assert any("no choices" in r.message.lower() for r in caplog.records)


# ─── create_message request wiring ──────────────────────────


async def test_create_message_threads_system_messages_params_and_tools() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response()
    await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        system="You are helpful",
        tools=[{"type": "function", "function": {"name": "t"}}],
        max_tokens=1024,
        temperature=0.3,
    )
    kwargs = model.chat.call_args.kwargs
    # System is prepended as a system-role message (OpenAI-shaped), then history.
    assert kwargs["messages"] == [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "hi"},
    ]
    assert kwargs["params"] == {"max_tokens": 1024, "temperature": 0.3}
    assert kwargs["tools"] == [{"type": "function", "function": {"name": "t"}}]


async def test_create_message_omits_system_and_tools_when_empty() -> None:
    provider, model = _make_watsonx_provider()
    model.chat.return_value = _watsonx_response()
    await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=64,
        temperature=0.0,
    )
    kwargs = model.chat.call_args.kwargs
    # No system-role message when system is empty; no tools key when none.
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in kwargs


async def test_create_message_routes_to_the_requested_model() -> None:
    """The physical model id selects (and caches) the bound ModelInference."""
    provider, _model = _make_watsonx_provider()
    provider._get_model("ibm/granite-3-8b-instruct").chat.return_value = _watsonx_response(
        content="granite"
    )
    resp = await provider.create_message(
        model="ibm/granite-3-8b-instruct", messages=[{"role": "user", "content": "hi"}],
        system="", tools=[], max_tokens=64, temperature=0.0,
    )
    assert resp.text == "granite"
    assert provider._ModelInference.call_args.kwargs["model_id"] == "ibm/granite-3-8b-instruct"


# ─── append_tool_round (multi-round tool loop, OpenAI-shaped) ──


def test_append_tool_round() -> None:
    provider, _model = _make_watsonx_provider()
    messages = [{"role": "user", "content": "read file"}]
    response = LLMResponse(
        text="Reading...",
        tool_calls=[ToolCall(id="call_1", name="file_read", input={"path": "x.py"})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="call_1", content="file content", is_error=False)]
    new_msgs = provider.append_tool_round(messages, response, results)

    assert len(new_msgs) == 3
    assert new_msgs[0] == messages[0]
    # assistant turn: text content + tool_calls with JSON-string arguments.
    assert new_msgs[1]["role"] == "assistant"
    assert new_msgs[1]["content"] == "Reading..."
    assert new_msgs[1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_read", "arguments": '{"path": "x.py"}'},
        }
    ]
    # tool turn: one tool-role message per result, keyed by tool_call_id.
    assert new_msgs[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "file content",
    }


def test_append_tool_round_error_result() -> None:
    provider, _model = _make_watsonx_provider()
    response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="call_1", name="bad_tool", input={})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="call_1", content="Permission denied", is_error=True)]
    new_msgs = provider.append_tool_round([], response, results)
    # No text → empty-string content (OpenAI-shaped assistant message).
    assert new_msgs[0]["content"] == ""
    assert new_msgs[1]["content"] == "Permission denied"


async def test_append_tool_round_output_roundtrips_through_create_message() -> None:
    """The messages append_tool_round produces are accepted as-is by the next
    create_message call (the multi-round tool loop stays in native format)."""
    provider, model = _make_watsonx_provider()
    response = LLMResponse(
        text="calling",
        tool_calls=[ToolCall(id="call_1", name="file_read", input={"path": "x.py"})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="call_1", content="data", is_error=False)]
    next_messages = provider.append_tool_round(
        [{"role": "user", "content": "go"}], response, results
    )
    model.chat.return_value = _watsonx_response()
    await provider.create_message(
        model="meta-llama/llama-3-3-70b-instruct", messages=next_messages, system="",
        tools=[], max_tokens=64, temperature=0.0,
    )
    sent = model.chat.call_args.kwargs["messages"]
    assert sent[0] == {"role": "user", "content": "go"}
    assert sent[1]["role"] == "assistant"
    assert sent[2]["role"] == "tool"
