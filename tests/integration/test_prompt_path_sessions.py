"""Which sessions a real persona turn reads, and what gates the widening.

The L5 follow-up from the PR 451 deep review (RFC 0031 Phase 2): drive a
real ``_inject_memory_context`` turn, as ``test_cross_room_live.py``
does, over real tiers that share one database, with a spy on every tier
read.  The tier and facade session rules are pinned in
``test_session_continuity.py``, and the source scan of the prompt-path
modules in ``tests/unit/python/test_session_recall_default_path.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from agents.clock import WallClock
from agents.memory.facade import MemoryStore
from agents.memory.facts import FactStore
from agents.memory.relationship import RelationshipMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime import memory_context
from agents.persona_runtime.cross_room import (
    CROSS_ROOM_LIVE,
    CROSS_ROOM_OFF,
    CROSS_ROOM_SHADOW,
    DEFAULT_EPISODIC_CROSS_ROOM,
    DEFAULT_FACTS_CROSS_ROOM,
)
from agents.persona_types import AgentEvent, EventType


class _Tiers(NamedTuple):
    """One session's tiers on the shared database."""

    facade: MemoryStore  # its EpisodicMemory serves episodes, channel history, notes
    rels: RelationshipMemory
    facts: FactStore


_OpenSession = Callable[[str], Awaitable[_Tiers]]


@pytest.fixture
async def open_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_OpenSession]:
    """Open a session's tiers under a named ``PERSATRIX_SESSION_ID``,
    which each tier snapshots when it is built."""
    db = str(tmp_path / "shared.db")
    opened: list[_Tiers] = []

    async def _open(session_id: str) -> _Tiers:
        monkeypatch.setenv("PERSATRIX_SESSION_ID", session_id)
        tiers = _Tiers(
            MemoryStore(agent_id="ember-owl", db_path=db),
            RelationshipMemory(agent_id="ember-owl", db_path=db),
            FactStore(agent_id="ember-owl", db_path=db),
        )
        await tiers.facade.initialize()
        await tiers.rels.initialize()
        await tiers.facts.initialize()
        opened.append(tiers)
        return tiers

    yield _open
    for tiers in opened:
        await tiers.facade.close()
        await tiers.rels.close()
        await tiers.facts.close()


#: Every tier read one turn makes, as (tier, method).
_TIER_READS = (
    ("facts", "recall"),
    ("facts", "topic_subjects"),
    ("episodic", "recall"),
    ("episodic", "recall_notes"),
    ("rels", "get_relationship_summary"),
)

#: One recorded read: (``tier.method``, the ``sessions`` it passed, whether
#: a shadow pass made it, the rows it returned).
_Read = tuple[str, object, bool, Any]
_AsyncFn = Callable[..., Awaitable[Any]]


def _spy_tier_reads(monkeypatch: pytest.MonkeyPatch, tiers: _Tiers) -> list[_Read]:
    """Record every read one turn makes on *tiers*, marking the ones made
    inside a shadow pass, whose only output is a log record."""
    reads: list[_Read] = []
    in_shadow: list[bool] = []
    targets = {"facts": tiers.facts, "episodic": tiers.facade._episodic, "rels": tiers.rels}

    def recorded(read: str, orig: _AsyncFn) -> _AsyncFn:
        async def spy(*args: Any, **kwargs: Any) -> Any:
            sessions = kwargs.get("sessions")
            shadow = bool(in_shadow)
            rows = await orig(*args, **kwargs)
            # A list would be unhashable in the sets the test builds.
            if isinstance(sessions, list):
                sessions = tuple(sessions)
            reads.append((read, sessions, shadow, rows))
            return rows

        return spy

    def marked(orig: _AsyncFn) -> _AsyncFn:
        async def shadow_pass(*args: Any, **kwargs: Any) -> None:
            in_shadow.append(True)
            try:
                await orig(*args, **kwargs)
            finally:
                in_shadow.pop()

        return shadow_pass

    for tier, name in _TIER_READS:
        target = targets[tier]
        monkeypatch.setattr(target, name, recorded(f"{tier}.{name}", getattr(target, name)))
    for name in ("emit_facts_shadow", "emit_episodes_shadow"):
        monkeypatch.setattr(memory_context, name, marked(getattr(memory_context, name)))
    return reads


def _prompt_host(tiers: _Tiers, cross_room: str) -> memory_context._MemoryContextMixin:
    """The persona memory mixin over *tiers*, built the way
    ``test_cross_room_live.py`` builds it."""

    class _Host(memory_context._MemoryContextMixin):
        def _format_event(self, event: AgentEvent) -> str:
            return str(event.payload.get("content", ""))

    host = _Host()
    host.agent_id = "ember-owl"
    host._working_memory = WorkingMemory(max_tokens=8192)
    host._episodic_memory = tiers.facade._episodic
    host._relationship_memory = tiers.rels
    host._fact_store = tiers.facts
    host._clock = WallClock()
    host._timezone = "UTC"
    host._facts_cross_room = host._episodic_cross_room = cross_room
    return host


