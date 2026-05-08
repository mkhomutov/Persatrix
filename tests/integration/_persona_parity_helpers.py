"""Shared helpers for the RFC 0020 PR 2 single-turn parity tests.

Extracted from ``test_interaction_single_turn_parity.py`` so that file
plus the slice-4 follow-ups suite (``test_interaction_single_turn_parity_followups.py``,
PR-2 review #9 + #10 coverage) can share the persona config, mock LLM
client, episode probe, and metric-counter probe without either file
blowing through the 500-line cap enforced by
``scripts/checks/file_size.py --strict``.

The leading underscore prevents pytest from collecting this module as
a test file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent

PERSONA_CONFIG: dict = {
    "id": "parity-persona",
    "model": "test-model",
    "role": "Parity test persona",
    "type": "persona",
    "max_llm_calls": 5,
    "max_tokens": 1024,
    "tools": [],
    "persona": {
        "name": "Parity Agent",
        "background": "A persona used by the RFC 0020 PR 2 parity test.",
        "behavior": {
            "directness": "balanced",
            "formality": "professional",
            "risk_tolerance": "moderate",
        },
    },
    "autonomy": {
        "level": "semi-autonomous",
        "tick_interval_seconds": 1,
        "max_actions_per_tick": 3,
        "idle_after_ticks": 5,
    },
    "memory": {
        "db_path": ":memory:",
        "working": {"max_tokens": 50000},
    },
    "relationships": [],
}


def do_nothing_client() -> LLMClient:
    """Mock client whose every reply parses to a single DO_NOTHING action.

    Single-turn parity does not depend on action shape — only on episode
    count and column population — so the cheapest possible response is
    used to keep the test deterministic.
    """
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def make_agent() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=PERSONA_CONFIG["id"],
        config=PERSONA_CONFIG,
        llm_client=do_nothing_client(),
    )
    await agent.initialize_memory()
    return agent


def counter_total(reader: Any, name: str) -> int:
    """Sum every data point of an OTel counter exported through ``reader``.

    Mirrors ``test_interaction_tracker.TestMetricEmission._counter_total``;
    duplicated here rather than imported because the unit-test module
    binds the helper as a static method on its assertion class.
    """
    data = reader.get_metrics_data()
    if data is None:
        return 0
    total = 0
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        total += point.value
    return total


async def all_episodes(agent: _LLMPersonaAgent) -> list[dict]:
    """Read every episode row directly so we can assert on the new
    interaction columns without going through the recall scorer (which
    filters NULL-summary rows and applies the §I boost).
    """
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        """
        SELECT summary, interaction_id, started_at, closed_at,
               turn_count, scope
        FROM episodes
        WHERE agent_id = ?
        ORDER BY created_at
        """,
        (agent.agent_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "summary": r[0],
            "interaction_id": r[1],
            "started_at": r[2],
            "closed_at": r[3],
            "turn_count": r[4],
            "scope": r[5],
        }
        for r in rows
    ]
