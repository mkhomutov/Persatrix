"""Edge-case coverage for the FacadeBoundSpawner rollback path.

Split out from ``test_delegation_end_to_end.py`` during PR #224 review
fix-up: the parent file hit the 500-line review-friendliness limit
(see ``scripts/checks/file_size.py``) when the two N5 coverage tests
landed.  These tests are conceptually distinct from the round-trip
end-to-end suite — they assert *failure-mode* behaviour of the
rollback compensator added in RFC 0008 PR 3a — so a dedicated file
keeps both suites focused.

PR #224 review (Should #3) closes the two N5 gaps:
    (a) rollback-during-rollback must not mask the original cause;
    (b) a facade variant without an ``episodic`` accessor must
        degrade to a warning rather than crashing dispatch.
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
    DelegationRequest,
    DelegationResult,
    FacadeBoundSpawner,
    MemoryWriteEntry,
)


class _ScriptedSubAgent(BaseAgent):
    """Pre-canned sub-agent — duplicated from the end-to-end suite to
    keep this file self-contained (the original is module-private)."""

    def __init__(self, agent_id: str, result: DelegationResult) -> None:
        super().__init__(agent_id=agent_id, config={})
        self._result = result

    async def handle(self, task: TaskInput) -> TaskOutput:
        assert DELEGATION_REQUEST_KEY in task.context
        return TaskOutput(
            status=TaskStatus.COMPLETED,
            result=self._result.summary,
            metadata={DELEGATION_RESULT_KEY: self._result.to_json()},
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


@pytest.mark.asyncio
async def test_rollback_failure_does_not_mask_original_cause(
    parent_facade: MemoryFacade,
) -> None:
    """N5 coverage: when ``delete_episode`` itself raises during
    rollback, the original ``store_observation`` failure must still be
    the surfaced exception (rollback errors are logged, not re-raised).
    """
    real_store = parent_facade.store_observation
    call_count = {"n": 0}

    async def flaky_store(*args: Any, **kwargs: Any) -> str:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated store failure")
        return await real_store(*args, **kwargs)

    parent_facade.store_observation = flaky_store  # type: ignore[method-assign]

    # Force every rollback delete to raise.
    async def boom_delete(_entry_id: str) -> bool:
        raise RuntimeError("simulated delete_episode failure")

    parent_facade.episodic.delete_episode = boom_delete  # type: ignore[method-assign]

    canned = DelegationResult(
        summary="batch",
        status="completed",
        memory_writes=tuple(
            MemoryWriteEntry(tier="episodic", key=f"k{i}", content=f"c{i}")
            for i in range(5)
        ),
    )
    child = _ScriptedSubAgent("batcher-rb-fail", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=parent_facade,
    )
    req = DelegationRequest(objective="batch persist (rollback fails)")

    # The original store failure must still be the surfaced exception —
    # the delete_episode failure is intentionally swallowed.
    with pytest.raises(RuntimeError, match="simulated store failure"):
        await spawner.dispatch(child, req)


@pytest.mark.asyncio
async def test_rollback_skipped_when_facade_lacks_episodic_accessor() -> None:
    """N5 coverage: a facade variant without an ``episodic`` accessor
    must NOT crash the dispatch path.  The spawner logs a warning and
    re-raises the original cause (no rollback attempted).
    """

    class _NoEpisodicFacade:
        """Minimal facade stub exposing only ``store_observation``."""

        def __init__(self) -> None:
            self.calls = 0

        async def store_observation(
            self, content: str, **_kwargs: Any,
        ) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated store failure")
            return f"stub-id-{self.calls}"

    facade = _NoEpisodicFacade()
    canned = DelegationResult(
        summary="batch",
        status="completed",
        memory_writes=(
            MemoryWriteEntry(tier="episodic", key="k0", content="c0"),
            MemoryWriteEntry(tier="episodic", key="k1", content="c1"),
        ),
    )
    child = _ScriptedSubAgent("batcher-no-episodic", canned)
    spawner = FacadeBoundSpawner(
        parent_agent_id="coordinator", memory_facade=facade,
    )
    req = DelegationRequest(objective="batch persist (no episodic)")

    with pytest.raises(RuntimeError, match="simulated store failure"):
        await spawner.dispatch(child, req)