class TestInjectMemoryContextDefaultPath:
    """Since RFC 0049 PR 4 the default ``cross_room: live`` makes the
    facts recall pass ``sessions="*"``, so F-3 is "no ungated widening":
    on the reads that feed the prompt, ``"*"`` reaches only the facts
    tier, only under ``live``, and only through the gate.  One turn in
    session ``arc-2``, rows in ``arc-1``:

    * Only the facts reads (``FactStore.recall`` and
      ``FactStore.topic_subjects``) receive ``"*"``: the prompt's under
      ``live``, and under ``shadow`` only the log-only shadow pass's.
    * Every other read passes ``sessions=None``, the §D default, so the
      other session's note never reaches the prompt in any mode.
    * A widened fact reaches the prompt only through the RFC 0037 §D
      gate: the ``"*"`` read returns the ``restricted`` fact, and the
      gate withholds it from an ``internal`` turn.
    * ``off`` is the negative control: the same spies see no ``"*"``.

    The live episodic tier reads across sessions without ``"*"``:
    ``recall_room_ranked`` gives its query helpers ``sessions=None`` and a
    same-room boost, so the other session's episode arrives unseen by the
    spies.  The relationship identity read is cross-room by design (the
    F-7 amendment) and takes no ``sessions``.
    """

    @pytest.mark.parametrize("mode", [CROSS_ROOM_OFF, CROSS_ROOM_SHADOW, CROSS_ROOM_LIVE])
    async def test_star_reaches_only_the_gated_facts_read(
        self, open_session: _OpenSession, monkeypatch: pytest.MonkeyPatch, mode: str,
    ) -> None:
        # ``live`` is the shipped default, so its case is the default path.
        assert DEFAULT_FACTS_CROSS_ROOM == DEFAULT_EPISODIC_CROSS_ROOM == CROSS_ROOM_LIVE
        other = await open_session("arc-1")
        await other.facade.store_observation("met alice at the lake")
        await other.facade._episodic.store_note(
            "alice", "alice hides the lake boathouse key", session_id="arc-1",
        )
        await other.facts.store(
            subject="alice", predicate="works_at", object="lakeshore",
            source_interaction_id="ix-1", asserted_at=1000.0, session_id="arc-1",
        )
        vault = await other.facts.store(
            subject="alice", predicate="knows", object="vault-code",
            source_interaction_id="ix-2", asserted_at=1000.0, session_id="arc-1",
            protection_level="restricted",
        )
        here = await open_session("arc-2")
        await here.facade._episodic.store_note(
            "alice", "alice guides lake tours", session_id="arc-2",
        )
        reads = _spy_tier_reads(monkeypatch, here)
        host = _prompt_host(here, mode)

        result = await host._inject_memory_context(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "alice lake"},
            channel_id="group:lake",
            sender_id="alice",
            metadata={"channel_classification": "internal"},
        ))

        # Every tier read ran, so the absences below are not vacuous.
        assert {read for read, *_ in reads} == {f"{t}.{m}" for t, m in _TIER_READS}
        # "*" is the only width any read passes, and only the facts reads
        # pass it: the prompt's under live, the shadow pass's under shadow.
        assert {sessions for _, sessions, _, _ in reads} <= {None, "*"}
        facts_reads = ("facts.recall", "facts.topic_subjects")
        assert {(read, shadow) for read, sessions, shadow, _ in reads if sessions == "*"} == {
            CROSS_ROOM_OFF: set(),
            CROSS_ROOM_SHADOW: {(read, True) for read in facts_reads},
            CROSS_ROOM_LIVE: {(read, False) for read in facts_reads},
        }[mode]

        rendered = "\n".join(s.content for s in host._working_memory._sections)
        manifest = {e.entry_id for e in result.manifest}
        live = mode == CROSS_ROOM_LIVE
        # The other session's fact and episode reach the prompt under live only ...
        assert ("lakeshore" in rendered) is live
        assert ("met alice at the lake" in rendered) is live
        # ... and only through the gate: the "*" read returned the
        # restricted fact, and the gate kept it out of prompt and manifest.
        star_rows = {
            fact.fact_id for read, sessions, _, rows in reads
            if read == "facts.recall" and sessions == "*" for fact in rows
        }
        assert (vault in star_rows) is (mode != CROSS_ROOM_OFF)
        assert vault not in manifest
        assert "vault-code" not in rendered
        # Notes stay session-scoped in every mode.
        assert "guides lake tours" in rendered
        assert "boathouse" not in rendered
