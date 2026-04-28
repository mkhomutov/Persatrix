"""End-to-end test for the RFC 0008 PR 3 delegation pipeline.

Covers the round-trip:

1. Caller builds a :class:`DelegationRequest`.
2. :class:`SubAgentSpawner` dispatches to a child :class:`BaseAgent`
   in-process.
3. Child returns a :class:`DelegationResult` via the synthesised
   metadata path provided by :class:`agents.task_agent.TaskAgent`.
4. :class:`MergeEngine` admits / rejects entries per the deterministic
   6-step pipeline.
5. :class:`FacadeBoundSpawner` persists admitted entries through the
   parent's :class:`agents.memory.facade.MemoryFacade` and they round-
   trip via ``retrieve_relevant``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.memory import MemoryFacade
from agents.sub_agents import (
    DELEGATION_REQUEST_KEY,
    DELEGATION_RESULT_KEY,
    BudgetEnvelope,
    DelegationFailure,
    DelegationRequest,
    DelegationResult,
    FacadeBoundSpawner,
    MemoryWriteEntry,
)
from agents.sub_agents.delegation import (
    MAX_CONTEXT_PACKAGE_BYTES,
    DelegationContractError,
)


class _ScriptedSubAgent(BaseAgent):
    """A minimal sub-agent that returns a pre-canned ``DelegationResult``.

    Bypasses the LLM loop so the integration test exercises only the
    delegation contract + merge engine + memory persistence path.
    """

    def __init__(self, agent_id: str, result: DelegationResult) -> None:
        super().__init__(agent_id=agent_id, config={})
        self._result = result

    async def handle(self, task: TaskInput) -> TaskOutput:
        # Sanity-check the request is on the wire under the reserved key.
        assert DELEGATION_REQUEST_KEY in task.context
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result=self._result.summary,
            metadata={DELEGATION_RESULT_KEY: self._result.to_json()},
        )


class _MalformedSubAgent(BaseAgent):
    """Returns a TaskOutput without the reserved metadata key."""

    def __init__(self, agent_id: str = "malformed") -> None:
        super().__init__(agent_id=agent_id, config={})

    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="raw text")


@pytest.fixture
async def parent_facade(tmp_path: Any) -> AsyncGenerator[MemoryFacade, None]:
    facade = MemoryFacade(
        agent_id="parent-coordinator", db_path=str(tmp_path / "parent.db"),
    )
    await facade.initialize()
    try:
        yield facade
    finally:
        await facade.close()


# ─── Happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_round_trip_persists_admitted_entries(
    parent_facade: MemoryFacade,
) -> None:
    canned = DelegationResult(
        summary="Reviewed module X",
        status="completed",
        artifacts={"review_score": 8},
        decisions=("ship",),
        memory_writes=(
            MemoryWriteEntry(
                tier="episodic",
                key="review-mod-x",
                content="Module X looks good; ship it.",
                importance=0.6,
                tags=("review",),
            ),
            MemoryWriteEntry(
                tier="notes",
                key="followup-mod-x",
                content="Schedule perf test next sprint.",
                importance=0.5,
                tags=("followup",),
            ),
        ),
    )
    child = _ScriptedSubAgent("reviewer-1", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(
        objective="review module X",
        budget=BudgetEnvelope(tokens=2000, max_llm_calls=5),
    )

    spawn = await spawner.dispatch(child, req)

    assert len(spawn.outcome.admitted) == 2
    assert spawn.outcome.rejected == []
    assert all(e.source_agent == "reviewer-1" for e in spawn.outcome.admitted)
    assert len(spawn.admitted_entry_ids) == 2

    # The persisted entries round-trip through retrieve_relevant.
    hits = await parent_facade.retrieve_relevant(query="module", limit=10)
    assert "Module X looks good; ship it." in {h.content for h in hits}
    hits2 = await parent_facade.retrieve_relevant(query="perf", limit=10)
    assert "Schedule perf test next sprint." in {h.content for h in hits2}

    # PR #222 deep review N3: lock the cross-PR contract that PR 5
    # (tier-discriminated persistence path) will replace.  Today both
    # ``episodic`` and ``notes`` writes route through ``store_observation``
    # tagged with ``tier:<name>``; PR 5 must preserve the per-tier label
    # so downstream tag-prefix consumers (RFC 0011 channel-scoped recall,
    # tier-based ACLs) keep working when the storage path changes.
    by_content = {h.content: h for h in [*hits, *hits2]}
    episodic_hit = by_content["Module X looks good; ship it."]
    notes_hit = by_content["Schedule perf test next sprint."]
    assert "tier:episodic" in episodic_hit.tags
    assert "tier:notes" in notes_hit.tags
    assert "source:reviewer-1" in episodic_hit.tags
    assert "source:reviewer-1" in notes_hit.tags


# ─── Trust ceiling enforced end-to-end ──────────────────────────


@pytest.mark.asyncio
async def test_trust_ceiling_downscales_persisted_importance(
    parent_facade: MemoryFacade,
) -> None:
    canned = DelegationResult(
        summary="overconfident result",
        status="completed",
        memory_writes=(
            MemoryWriteEntry(
                tier="episodic",
                key="overconfident",
                content="The world ends Tuesday.",
                importance=0.99,
            ),
        ),
    )
    child = _ScriptedSubAgent("doomsayer", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="forecast", trust_ceiling=0.5)

    spawn = await spawner.dispatch(child, req)
    assert len(spawn.outcome.admitted) == 1
    assert spawn.outcome.admitted[0].importance == pytest.approx(0.5)


# ─── Cap enforced end-to-end ───────────────────────────────────


@pytest.mark.asyncio
async def test_max_memory_writes_cap_drops_overflow(
    parent_facade: MemoryFacade,
) -> None:
    entries = tuple(
        MemoryWriteEntry(tier="episodic", key=f"k{i}", content=f"c{i}")
        for i in range(10)
    )
    canned = DelegationResult(
        summary="many notes", status="completed", memory_writes=entries,
    )
    child = _ScriptedSubAgent("verbose", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="brain dump", max_memory_writes=3)

    spawn = await spawner.dispatch(child, req)
    assert len(spawn.outcome.admitted) == 3
    assert len(spawn.outcome.rejected) == 7


# ─── Contract failure surfaces as DelegationFailure ─────────────


@pytest.mark.asyncio
async def test_missing_metadata_key_raises_delegation_failure(
    parent_facade: MemoryFacade,
) -> None:
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="anything")
    with pytest.raises(DelegationFailure, match="contract violation"):
        await spawner.dispatch(_MalformedSubAgent(), req)


@pytest.mark.asyncio
async def test_failed_sub_agent_raises_delegation_failure(
    parent_facade: MemoryFacade,
) -> None:
    class _Failing(BaseAgent):
        def __init__(self) -> None:
            super().__init__(agent_id="loser", config={})

        async def handle(self, task: TaskInput) -> TaskOutput:
            return TaskOutput(status=TaskStatus.FAILED, result="boom")

    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    with pytest.raises(DelegationFailure, match="FAILED"):
        await spawner.dispatch(_Failing(), DelegationRequest(objective="x"))


# ─── PR #222 deep review S5 — context_package size cap ─────────


@pytest.mark.asyncio
async def test_oversize_context_package_rejected_at_dispatch(
    parent_facade: MemoryFacade,
) -> None:
    """A hostile or buggy caller cannot push an arbitrarily large
    ``context_package`` into the sub-agent's ``task.context``: the
    spawner enforces :data:`MAX_CONTEXT_PACKAGE_BYTES` *before* the
    sub-agent is invoked so failure surfaces in the caller stack
    (OWASP A05). Mirrors PR 1's bounded-shape discipline on the
    existing ``_context_package`` reservation."""
    canned = DelegationResult(summary="never reached", status="completed")
    child = _ScriptedSubAgent("noop", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    # Build a payload comfortably above the 256 KiB cap.
    huge = "x" * (MAX_CONTEXT_PACKAGE_BYTES + 1024)
    req = DelegationRequest(
        objective="huge package",
        context_package={"blob": huge},
    )
    with pytest.raises(DelegationContractError, match="context_package"):
        await spawner.dispatch(child, req)


@pytest.mark.asyncio
async def test_oversize_output_schema_rejected_at_dispatch(
    parent_facade: MemoryFacade,
) -> None:
    """Same trust-boundary rationale as ``context_package`` \u2014 see the
    S5 finding in the PR #222 deep review."""
    canned = DelegationResult(summary="never reached", status="completed")
    child = _ScriptedSubAgent("noop2", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    huge = "x" * (MAX_CONTEXT_PACKAGE_BYTES + 1024)
    req = DelegationRequest(
        objective="huge schema",
        output_schema={"description": huge},
    )
    with pytest.raises(DelegationContractError, match="output_schema"):
        await spawner.dispatch(child, req)
