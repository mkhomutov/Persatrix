"""Which Claude models the Anthropic adapter sends ``temperature`` to.

Claude models from Opus 4.7 on reject ``temperature`` with an HTTP 400, so a
request that carries it fails before the model runs. The adapter sends it only
to the older model families that accept it. EXP-001's arms run on
``ARMS_MODEL`` with a pre-registered temperature, so that model must keep
receiving it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import llm_providers
from agents.llm_client import AnthropicProvider
from evaluators.exp001.costs import ARMS_MODEL


@pytest.fixture(autouse=True)
def _no_model_warned_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    # The adapter warns once per model per process; each test starts fresh.
    monkeypatch.setattr(llm_providers, "_warned_no_temperature", set())


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
        # One model for each entry in the adapter's prefix list, so removing
        # any entry fails a case.
        "claude-3-haiku-20240307",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-0",
        "claude-opus-4-1-20250805",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-20250514",
    ],
)
async def test_older_models_receive_temperature(model: str) -> None:
    assert (await _sent(model))["temperature"] == 0.2


async def test_exp001_arms_model_receives_temperature() -> None:
    assert (await _sent(ARMS_MODEL))["temperature"] == 0.2


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


async def test_dropped_temperature_is_logged_once_per_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    await _sent("claude-opus-5")
    await _sent("claude-opus-5")
    await _sent("claude-sonnet-5")
    await _sent("claude-sonnet-4-6")
    dropped = [r.getMessage() for r in caplog.records if "temperature" in r.getMessage()]
    assert len(dropped) == 2
    assert "claude-opus-5" in dropped[0]
    assert "claude-sonnet-5" in dropped[1]
