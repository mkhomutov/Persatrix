"""One session's persona-memory tiers on a shared database.

Lifted from ``test_session_continuity.py`` when
``test_prompt_path_sessions.py`` was split out of it, so both files open
their sessions the same way instead of each keeping a copy.  The fixture
is re-exported through ``conftest.py``: importing it by name into a test
file makes ruff read every fixture parameter as a redefinition (F811).

The leading underscore prevents pytest from collecting this module as
a test file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from agents.memory.facade import MemoryStore
from agents.memory.facts import FactStore
from agents.memory.notes import NoteStore
from agents.memory.relationship import RelationshipMemory


class SessionTiers:
    """Construction-time snapshot of every persona-memory tier for one
    operator session.  Mirrors how :class:`agents.base.BaseAgent` /
    persona-runtime ``initialize_memory`` wire the tiers under a single
    resolved ``PERSATRIX_SESSION_ID``.
    """

    def __init__(
        self,
        facade: MemoryStore,
        rels: RelationshipMemory,
        facts: FactStore,
        notes: NoteStore,
    ) -> None:
        self.facade = facade
        self.rels = rels
        self.facts = facts
        self.notes = notes


#: ``facade_factory(session_id, agent_id="ember-owl")``.
FacadeFactory = Callable[..., Awaitable[SessionTiers]]


@pytest.fixture
async def facade_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FacadeFactory]:
    """Build a :class:`SessionTiers` keyed to a named ``PERSATRIX_SESSION_ID``.

    Every tier shares the same ``db_path`` — the cross-run state-bleed
    surface these tests exist to close.  The env-var snapshot is
    captured at construction; a subsequent ``_build`` call overrides
    the env var but already-built bundles keep their snapshot.

    The facade (:class:`MemoryStore`) is exercised for the
    :meth:`retrieve_relevant` / :meth:`store_observation` surface only;
    relationships / facts / notes are constructed alongside it because
    the RFC 0029 facade does not expose those tiers — persona-runtime
    ``initialize_memory`` wires them through the agent harness, but
    the recall semantics under test are tier-level and the parallel
    construction is the lightest fixture that exercises them.
    """
    db_path = str(tmp_path / "shared.db")

    async with AsyncExitStack() as stack:

        async def _build(session_id: str, agent_id: str = "ember-owl") -> SessionTiers:
            monkeypatch.setenv("PERSATRIX_SESSION_ID", session_id)
            fac = MemoryStore(agent_id=agent_id, db_path=db_path)
            rels = RelationshipMemory(agent_id=agent_id, db_path=db_path)
            facts = FactStore(agent_id=agent_id, db_path=db_path)
            for tier in (fac, rels, facts):
                # Registered before ``initialize()``, so a tier that fails
                # to open never strands the ones already open; ``close()``
                # on a tier that never opened does nothing.
                stack.push_async_callback(tier.close)
                await tier.initialize()
            # The notes tier rides on EpisodicMemory's connection; reuse
            # the facade's underlying tier rather than building a parallel
            # NoteStore that would race on the shared DB file.
            notes = fac._episodic._note_store
            assert notes is not None
            return SessionTiers(fac, rels, facts, notes)

        yield _build
