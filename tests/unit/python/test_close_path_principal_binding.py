"""ISSUE-0123 R-1 — the close path writes under the RECORD's tenant.

PR #846 review, open finding #1.  The ``(principal, speaker, scope)``
re-key freezes the tenant on the record at open, but every storage tier
resolves its own tenant from the AMBIENT ``principal_scope`` the persona
binds per EVENT (``resolve_active_principal`` in ``episodic`` / ``facts``
/ ``relationship``).  That agreed while a scope held ONE record, because
the record and the event were then the same tenant's.

Since the re-key a room holds one record per ``(principal, speaker)``,
and both the room-wide close fans and ``idle_check`` close OTHER tenants'
records inside whichever tenant's request scope triggered them.  So the
closer's principal was stamped on every row the close derived — and with
strict-equality recall and no carve-out (``_principal_filter``) that
INVERTS the boundary the re-key exists to draw: the speaker's own
conversation becomes invisible to the speaker and readable by whoever
closed the room.

``persist_closed_interaction`` now binds ``interaction.principal_id``
around both phases.  These pins hold three things: the persisted column,
the recall consequence, and that Phase 2 inherits the binding across the
``asyncio.create_task`` boundary (a task snapshots the context at
creation, so the block has to enclose the construction — binding Phase 1
alone would leave an episode whose derived facts live in a DIFFERENT
tenant, which is worse than the bug, since RFC 0049 Phase 1 facts are
cross-room).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.interactions import Interaction, Turn
from agents.persona_runtime import close_path
from agents.principal_id import current_principal_id, principal_scope

_CLOSER = "p-bob"
_OWNER = "p-alice"


def _closed_record(principal: str) -> Interaction:
    """A closed record frozen under ``principal`` — what a room fan hands
    the close path for a speaker who is NOT the one that closed the room."""
    return Interaction(
        interaction_id=f"i-{principal}",
        scope="group:planning",
        started_at=1_000.0,
        closed_at=1_100.0,
        close_reason=REASON_STRUCTURAL,
        principal_id=principal,
        speaker_id="alice",
        turns=[Turn(at=1_000.0, payload={"sender": "alice"})],
    )


async def _persist(memory: Any, interaction: Interaction) -> None:
    await close_path.persist_closed_interaction(
        episodic=memory,
        llm_client=MagicMock(),
        memory_ns=MagicMock(),
        agent_id="test-agent",
        interaction=interaction,
        pending_tasks=set(),
        on_finalized=_noop,
    )


async def _noop() -> None:
    return None


@pytest.fixture
def _no_phase_two(monkeypatch):
    """Stub Phase 2 so these pins exercise the Phase-1 write only."""
    async def _skip(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(close_path, "finalize_closed_interaction", _skip)


async def _principal_of(memory: Any, interaction_id: str) -> str:
    db = memory._ensure_db()
    async with db.execute(
        "SELECT principal_id FROM episodes WHERE interaction_id = ?",
        (interaction_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the close path wrote no episode"
    return str(row[0])


class TestPhaseOneTenant:
    async def test_row_carries_the_records_principal_not_the_closers(
        self, memory, _no_phase_two,
    ):
        """The fan closes alice's record inside bob's request scope."""
        record = _closed_record(_OWNER)
        with principal_scope(_CLOSER):
            await _persist(memory, record)

        assert await _principal_of(memory, record.interaction_id) == _OWNER

    async def test_owner_can_recall_it_and_the_closer_cannot(
        self, memory, _no_phase_two,
    ):
        """The consequence that makes it a boundary defect rather than a
        mislabel: the principal predicate is unconditional strict
        equality, so a row written under the wrong tenant is not merely
        mis-tagged — it is lost to its owner and exposed to the closer.

        Driven through the real recall path, which means finishing the
        two-phase write: a ``[summary pending]`` row is deliberately
        invisible to recall until Phase 2 upgrades it (that upgrade keys
        on ``agent_id`` + ``interaction_id``, so it is tenant-agnostic
        and cannot itself paper over a wrong Phase-1 tenant)."""
        record = _closed_record(_OWNER)
        with principal_scope(_CLOSER):
            await _persist(memory, record)
        await memory.update_episode_summary(
            record.interaction_id, "alice ships the ledger on Friday",
        )

        with principal_scope(_OWNER):
            assert len(await memory.recall(limit=10)) == 1
        with principal_scope(_CLOSER):
            assert await memory.recall(limit=10) == []

    async def test_single_tenant_deployment_is_unchanged(
        self, memory, _no_phase_two,
    ):
        """The frozen value was resolved through the same precedence the
        tiers use, so with no scope active it equals what the ambient
        read would have produced."""
        record = _closed_record("local")
        await _persist(memory, record)

        assert await _principal_of(memory, record.interaction_id) == "local"


class TestPhaseTwoInheritsTheBinding:
    async def test_background_task_runs_under_the_records_principal(
        self, memory, monkeypatch,
    ):
        """``asyncio.create_task`` snapshots the context at creation, so
        the binding has to enclose the task CONSTRUCTION — not just the
        Phase-1 await.  Phase 2 is where the facts and relationship rows
        are written, and they resolve the tenant the same ambient way."""
        seen: list[str | None] = []

        async def _capture(**kwargs: object) -> None:
            seen.append(current_principal_id())

        monkeypatch.setattr(close_path, "finalize_closed_interaction", _capture)
        tasks: set[Any] = set()
        with principal_scope(_CLOSER):
            await close_path.persist_closed_interaction(
                episodic=memory, llm_client=MagicMock(), memory_ns=MagicMock(),
                agent_id="test-agent", interaction=_closed_record(_OWNER),
                pending_tasks=tasks, on_finalized=_noop,
            )
            for task in list(tasks):
                await task

        assert seen == [_OWNER], (
            "Phase 2 must derive facts under the tenant Phase 1 wrote the "
            "episode for, or the two land in different tenants"
        )

    async def test_the_scope_is_restored_after_the_close(
        self, memory, _no_phase_two,
    ):
        """The binding is a block, not a latch: the caller's own request
        scope survives it, so the next record in the fan resolves its own
        principal rather than inheriting its predecessor's."""
        with principal_scope(_CLOSER):
            await _persist(memory, _closed_record(_OWNER))
            assert current_principal_id() == _CLOSER
