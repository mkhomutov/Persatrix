"""Edge-case tests for the Gemini provider (``agents.llm_gemini``).

Split from ``test_llm_gemini.py`` (which stays at the core translation logic)
so neither file crosses the 500-line review cap — the ``redactor_google_test.go``
split precedent. Covers the RFC 0053 PR 1 review follow-ups:

* the ``provider_config.thinking_budget`` lever (Gemini 2.5 thinking is drawn
  from ``max_output_tokens``, so a low budget can truncate the reply to empty);
* a prompt-blocked response (empty ``candidates`` + ``prompt_feedback``) warns
  instead of degrading to a silent empty turn;
* thoughts tokens are still billed on a ``MAX_TOKENS``-truncated turn;
* parallel calls to the *same* tool round-trip with positional correlation;
* the Gemini 3.x ``thought_signature`` (a thinking-model artifact) is captured
  off the tool-call part and replayed verbatim next turn — the API 400s the
  multi-round tool loop otherwise (RFC 0053 gemini-3.5-flash follow-up).

The ``google-genai`` doubles are shared via ``_gemini_test_helpers`` (no
network; runs with or without the optional extra installed).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.llm_client import LLMResponse, LLMToolResult, StopReason, ToolCall

from ._gemini_test_helpers import (
    _gemini_part,
    _gemini_response,
    _make_gemini_provider,
)

# ─── thinking_budget lever (provider_config → config.thinking_config) ──


async def test_thinking_budget_threaded_from_provider_config() -> None:
    """A provider_config ``thinking_budget`` reaches ``config.thinking_config``.

    Gemini 2.5 thinking is drawn from ``max_output_tokens``; the demo's low-
    budget Flash roles set ``thinking_budget: 0`` to keep short-budget calls
    from truncating to empty. Unset leaves the model default (no key emitted).
    """
    provider = _make_gemini_provider(provider_config={"thinking_budget": 0})
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model="gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}],
        system="", tools=[], max_tokens=64, temperature=0.0,
    )
    assert gc.call_args.kwargs["config"]["thinking_config"] == {"thinking_budget": 0}


async def test_thinking_config_absent_when_unset() -> None:
    """No provider_config ``thinking_budget`` → no ``thinking_config`` key at all
    (leave the Gemini-2.5 default, don't force a value)."""
    provider = _make_gemini_provider()
    gc = AsyncMock(return_value=_gemini_response())
    provider._client.aio.models.generate_content = gc
    await provider.create_message(
        model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}],
        system="", tools=[], max_tokens=4096, temperature=0.0,
    )
    assert "thinking_config" not in gc.call_args.kwargs["config"]


# ─── prompt-blocked (no candidates) + truncation accounting ────


