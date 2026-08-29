"""
ISSUE-0130 — the catch-up replay must not derive memory it cannot attribute.

On agent startup the RFC 0011 channel catch-up replays the last N messages
of every subscribed channel through ``on_event`` with
``metadata["replay_mode"] = True``.  Those events carry **no principal**:
the orchestrator's ``messages`` table has no principal column, so
``_build_replay_event`` has nothing to seed and the persona binds its
default (``local``).  Before this fix the close path still summarised the
replayed span and ran the RFC 0026 extractor over it, writing one
authenticated person's content into the shared ``local`` tenant — where
the whole persona fleet, every autonomous turn and every caller under
``auth.mode: disabled`` resolves.  Catch-up has no watermark (RFC 0011
OQ #8), so it re-ingests the window on every boot and the duplication is
unbounded.

Found live at the v0.3.14 ``MT-MEMORY-MULTIUSER-001`` execution run
(F-2), where a persona restart mid-arc produced two ``local`` episodes
and two ``local`` facts duplicating Alice's private disclosure.

The bar these tests hold:

1. an interaction opened by a replayed turn is flagged, and its close
   derives nothing;
2. the flag rides the same only-on-open contract as ``session_id`` — it
   is frozen in both directions;
3. **a live turn never lands in a flagged span.**  Replay opens tracker
   scopes and never closes them on its own, so without a boundary the
   next live CHANNEL_MESSAGE would append to the catch-up interaction
   (``channel_catchup``'s "catch-up → live" contract) and the skip would
   eat a fully attributable conversation.  Two closes hold that line:
   the sweep at the end of the catch-up pass
   (:func:`~agents.persona_runtime.close_path.close_replayed_scopes`)
   and, for a live turn that arrives while the pass is still running —
   the gRPC dispatch surface is already serving by then — the ingest-time
   split (:func:`~agents.persona_runtime.interaction_boundary
   .stale_close_reason`).
"""

from __future__ import annotations

import asyncio

import pytest

from agents.memory.boundary_detectors import (
    REASON_CATCHUP_COMPLETE,
    REASON_STRUCTURAL,
)
from agents.memory.interactions import InteractionTracker
from agents.persona import create_persona_agent
from agents.persona_runtime.close_path import (
    close_replayed_scopes,
    persist_closed_interaction,
)
from agents.persona_runtime.interaction_boundary import stale_close_reason
from agents.persona_types import AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── The tracker contract: frozen at open ──────────────────────────────


def test_replayed_turn_flags_the_interaction_it_opens() -> None:
    tracker = InteractionTracker()
    interaction = tracker.add_turn("dm:alice", replayed=True)
    assert interaction.replayed is True


def test_live_turn_leaves_the_interaction_unflagged() -> None:
    tracker = InteractionTracker()
    interaction = tracker.add_turn("dm:alice")
    assert interaction.replayed is False


def test_replay_appended_to_a_live_interaction_does_not_flag_it() -> None:
    """Frozen-at-open, exactly like ``session_id``: a later replayed turn
    cannot relabel a span a live turn opened."""
    tracker = InteractionTracker()
    live = tracker.add_turn("dm:alice")
    same = tracker.add_turn("dm:alice", replayed=True)
    assert same is live
    assert live.replayed is False


def test_live_turn_cannot_clear_a_replayed_interactions_flag() -> None:
    """The converse: the flag is frozen in both directions."""
    tracker = InteractionTracker()
    replayed = tracker.add_turn("dm:alice", replayed=True)
    tracker.add_turn("dm:alice")
    assert replayed.replayed is True


# ─── The close-path gate ───────────────────────────────────────────────


