"""Which Claude models the Anthropic adapter sends ``temperature`` to.

Claude models from Opus 4.7 on reject ``temperature`` with an HTTP 400, so a
request that carries it fails before the model runs. The adapter sends it only
to the older model families that accept it. EXP-001's arms run on
``claude-sonnet-4-6`` with a pre-registered temperature, so that model must
keep receiving it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.llm_client import AnthropicProvider


def _provider(create: AsyncMock) -> AnthropicProvider:
    sdk = MagicMock()
    sdk.AsyncAnthropic.return_value = AsyncMock()
    with patch.dict(sys.modules, {"anthropic": sdk}):
        provider = AnthropicProvider(api_key="test-key")
    provider._client = AsyncMock()
    provider._client.messages.create = create
    return provider


async def _sent(model: str) -> Mapping[str, Any]:
    """The keyword arguments the adapter hands the SDK for *model*."""
    create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
    )
    await _provider(create).create_message(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        system="",
        tools=[],
        max_tokens=100,
        temperature=0.2,
    )
    return create.call_args.kwargs


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-4-6",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-6",
    ],
)
async def test_older_models_receive_temperature(model: str) -> None:
    assert (await _sent(model))["temperature"] == 0.2


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5-1",
    ],
)
async def test_newer_models_receive_no_temperature(model: str) -> None:
    assert "temperature" not in await _sent(model)
