"""Shared fixtures for the RFC 0020 multi-turn integration suites.

Extracted from :mod:`test_interaction_multi_turn_followups` (slice 5)
so the slice-6 cap-failure suite, the slice-6 scoping suite, and any
future RFC 0020 multi-turn integration test can share the persona
config / mock LLM client / clock-aware agent factory / episode probe
without each file blowing through the 500-line cap enforced by
``scripts/checks/file_size.py --strict``.

The leading underscore prevents pytest from collecting this module as
a test file.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from agents.clock import FrozenClock
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.prompt_loader import load_snippet

__all__ = [
    "TEST_IDLE_TIMEOUT_SEC",
    "all_episodes",
    "do_nothing_client",
    "make_agent_with_clock",
    "persona_config",
]

# Short idle timeout keeps clock-driven tests cheap — production
# default is 600s (RFC 0020 §B); a 5s window means a fake-clock
# advance of 6s is unambiguously past the threshold without making
# the test wait for real wall-clock time.
TEST_IDLE_TIMEOUT_SEC: float = 5.0


def persona_config(*, agent_id: str = "multi-turn-test-persona") -> dict:
    """Return a fresh persona config dict for a multi-turn test agent.

    Each caller gets its own dict so monkey-patching one test's
    config (or its agent's ``_interaction_tracker._max_turns`` field)
    cannot leak into another test in the same pytest session.
    """
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "Multi-turn integration test persona",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": "Multi-Turn Test Agent",
            "background": "RFC 0020 multi-turn integration test persona.",
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
            "interaction_idle_timeout_sec": TEST_IDLE_TIMEOUT_SEC,
        },
        "relationships": [],
    }


def do_nothing_client() -> LLMClient:
    """Mock LLM client: persona-loop replies parse to a single
    DO_NOTHING; the close-path summariser call returns a valid combined
    ``{"summary": ..., "facts": []}`` envelope.

    Multi-turn aggregation tests don't depend on action shape — only on
    tracker state and persisted episode columns — but the summariser
    call must still receive a well-formed envelope: a non-envelope
    response makes the close path fall back to the unavailable-summary
    sentinel (ISSUE-0054), and F-6 now routes single-turn conversational
    closes through the summariser too.  The two call sites are told
    apart by ``system``: the close-path summariser is the only caller
    that passes the ``episode-summarizer`` system snippet, so routing
    on it stays correct regardless of either call's ``max_tokens``.
    """
    mock_provider = AsyncMock()
    summarizer_system = load_snippet("episode-summarizer")

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if system == summarizer_system:
            return LLMResponse(
                text=json.dumps(
                    {"summary": "Multi-turn session summary.", "facts": []},
                ),
                stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def make_agent_with_clock(
    clock: FrozenClock, *, config: dict | None = None,
) -> _LLMPersonaAgent:
    cfg = config if config is not None else persona_config()
    agent = create_persona_agent(
        agent_id=cfg["id"],
        config=cfg,
        llm_client=do_nothing_client(),
        clock=clock,
    )
    await agent.initialize_memory()
    return agent


async def all_episodes(agent: _LLMPersonaAgent) -> list[dict]:
    """Read every episode row directly so tests can assert on the new
    interaction columns without going through the recall scorer.
    """
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        """
        SELECT summary, interaction_id, started_at, closed_at,
               turn_count, scope, context_json
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
            "context_json": r[6],
        }
        for r in rows
    ]