class _RecordingEpisodic:
    """Fails the test if the close path writes anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def store_episode(self, **kwargs: object) -> str:
        self.calls.append("store_episode")
        return "should-not-happen"

    async def store_closed_interaction(self, **kwargs: object) -> str:
        self.calls.append("store_closed_interaction")
        return "should-not-happen"


@pytest.mark.asyncio
async def test_replayed_interaction_derives_nothing_on_close() -> None:
    """The leak-stopper: no episode row, so no facts extracted from it."""
    tracker = InteractionTracker()
    opened = tracker.add_turn(
        "dm:alice", payload={"text": "My daughter Mira turns seven next month."},
        replayed=True,
    )
    # ``close_record``, not ``close_scope``: the room fan deliberately
    # excludes replay-opened records (PR #846 review), and this test is
    # about what the close PATH does with one.
    closed = tracker.close_record(opened, reason=REASON_STRUCTURAL)
    assert closed is not None
    assert closed.turn_count == 1, "the span must be non-empty, or the test is vacuous"

    episodic = _RecordingEpisodic()
    pending: set[asyncio.Task[None]] = set()
    finalized: list[bool] = []

    async def _on_finalized() -> None:
        finalized.append(True)

    await persist_closed_interaction(
        episodic=episodic,  # type: ignore[arg-type]
        llm_client=None,  # type: ignore[arg-type]
        memory_ns=None,  # type: ignore[arg-type]
        agent_id="ember-owl",
        interaction=closed,
        pending_tasks=pending,
        on_finalized=_on_finalized,
    )

    assert episodic.calls == [], (
        "a replayed span must not reach storage — it has no principal to "
        f"attribute memory to, but wrote {episodic.calls}"
    )
    assert pending == set(), "no background summarisation task may be spawned"
    assert finalized == [], "the close must not tick the auto-reflect counter"


@pytest.mark.asyncio
async def test_live_interaction_still_derives__positive_control() -> None:
    """The control that keeps the test above honest.

    An absence assertion proves nothing unless the same harness produces a
    presence under the opposite condition: without this, a typo in the
    fixture (or a close path that silently no-ops for every interaction)
    would leave ``test_replayed_interaction_derives_nothing_on_close``
    passing for a reason unrelated to the gate.  Identical setup, only
    ``replayed`` flipped — this one MUST write.
    """
    tracker = InteractionTracker()
    opened = tracker.add_turn(
        "dm:alice", payload={"text": "My daughter Mira turns seven next month."},
    )
    closed = tracker.close_record(opened, reason=REASON_STRUCTURAL)
    assert closed is not None
    assert closed.replayed is False

    episodic = _RecordingEpisodic()
    pending: set[asyncio.Task[None]] = set()

    async def _on_finalized() -> None:
        return None

    await persist_closed_interaction(
        episodic=episodic,  # type: ignore[arg-type]
        llm_client=None,  # type: ignore[arg-type]
        memory_ns=None,  # type: ignore[arg-type]
        agent_id="ember-owl",
        interaction=closed,
        pending_tasks=pending,
        on_finalized=_on_finalized,
    )

    assert episodic.calls == ["store_episode"], (
        "the identical span must reach storage when it is NOT replayed — "
        "otherwise the absence assertion above is vacuous"
    )


# ─── The catch-up → live boundary ──────────────────────────────────────


def _event(*, replay: bool, wire_id: str = "wire-1") -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hello", "channel_type": "group"},
        channel_id="group:planning",
        sender_id="alex",
        metadata=(
            {"interaction_id": wire_id, "replay_mode": True}
            if replay
            else {"interaction_id": wire_id}
        ),
    )


def test_live_turn_splits_a_replay_opened_scope() -> None:
    """The regression this whole boundary exists for.

    Catch-up opens the scope at boot and never closes it, so a live turn
    arriving before the pass-end sweep would otherwise append to a span
    that derives nothing — silently losing the memory of the first
    conversation after every restart.  Note the wire id is UNCHANGED:
    a mid-conversation restart resumes the same channel interaction, so
    the rotation boundary cannot see this one.
    """
    tracker = InteractionTracker()
    replayed = tracker.add_turn("group:planning", replayed=True)
    reason = stale_close_reason(
        tracker.get("group:planning"), _event(replay=False), wire_id="wire-1",
    )
    assert reason == REASON_CATCHUP_COMPLETE
    assert replayed.replayed is True


def test_replayed_turn_does_not_split_a_replay_opened_scope() -> None:
    """Replay→replay is one unattributable span, segmented only by the
    wire rotation like any other — splitting per replayed turn would
    shred the catch-up window into one interaction per message."""
    tracker = InteractionTracker()
    tracker.add_turn("group:planning", replayed=True)
    assert stale_close_reason(
        tracker.get("group:planning"), _event(replay=True), wire_id="wire-1",
    ) is None


def test_live_turn_does_not_split_a_live_scope() -> None:
    """The guard against over-splitting: an unflagged span keeps the
    pre-ISSUE-0130 rotation-only behaviour."""
    tracker = InteractionTracker()
    tracker.add_turn("group:planning")
    assert stale_close_reason(
        tracker.get("group:planning"), _event(replay=False), wire_id="wire-1",
    ) is None


def test_wire_rotation_still_closes_a_live_scope() -> None:
    """...and the rotation boundary it shares the seam with is intact."""
    tracker = InteractionTracker()
    live = tracker.add_turn("group:planning")
    live.wire_interaction_id = "wire-1"
    assert stale_close_reason(
        tracker.get("group:planning"), _event(replay=False, wire_id="wire-2"),
        wire_id="wire-2",
    ) == REASON_STRUCTURAL


@pytest.mark.asyncio
async def test_catchup_sweep_closes_only_replayed_scopes() -> None:
    """The pass-end sweep: every scope catch-up opened is popped, so no
    later live turn can land in one.  Scopes opened by live traffic
    (a dispatch that raced the boot pass) are left alone."""
    tracker = InteractionTracker()
    tracker.add_turn("group:planning", replayed=True)
    tracker.add_turn("dm:alice", replayed=True)
    live = tracker.add_turn("group:launch")
    persisted: list[str] = []

    async def _persist(interaction) -> None:  # type: ignore[no-untyped-def]
        persisted.append(interaction.scope)

    closed = await close_replayed_scopes(tracker, _persist)

    assert closed == 2
    assert sorted(persisted) == ["dm:alice", "group:planning"]
    assert tracker.open_scopes() == ["group:launch"]
    assert tracker.get("group:launch") is live


@pytest.mark.asyncio
async def test_catchup_sweep_pops_the_scope_even_if_persist_fails() -> None:
    """Best-effort by contract — the sweep runs on the boot path.  The
    scope must be popped regardless, because that is the part live
    traffic depends on, and the sweep must not raise into startup."""
    tracker = InteractionTracker()
    tracker.add_turn("group:planning", replayed=True)

    async def _boom(interaction) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("episodic DB down at boot")

    assert await close_replayed_scopes(tracker, _boom) == 1
    assert tracker.open_scopes() == []


# ─── End to end: the conversation a restart interrupts ─────────────────


async def _episodes(agent) -> list[dict]:  # type: ignore[no-untyped-def]
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT summary, turn_count, scope FROM episodes "
        "WHERE agent_id = ? ORDER BY created_at",
        (agent.agent_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"summary": r[0], "turn_count": r[1], "scope": r[2]} for r in rows]


@pytest.mark.asyncio
async def test_conversation_resumed_after_a_restart_still_derives() -> None:
    """The full path, in the shape a real restart produces.

    Boot replays the tail of an in-flight channel conversation, then the
    same conversation continues live under the SAME wire interaction id
    (the orchestrator did not restart, so nothing rotated).  Exactly one
    episode must be written, covering the two LIVE turns and not the
    replayed one: the replayed span is dropped for want of a principal,
    the live span derives under its own.
    """
    cfg = {**_PERSONA_CONFIG}
    agent = create_persona_agent(
        agent_id=cfg["id"], config=cfg, llm_client=_make_client(),
    )
    await agent.initialize_memory()

    await agent._store_event_episode(_event(replay=True), [])
    await agent._store_event_episode(_event(replay=False), [])
    closing = _event(replay=False)
    closing.metadata["chat_end"] = True
    await agent._store_event_episode(closing, [])
    await agent.drain_pending_summaries()

    rows = await _episodes(agent)
    assert len(rows) == 1, (
        "the live half of a restart-interrupted conversation must still be "
        f"recorded — got {rows}"
    )
    assert rows[0]["turn_count"] == 2, (
        "the episode must cover the two live turns only; the replayed turn "
        "belongs to the span that was dropped"
    )
    await agent.close_memory()
