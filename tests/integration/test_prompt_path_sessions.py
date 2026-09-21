"""Which sessions a real persona turn reads, and what gates the widening.

The L5 follow-up from the PR 451 deep review (RFC 0031 Phase 2): drive a
real ``_inject_memory_context`` turn, as ``test_cross_room_live.py``
does, over real tiers that share one database, with a spy on every tier
read that takes a ``sessions`` width.  The tier and facade session rules
are pinned in ``test_session_continuity.py``, and the source scan of the
prompt-path modules in
``tests/unit/python/test_session_recall_default_path.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agents.clock import WallClock
from agents.memory.working import WorkingMemory
from agents.persona_runtime import memory_context
from agents.persona_runtime.cross_room import (
    CROSS_ROOM_LIVE,
    CROSS_ROOM_OFF,
    CROSS_ROOM_SHADOW,
)
from agents.persona_types import AgentEvent, EventType

from ._session_tiers_helpers import FacadeFactory, SessionTiers

#: Every tier read on the turn that takes a ``sessions`` width, as
#: (tier, method).  The live ranked episodic read and the relationship
#: identity read take none; the prompt text is what pins those.
_TIER_READS = (
    ("facts", "recall"),
    ("facts", "topic_subjects"),
    ("episodic", "recall"),
    ("episodic", "recall_notes"),
    ("rels", "get_relationship_summary"),
)

_MODES = (CROSS_ROOM_OFF, CROSS_ROOM_SHADOW, CROSS_ROOM_LIVE)

#: One recorded read: (``tier.method``, the ``sessions`` it passed, whether
#: a shadow pass made it, the rows it returned — ``None`` if it raised).
_Read = tuple[str, object, bool, Any]
_AsyncFn = Callable[..., Awaitable[Any]]


def _spy_tier_reads(monkeypatch: pytest.MonkeyPatch, tiers: SessionTiers) -> list[_Read]:
    """Record every listed read one turn makes on *tiers*, marking the
    ones made inside a shadow pass, whose only output is a log record."""
    reads: list[_Read] = []
    in_shadow: list[bool] = []
    targets = {"facts": tiers.facts, "episodic": tiers.facade._episodic, "rels": tiers.rels}

    def recorded(read: str, orig: _AsyncFn) -> _AsyncFn:
        async def spy(*args: Any, **kwargs: Any) -> Any:
            shadow = bool(in_shadow)
            rows: Any = None
            try:
                rows = await orig(*args, **kwargs)
                return rows
            finally:
                # Recorded even when the read raises: the turn logs a tier
                # failure and carries on, and the width it passed still counts.
                reads.append((read, kwargs.get("sessions"), shadow, rows))

        return spy

    def marked(orig: _AsyncFn) -> _AsyncFn:
        async def shadow_pass(*args: Any, **kwargs: Any) -> Any:
            in_shadow.append(True)
            try:
                return await orig(*args, **kwargs)
            finally:
                in_shadow.pop()

        return shadow_pass

    for tier, name in _TIER_READS:
        target = targets[tier]
        monkeypatch.setattr(target, name, recorded(f"{tier}.{name}", getattr(target, name)))
    for name in ("emit_facts_shadow", "emit_episodes_shadow"):
        monkeypatch.setattr(memory_context, name, marked(getattr(memory_context, name)))
    return reads


def _prompt_host(
    tiers: SessionTiers, facts_mode: str, episodic_mode: str,
) -> memory_context._MemoryContextMixin:
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
    host._facts_cross_room = facts_mode
    host._episodic_cross_room = episodic_mode
    return host


class TestInjectMemoryContextDefaultPath:
    """Since RFC 0049 PR 4 the default ``cross_room: live`` makes the
    facts recall pass ``sessions="*"``, so F-3 is "no ungated widening":
    on the reads that feed the prompt, ``"*"`` reaches only the facts
    tier, only under ``live``, and only through the gate.  One turn in
    session ``arc-2``, rows in ``arc-1``, under every pairing of the two
    ``cross_room`` knobs, so each tier is held to its own knob:

    * Only the facts reads (``FactStore.recall`` and
      ``FactStore.topic_subjects``) receive ``"*"``: the prompt's under
      ``live``, and under ``shadow`` only the log-only shadow pass's.
    * Every other spied read keeps the §D default (``sessions=None``, or
      no ``sessions`` at all), so the other session's note, relationship
      row and channel turn never reach the prompt in any mode.
    * A widened row reaches the prompt only through the RFC 0037 §D
      gate: the ``"*"`` read returns the ``restricted`` fact, the live
      episodic read the ``restricted`` episode, and the gate withholds
      both from an ``internal`` turn.
    * ``off`` is the negative control: the same spies see no ``"*"``.

    The live episodic tier reads across sessions without ``"*"``:
    ``recall_room_ranked`` gives its query helpers ``sessions=None`` and a
    same-room boost, so the other session's episode arrives unseen by the
    spies.  The relationship identity read is cross-room by design (the
    F-7 amendment) and takes no ``sessions``.
    """

    @pytest.mark.parametrize("episodic_mode", _MODES)
    @pytest.mark.parametrize("facts_mode", _MODES)
    async def test_star_reaches_only_the_gated_facts_read(
        self, facade_factory: FacadeFactory, monkeypatch: pytest.MonkeyPatch,
        facts_mode: str, episodic_mode: str,
    ) -> None:
        other = await facade_factory("arc-1")
        await other.facade.store_observation("met alice at the lake")
        sealed = await other.facade._episodic.store_episode(
            "alice sealed the lake ledger", {}, importance=0.9,
            session_id="arc-1", protection_level="restricted",
        )
        await other.facade.store_observation(
            "bram mended the pier lantern", scope="group:harbor",
        )
        await other.facade._episodic.store_note(
            "alice", "alice hides the lake boathouse key", session_id="arc-1",
        )
        # The first write tags the relationship row, and it defaults to ``legacy``.
        await other.rels.record_interaction("alice", "chat", session_id="arc-1")
        await other.rels.update_trust("alice", 0.1, "alice owes the ferry toll")
        lakeshore = await other.facts.store(
            subject="alice", predicate="works_at", object="lakeshore",
            source_interaction_id="ix-1", asserted_at=1000.0, session_id="arc-1",
        )
        vault = await other.facts.store(
            subject="alice", predicate="knows", object="vault-code",
            source_interaction_id="ix-2", asserted_at=1000.0, session_id="arc-1",
            protection_level="restricted",
        )
        here = await facade_factory("arc-2")
        await here.facade.store_observation("picnic by the lake at noon")
        await here.facade.store_observation(
            "cato moored the skiff", scope="group:harbor",
        )
        await here.facade._episodic.store_note(
            "alice", "alice guides lake tours", session_id="arc-2",
        )
        reads = _spy_tier_reads(monkeypatch, here)
        host = _prompt_host(here, facts_mode, episodic_mode)

        result = await host._inject_memory_context(AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            # One word, and not the room's name: the LIKE fallback of a
            # build without FTS5 matches one substring, and the index
            # covers an episode's scope as well as its summary.
            payload={"content": "lake"},
            channel_id="group:harbor",
            sender_id="alice",
            metadata={"channel_classification": "internal"},
        ))

        # Every spied read ran, so the width checks below are not vacuous.
        # (``episodic.recall`` is the channel-history read too; the rows
        # asserted present further down show each tier's own read ran.)
        assert {read for read, *_ in reads} == {f"{t}.{m}" for t, m in _TIER_READS}
        # "*" is the only width any read passes, and only the facts reads
        # pass it: the prompt's under live, the shadow pass's under shadow.
        assert [(read, width) for read, width, *_ in reads if width not in (None, "*")] == []
        facts_reads = ("facts.recall", "facts.topic_subjects")
        assert {(read, shadow) for read, sessions, shadow, _ in reads if sessions == "*"} == {
            CROSS_ROOM_OFF: set(),
            CROSS_ROOM_SHADOW: {(read, True) for read in facts_reads},
            CROSS_ROOM_LIVE: {(read, False) for read in facts_reads},
        }[facts_mode]

        rendered = "\n".join(s.content for s in host._working_memory._sections)
        manifest = {e.entry_id for e in result.manifest}
        facts_live = facts_mode == CROSS_ROOM_LIVE
        # The other session's fact and episode reach the prompt only under
        # their own tier's live, and the manifest lists what arrived ...
        assert ("lakeshore" in rendered) is facts_live
        assert (lakeshore in manifest) is facts_live
        assert ("met alice at the lake" in rendered) is (episodic_mode == CROSS_ROOM_LIVE)
        # ... and only through the gate: the "*" read returned the
        # restricted fact, and the gate kept it out of prompt and manifest.
        star_rows = {
            fact.fact_id for read, sessions, _, rows in reads
            if read == "facts.recall" and sessions == "*" for fact in rows or ()
        }
        assert (vault in star_rows) is (facts_mode != CROSS_ROOM_OFF)
        assert vault not in manifest
        assert "vault-code" not in rendered
        # The restricted episode matches the query like its neighbour that
        # arrives under live, and the gate keeps it out the same way.
        assert sealed in {e.id for e in await other.facade._episodic.recall("lake", sessions="*")}
        assert sealed not in manifest
        assert "lake ledger" not in rendered
        # This session's rows arrive in every mode; the other session's
        # note, channel turn and relationship row never do.
        assert "picnic by the lake" in rendered
        assert "cato moored the skiff" in rendered
        assert "guides lake tours" in rendered
        assert "boathouse" not in rendered
        assert "pier lantern" not in rendered
        assert "ferry toll" in ((await other.rels.get_relationship_summary("alice")).notes or "")
        assert "ferry toll" not in rendered