async def test_no_candidates_warns_and_defaults_to_end_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prompt-blocked response (empty candidates + ``prompt_feedback``) warns
    and degrades to an empty END_TURN, not a silent empty turn."""
    provider = _make_gemini_provider()
    blocked = SimpleNamespace(
        candidates=[],
        prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
        usage_metadata=None,
    )
    provider._client.aio.models.generate_content = AsyncMock(return_value=blocked)
    with caplog.at_level(logging.WARNING):
        resp = await provider.create_message(
            model="gemini-2.5-pro", messages=[], system="", tools=[],
            max_tokens=64, temperature=0.0,
        )
    assert resp.text is None
    assert resp.tool_calls == []
    assert resp.stop_reason == StopReason.END_TURN
    assert any(
        "no candidates" in r.message and "SAFETY" in r.message
        for r in caplog.records
    )


async def test_thoughts_tokens_counted_on_max_tokens_truncation() -> None:
    """When thinking exhausts the output budget Gemini truncates (MAX_TOKENS),
    and thoughts tokens are still billed — output = candidates + thoughts even
    on the truncated turn (the under-charge this guards is worst on truncation).
    """
    provider = _make_gemini_provider()
    resp_obj = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[]),
                finish_reason=SimpleNamespace(name="MAX_TOKENS"),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=0,
            thoughts_token_count=64,
        ),
    )
    provider._client.aio.models.generate_content = AsyncMock(return_value=resp_obj)
    resp = await provider.create_message(
        model="gemini-2.5-flash", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.stop_reason == StopReason.MAX_TOKENS
    assert resp.text is None
    # 0 visible + 64 thinking — the truncated turn still cost 64 output tokens.
    assert resp.usage.output_tokens == 64


# ─── parallel same-tool correlation (append_tool_round) ────────


def test_append_tool_round_parallel_same_tool_keeps_order() -> None:
    """Two parallel calls to the *same* tool (no ids → both fall back to the
    name) still round-trip: Gemini correlates same-named ``function_response``
    parts positionally, so the model turn's two ``file_read`` calls and the user
    turn's two ``file_read`` responses stay in the same order the results arrive.
    """
    provider = _make_gemini_provider()
    response = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(id="file_read", name="file_read", input={"path": "a.py"}),
            ToolCall(id="file_read", name="file_read", input={"path": "b.py"}),
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [
        LLMToolResult(tool_call_id="file_read", content="AAA", is_error=False),
        LLMToolResult(tool_call_id="file_read", content="BBB", is_error=False),
    ]
    new_msgs = provider.append_tool_round([], response, results)
    calls = [p["function_call"] for p in new_msgs[0]["parts"] if "function_call" in p]
    assert [c["args"]["path"] for c in calls] == ["a.py", "b.py"]
    responses = [p["function_response"] for p in new_msgs[1]["parts"]]
    assert [r["name"] for r in responses] == ["file_read", "file_read"]
    # Positional correlation preserved: 1st response is AAA, 2nd is BBB.
    assert [r["response"]["output"] for r in responses] == ["AAA", "BBB"]


# ─── thought_signature round-trip (Gemini 3.x multi-turn tools) ────


async def test_tool_call_captures_thought_signature() -> None:
    """Gemini 3.x emits a Part-level ``thought_signature`` on the tool-call part;
    _normalize carries it onto the ToolCall so append_tool_round can replay it
    (the API 400s the next turn if it is dropped)."""
    provider = _make_gemini_provider()
    fc = SimpleNamespace(id="fc_1", name="recall_notes", args={"q": "x"})
    part = _gemini_part(function_call=fc, thought_signature=b"sig-abc")
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(parts=[part])
    )
    resp = await provider.create_message(
        model="gemini-3.5-flash", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.tool_calls[0].signature == b"sig-abc"


async def test_tool_call_signature_none_when_absent() -> None:
    """No thought_signature (2.x / non-thinking) leaves ToolCall.signature None."""
    provider = _make_gemini_provider()
    fc = SimpleNamespace(id="fc_1", name="file_read", args={"path": "x.py"})
    provider._client.aio.models.generate_content = AsyncMock(
        return_value=_gemini_response(parts=[_gemini_part(function_call=fc)])
    )
    resp = await provider.create_message(
        model="gemini-2.5-pro", messages=[], system="", tools=[],
        max_tokens=64, temperature=0.0,
    )
    assert resp.tool_calls[0].signature is None


def test_append_tool_round_replays_thought_signature() -> None:
    """A ToolCall carrying a Gemini 3.x signature replays it on the SAME model
    part the function call rides — the API requires it verbatim or 400s."""
    provider = _make_gemini_provider()
    response = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(id="t1", name="recall_notes", input={"q": "x"}, signature=b"sig-1")
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="t1", content="notes", is_error=False)]
    new_msgs = provider.append_tool_round([], response, results)

    fc_parts = [p for p in new_msgs[0]["parts"] if "function_call" in p]
    assert fc_parts == [
        {
            "function_call": {"name": "recall_notes", "args": {"q": "x"}, "id": "t1"},
            "thought_signature": b"sig-1",
        }
    ]


def test_append_tool_round_omits_signature_when_none() -> None:
    """No signature (2.x / non-thinking) → no ``thought_signature`` key, so the
    2.x wire shape is unchanged."""
    provider = _make_gemini_provider()
    response = LLMResponse(
        text=None,
        tool_calls=[ToolCall(id="t1", name="file_read", input={"path": "x.py"})],
        stop_reason=StopReason.TOOL_USE,
    )
    results = [LLMToolResult(tool_call_id="t1", content="data", is_error=False)]
    new_msgs = provider.append_tool_round([], response, results)

    fc_part = next(p for p in new_msgs[0]["parts"] if "function_call" in p)
    assert "thought_signature" not in fc_part
