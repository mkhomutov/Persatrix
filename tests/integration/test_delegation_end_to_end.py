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

from ._delegation_helpers import (
    MalformedSubAgent as _MalformedSubAgent,
)
from ._delegation_helpers import (
    ScriptedSubAgent as _ScriptedSubAgent,
)


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
    """Same trust-boundary rationale as ``context_package`` — see the
    S5 finding in the PR #222 deep review.

    PR #224 (RFC 0008 PR 3a) — N7: the per-field check on
    ``output_schema`` was collapsed into a single whole-payload
    cap (``output_schema`` is already a constituent of the
    serialised request).  The error message now references the
    request payload rather than the field, but the behaviour
    — oversize requests rejected at the trust boundary — is
    preserved."""
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
    with pytest.raises(DelegationContractError, match="payload exceeds"):
        await spawner.dispatch(child, req)


# ─── PR #224 (RFC 0008 PR 3a) — S1: output_schema enforcement ──


@pytest.mark.asyncio
async def test_output_schema_enforced_against_artifacts(
    parent_facade: MemoryFacade,
) -> None:
    """S1: ``output_schema`` is no longer advisory.  The spawner runs
    Draft-7 validation against ``DelegationResult.artifacts`` *before*
    the merge engine, so a sub-agent that returns the wrong artifact
    shape surfaces as :class:`DelegationFailure` at the trust boundary
    rather than at the next consumer."""
    canned = DelegationResult(
        summary="wrong shape",
        status="completed",
        artifacts={"score": "not-a-number"},
    )
    child = _ScriptedSubAgent("typo-bot", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(
        objective="produce numeric score",
        output_schema={
            "type": "object",
            "properties": {"score": {"type": "number"}},
            "required": ["score"],
        },
    )
    with pytest.raises(DelegationFailure, match="output_schema"):
        await spawner.dispatch(child, req)


@pytest.mark.asyncio
async def test_output_schema_pass_persists_admitted(
    parent_facade: MemoryFacade,
) -> None:
    """S1 happy path: artifacts that conform to ``output_schema`` flow
    through the merge engine and persist normally."""
    canned = DelegationResult(
        summary="ok",
        status="completed",
        artifacts={"score": 7},
        memory_writes=(
            MemoryWriteEntry(
                tier="episodic", key="ok", content="scored 7",
            ),
        ),
    )
    child = _ScriptedSubAgent("good-bot", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(
        objective="score it",
        output_schema={
            "type": "object",
            "properties": {"score": {"type": "number"}},
            "required": ["score"],
        },
    )
    spawn = await spawner.dispatch(child, req)
    assert len(spawn.outcome.admitted) == 1


@pytest.mark.asyncio
async def test_malformed_output_schema_rejected(
    parent_facade: MemoryFacade,
) -> None:
    """S1 caller-bug path: a malformed ``output_schema`` (does not pass
    the Draft-7 meta-schema) surfaces as :class:`DelegationFailure` so
    callers cannot silently ship broken schemas."""
    canned = DelegationResult(summary="ok", status="completed")
    child = _ScriptedSubAgent("ok-bot", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(
        objective="x",
        output_schema={"type": "not-a-real-type"},
    )
    with pytest.raises(DelegationFailure, match="Draft-7"):
        await spawner.dispatch(child, req)


# ─── PR #224 (RFC 0008 PR 3a) — N5: rollback on partial failure ─


@pytest.mark.asyncio
async def test_partial_persist_failure_rolls_back_admitted(
    parent_facade: MemoryFacade,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N5: when ``store_observation`` fails part-way through a batch,
    :class:`FacadeBoundSpawner` deletes every successfully-persisted
    entry from the same batch (best-effort, reverse order) so the
    parent's memory does not end up with a partial batch.  The original
    exception is re-raised so callers see the real cause.

    PR #224 review round-5 (Should #2): switched the ``flaky_store``
    swap from manual attribute assignment + explicit restore to the
    ``monkeypatch`` fixture.  Always-runs teardown restores the
    original ``store_observation`` even if the post-``raises``
    assertion fails before the manual restore line, eliminating the
    silent-leak hazard the previous pattern carried.  Restore of
    ``parent_facade.store_observation`` for the post-failure
    ``retrieve_relevant`` call below is performed explicitly via
    ``monkeypatch.undo()`` so the assertion runs against the real
    facade method.
    """
    # Wrap the facade so the third store_observation call fails.
    real_store = parent_facade.store_observation
    call_count = {"n": 0}

    async def flaky_store(*args: Any, **kwargs: Any) -> str:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated store failure")
        return await real_store(*args, **kwargs)

    monkeypatch.setattr(parent_facade, "store_observation", flaky_store)

    canned = DelegationResult(
        summary="batch",
        status="completed",
        memory_writes=tuple(
            MemoryWriteEntry(tier="episodic", key=f"k{i}", content=f"c{i}")
            for i in range(5)
        ),
    )
    child = _ScriptedSubAgent("batcher", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="batch persist")

    with pytest.raises(RuntimeError, match="simulated store failure"):
        await spawner.dispatch(child, req)

    # The two entries that landed before the failure must have been
    # rolled back; retrieve_relevant should not see them.  Undo the
    # patch first so retrieval runs against the real facade method
    # (the previous ``= real_store`` assignment did the same thing
    # manually; ``monkeypatch.undo()`` is the fixture-managed
    # equivalent and runs even if the assertion below raises).
    monkeypatch.undo()
    hits = await parent_facade.retrieve_relevant(query="c0", limit=10)
    assert all("c0" not in h.content for h in hits), (
        "rollback should have removed partially-persisted entries"
    )


# PR #224 review (Should #3): the two N5 coverage gaps (rollback-
# during-rollback and missing-episodic-accessor) live in
# ``test_delegation_rollback_edges.py``.  Split out so this end-to-end
# file stays under the 500-line review-friendliness limit enforced by
# ``scripts/checks/file_size.py``.
