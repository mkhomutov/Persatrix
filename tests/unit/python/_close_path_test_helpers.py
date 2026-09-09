"""Shared fixtures and helpers for the close-path tenancy suites.

Extracted so ``test_close_path_principal_binding.py`` (ISSUE-0123 R-1 —
the ambient binding) and ``test_explicit_record_principal.py``
(ISSUE-0137 — the explicit argument) describe ONE record shape and ONE
persist call instead of two independently-worded copies.  Both files
assert on the same column of the same write, so a drift between their
helpers would leave one of them green and stale — the record gaining a
field that short-circuits derivation (``replay_attributed`` is the live
precedent) would make the stale copy's pins vacuous rather than red.

The pattern mirrors ``_catchup_test_helpers.py`` — a private module
sibling to the test files, imported by name.  Pytest discovers the
fixtures because the test files re-export them (``from
._close_path_test_helpers import no_phase_two``).
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.interactions import Interaction, Turn
from agents.persona_runtime import close_path

__all__ = [
    "CLOSER",
    "OWNER",
    "closed_record",
    "episode_principal",
    "no_phase_two",
    "noop",
    "persist",
    "wrapper_neutralised",
]

#: The tenant whose request happens to be running when the close fires.
CLOSER = "p-bob"
#: The tenant the closing record was opened under, and the one every
#: row derived from it must land in.
OWNER = "p-alice"


def closed_record(principal: str) -> Interaction:
    """A closed record frozen under ``principal`` — what a room-wide fan
    or an ``idle_check`` sweep hands the close path from a DIFFERENT
    tenant's request scope."""
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


async def noop() -> None:
    return None


async def persist(
    memory: Any,
    interaction: Interaction,
    *,
    memory_ns: Any = None,
    pending_tasks: set[Any] | None = None,
) -> None:
    """Drive the real Phase-1 close.

    ``memory_ns`` defaults to a ``MagicMock`` because most callers stub
    Phase 2 out; the facts pins pass a real namespace so the background
    task writes through the live dispatcher.  ``pending_tasks`` is the
    caller's set when it means to await the Phase-2 task.
    """
    await close_path.persist_closed_interaction(
        episodic=memory,
        llm_client=MagicMock(),
        memory_ns=MagicMock() if memory_ns is None else memory_ns,
        agent_id="test-agent",
        interaction=interaction,
        pending_tasks=set() if pending_tasks is None else pending_tasks,
        on_finalized=noop,
    )


async def episode_principal(memory: Any, interaction_id: str) -> str:
    db = memory._ensure_db()
    async with db.execute(
        "SELECT principal_id FROM episodes WHERE interaction_id = ?",
        (interaction_id,),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the close path wrote no episode"
    return str(row[0])


@pytest.fixture
def no_phase_two(monkeypatch):
    """Stub Phase 2 so a pin exercises the Phase-1 write only."""
    async def _skip(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(close_path, "finalize_closed_interaction", _skip)


@pytest.fixture
def wrapper_neutralised(monkeypatch):
    """Remove the ambient binding the tenancy invariant used to rest on.

    Before ISSUE-0137, deleting ``record_write_scopes`` changed no
    signature and broke no type — every close-derived row silently
    acquired the closer's tenant.  With the principal travelling by
    argument, the tiers that TAKE that argument must land correctly with
    the wrapper gone.

    Deliberately scoped to the PRINCIPAL claim, and only for the two
    tiers that have the parameter: the epoch half still rides the
    wrapper, and so does the relationship tier, so a test using this
    fixture must assert tenancy on ``episodes`` / ``facts`` only.
    """
    monkeypatch.setattr(
        close_path, "record_write_scopes",
        lambda interaction: contextlib.nullcontext(),
    )
