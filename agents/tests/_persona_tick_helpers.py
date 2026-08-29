"""Shared fixtures + helpers for the persona TICK test files.

``test_persona_tick_shortcircuit.py`` reached the 500-line review-friendly
cap, so the DB-failure contract split out into
``test_persona_tick_db_failure.py``.  These helpers live in this
underscore-prefixed module (not a test file) so the two import one
canonical source instead of duplicating the config + builder pair — the
same arrangement ``_scheduled_wakes_wiring_helpers.py`` uses.

A pure extraction: the definitions below are unchanged from the file they
came out of.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult

__all__ = [
    "DO_NOTHING_RESPONSE",
    "PERSONA_CONFIG",
    "SUBSTANTIVE_RESPONSE",
    "make_agent",
    "make_client",
    "nonzero_injection",
    "zero_injection",
]


PERSONA_CONFIG: dict[str, Any] = {
    "id": "test-agent",
    "type": "persona",
    "name": "Test Agent",
    "role": "Testing",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 10,
    "max_tokens": 4096,
    "persona": {
        "background": "Test background.",
        "behavior": {},
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}

DO_NOTHING_RESPONSE = LLMResponse(
    text='[{"action_type": "do_nothing", "payload": {}}]',
    stop_reason=StopReason.END_TURN,
    usage=Usage(input_tokens=10, output_tokens=5),
)

SUBSTANTIVE_RESPONSE = LLMResponse(
    text='[{"action_type": "complete_task", "payload": {"result": "done"}}]',
    stop_reason=StopReason.END_TURN,
    usage=Usage(input_tokens=10, output_tokens=10),
)


def make_client(response: LLMResponse = SUBSTANTIVE_RESPONSE) -> LLMClient:
    """Return a mock LLMClient that returns the given response."""
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(return_value=response)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(return_value=[])
    return LLMClient(mock_provider)


async def make_agent(
    *,
    goal_progress: dict[str, float] | None = None,
    recent_context: list[str] | None = None,
    client: LLMClient | None = None,
) -> _LLMPersonaAgent:
    """Create an initialized _LLMPersonaAgent with optional state preset."""
    agent = create_persona_agent(
        agent_id="test-agent",
        # Deep-copy so per-test mutations of nested dicts (``persona``,
        # ``permissions``, ``memory``) cannot leak across tests via the
        # shared module-level PERSONA_CONFIG reference.
        config=copy.deepcopy(PERSONA_CONFIG),
        llm_client=client or make_client(),
    )
    await agent.initialize_memory()
    if goal_progress is not None:
        agent._state.goal_progress = goal_progress
    if recent_context is not None:
        agent._state.recent_context = recent_context
    return agent


def zero_injection() -> MemoryInjectionResult:
    """Return a MemoryInjectionResult with zero admitted tokens."""
    return MemoryInjectionResult(memory_admitted_tokens=0)


def nonzero_injection(tokens: int = 200) -> MemoryInjectionResult:
    """Return a MemoryInjectionResult with non-zero admitted tokens."""
    return MemoryInjectionResult(memory_admitted_tokens=tokens)
