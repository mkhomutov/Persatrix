"""Shared fixtures for the RFC 0051 structured-verdict tests (v0.3.10).

The reasoning tests split across two files to stay under the 500-line review
cap (the same discipline that split ``salience_deliberation`` out of
``salience_bid``): ``test_salience_bid_reasoning.py`` pins the grammar/verdict
half, ``test_salience_bid_reasoning_dispatch.py`` the prompt/budget/mode
dispatch half. Both drive the bid through the same mock ``fast`` alias and the
same in-round transcript, so that scaffold lives here once.

Importable as ``_salience_reasoning_helpers`` because ``tests/conftest.py`` puts
``tests/`` on ``sys.path`` (cf. ``_otel_test_helpers``)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from agents.llm_client import LLMClient, LLMResponse
from agents.model_aliases import use_alias_map
from agents.salience_bid import SalienceDecision, evaluate_salience

_FAST_ALIAS_MAP: dict[str, dict[str, Any]] = {
    "fast": {
        "provider": "mock",
        "model": "mock-fast",
        "input_per_1m_tokens": 0.0,
        "output_per_1m_tokens": 0.0,
    },
}

_TRANSCRIPT: list[dict[str, Any]] = [
    {"role": "user", "content": "[iron-fox]: We should pick a database for the cache."},
    {"role": "assistant", "content": "Redis is the obvious fit for a cache layer."},
]


def _client(text: str | None = None, *, raises: Exception | None = None) -> LLMClient:
    provider = AsyncMock()
    if raises is not None:
        provider.create_message = AsyncMock(side_effect=raises)
    else:
        provider.create_message = AsyncMock(return_value=LLMResponse(text=text))
    return LLMClient(provider)


async def _bid(
    *,
    client: LLMClient,
    mode: str,
    content: str = "What database should we use for the cache?",
    threshold: float | None = 0.4,
) -> SalienceDecision:
    with use_alias_map(_FAST_ALIAS_MAP):
        return await evaluate_salience(
            llm_client=client,
            content=content,
            transcript=_TRANSCRIPT,
            agent_id="ember-owl",
            persona_name="Ember Owl",
            persona_role="VP of Engineering",
            threshold=threshold,
            mode=mode,
        )
