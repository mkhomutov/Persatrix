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
2. an interaction opened by a LIVE turn is not flagged even when a
   replayed turn is later appended to it — that span still closes under
   the live principal and must derive normally (the frozen-at-open rule
   the ``session_id`` sibling-mislabel guard already uses);
3. the flag rides the same only-on-open contract as ``session_id``.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.interactions import InteractionTracker
from agents.persona_runtime.close_path import persist_closed_interaction

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
    """The load-bearing case for not over-suppressing.

    A replayed turn that lands in an already-open LIVE interaction must
    not mark the span: that interaction still closes under the live
    turn's principal, so its derivation is correctly attributed and must
    happen.  Frozen-at-open, exactly like ``session_id``.
    """
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
    tracker.add_turn(
        "dm:alice", payload={"text": "My daughter Mira turns seven next month."},
        replayed=True,
    )
    closed = tracker.close("dm:alice", reason=REASON_STRUCTURAL)
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
    tracker.add_turn(
        "dm:alice", payload={"text": "My daughter Mira turns seven next month."},
    )
    closed = tracker.close("dm:alice", reason=REASON_STRUCTURAL)
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
