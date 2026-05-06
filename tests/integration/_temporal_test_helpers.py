"""Shared agent factory and fixtures for RFC 0021 temporal integration tests.

Used by:
- ``test_temporal_prompt_shape.py`` — prompt-shape and budget invariant tests
- ``test_temporal_metrics.py`` — telemetry counter accuracy tests (PR #260 M-1)
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agents.clock import FrozenClock
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent

# Pinned wall-clock instant shared by both test modules.  A weekday afternoon
# in UTC keeps the part-of-day word stable across DST-shifting host locales.
FROZEN_EPOCH = 1745591520.0  # 2025-04-25T14:32:00+00:00 — Friday afternoon

PERSONA_CONFIG: dict[str, Any] = {
    "id": "temporal-persona",
    "type": "persona",
    "name": "Temporal Test Agent",
    "role": "Verifies the RFC 0021 PR 2 prompt shape",
    "model": "test-model",
    "max_llm_calls": 1,
    "max_tokens": 512,
    "persona": {
        "title": "Tester",
        "background": "Exists only inside this test module.",
        "behavior": {
            "directness": "balanced",
            "formality": "professional",
        },
        "timezone": "UTC",
    },
    "permissions": {"memory": {"read": True, "write": True}},
    "memory": {"db_path": ":memory:"},
}


def make_client() -> LLMClient:
    provider = AsyncMock()
    provider.create_message = AsyncMock(
        return_value=LLMResponse(text="ok"),
    )
    provider.format_tool_definitions = MagicMock(return_value=[])
    provider.append_tool_round = MagicMock(side_effect=lambda msgs, *_: msgs)
    return LLMClient(provider)


async def make_agent(
    *, clock: FrozenClock | None = None, config: dict | None = None,
):
    cfg = config or deepcopy(PERSONA_CONFIG)
    agent = create_persona_agent(
        agent_id=cfg["id"],
        config=cfg,
        llm_client=make_client(),
        clock=clock or FrozenClock(FROZEN_EPOCH, tz="UTC"),
    )
    await agent.initialize_memory()
    return agent
