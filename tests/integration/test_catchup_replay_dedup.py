"""ISSUE-0130 (b) — the derivation is bounded, in both directions.

Split from ``test_catchup_replay_attribution.py`` (v0.3.15 PR B2 review
round 3) at the 500-line cap.  The seam matches the property under test:
that suite pins WHOSE tenant a replayed span derives under, this one pins
that the same content is not derived twice — across boots (the span
identity and the completeness gate) and within one (the live/replay
overlap).

Both are bounds on duplication, and both errs deliberately toward
duplicating rather than losing: a guard that matched too widely would drop
a span's memory outright, which ISSUE-0130 refuses everywhere.
"""

from __future__ import annotations

import pytest

from agents.channel_replay_event import build_replay_event
from agents.clock import FrozenClock
from agents.principal_id import seed_principal_metadata
from agents.tools.registry import clear_registry

from ._catchup_replay_helpers import (
    CHANNEL,
    _derived,
    _history_row,
    _replay,
    _replay_derived,
    _replay_identity,
)
from ._interaction_multi_turn_helpers import (
    GROUP_CHANNEL,
    channel_event,
    make_agent_with_clock,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestALiveTurnMidPassDerivesNoPrefix:
    """The doors the completeness gate did not cover (PR B2 review).

    Dispatch is already serving while catch-up runs — ``agents.server``
    self-registers before ``replay_for_persona_agents`` — so a live turn
    can land between two replayed rows.  Both of the ways it can reach a
    replay-opened record used to close AND derive it, holding only the
    prefix ingested so far, because the gate lived in the pass-end sweep's
    own loop body and neither of these paths goes through it.

    A prefix's span identity is a digest over the turns the record holds,
    so no later boot recomputes it: the next uninterrupted boot derives the
    whole window on top, and every interrupted boot adds another orphan.
    That is the ``0 → 2 → 5 → 13 → 18`` growth curve shape (b) exists to
    bound, through the doors its gate did not reach.  Both now read
    ``Interaction.replay_window_complete`` at the shared chokepoint.
    """

    async def test_a_live_turn_splitting_the_window_derives_no_prefix(self):
        """Door 1: the ingest-time replay→live split, on the SAME key."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        rows = [
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "alice", "and she loves whales", "alice-person"),
        ]
        await agent.on_event(build_replay_event(
            rows[0], GROUP_CHANNEL, "all", CHANNEL,
        ))
        # Alice speaks LIVE, mid-window, AUTHENTICATED — the live gRPC
        # ingress seeds the same metadata key the replay builder does, so
        # her turn resolves to the same ``(principal, speaker, scope)``
        # record the replay opened.  That identity is what makes this the
        # split: an unauthenticated live turn would resolve to ``local``
        # and simply open its own record, never touching the replayed one.
        live = channel_event("still there?", sender="alice", wire_id="wire-A")
        seed_principal_metadata(live.metadata, "alice-person")
        await agent.on_event(live)
        await agent.on_event(build_replay_event(
            rows[1], GROUP_CHANNEL, "all", CHANNEL,
        ))
        await agent.close_replayed_interactions(
            derive_channels=frozenset({GROUP_CHANNEL}),
        )
        await agent.drain_pending_summaries()

        assert await _replay_derived(agent) == [], (
            "no replayed span may derive from this pass: the split cut the "
            "window, so the prefix AND the tail it left behind both claim "
            "identities no uninterrupted boot can recompute"
        )
        assert [r for r in await _derived(agent) if r[1] == "alice"], (
            "the LIVE half must still derive normally — refusing it is the "
            "v0.3.14 cost, and without this the assertion above is vacuous"
        )

    async def test_a_wire_rotation_derives_no_other_speakers_prefix(self):
        """Door 2: a live wire ROTATION reaching a non-target record.

        ``stale_close_reason`` narrows the catch-up boundary to the record
        the arriving turn would land in — correctly, since fanning it
        room-wide chopped every unrelated live conversation into one-turn
        episodes.  But that narrowing also let a NON-target replayed record
        fall through to the rotation check, which closes it ``structural``
        and, before this gate, derived its prefix.
        """
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent.on_event(build_replay_event(
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            GROUP_CHANNEL, "all", CHANNEL,
        ))
        # BOB speaks live under a ROTATED wire id: a different record key,
        # so alice's replayed record is not the target — it reaches the
        # rotation branch instead of the catch-up one.
        await agent.on_event(channel_event(
            "new topic", sender="bob", wire_id="wire-B",
        ))
        await agent.close_replayed_interactions(
            derive_channels=frozenset({GROUP_CHANNEL}),
        )
        await agent.drain_pending_summaries()

        assert await _replay_derived(agent) == [], (
            "a replayed record closed by someone else's wire rotation holds "
            "a prefix like any other, and must not derive"
        )


@pytest.mark.asyncio
class TestTheSameMessageIsNotDerivedTwiceInOneBoot:
    """The same-boot live/replay overlap (PR B2 review round 3).

    Dispatch self-registers BEFORE `replay_for_persona_agents` runs, so a
    message published in that gap is dispatched live *and* is in the
    last-N page catch-up then fetches. Both records derive, and the
    re-derivation guard cannot see across them: the live record's
    `interaction_id` is a `uuid4`, not a content digest.

    Ingest dedup was rejected for the CROSS-boot case because the tracker
    is in-memory and a restart starts blind — which is exactly why it
    works within one boot. The replayed turn is still APPENDED and only
    MARKED, so the span digest keeps covering it: dropping it would make
    the identity depend on which messages happened to race, and the next
    boot would derive the window again under a different id.
    """

    async def test_a_message_ingested_live_is_not_summarised_again(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        # Published in the register→fetch gap: delivered live...
        live = channel_event("Mira turns seven", sender="alice", wire_id="wire-A")
        seed_principal_metadata(live.metadata, "alice-person")
        live.message_id = "m-1"
        await agent.on_event(live)
        # ...and still in the history page catch-up reads a moment later.
        await _replay(
            agent,
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "alice", "and she loves whales", "alice-person"),
        )

        replayed = await _replay_derived(agent)
        assert len(replayed) == 1, (
            "the replayed span still derives — it recovers m-2, which the "
            "agent was down for"
        )
        assert replayed[0][2] == 1, (
            "but over ONE turn, not two: m-1 was already ingested live this "
            "boot, so its content is excluded from the derivation input "
            f"(got turn_count={replayed[0][2]})"
        )

    async def test_the_span_identity_still_covers_the_excluded_turn(self):
        """The exclusion must not move the digest.

        A boot with no live overlap replays the same window and must
        compute the SAME identity, or the guard misses and the window
        derives a second time — trading a one-boot duplicate for an
        every-boot one.
        """
        overlapped = await make_agent_with_clock(FrozenClock(at=1_000.0))
        live = channel_event("Mira turns seven", sender="alice", wire_id="wire-A")
        seed_principal_metadata(live.metadata, "alice-person")
        live.message_id = "m-1"
        await overlapped.on_event(live)
        rows = [
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "alice", "and she loves whales", "alice-person"),
        ]
        await _replay(overlapped, *rows)

        clean = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(clean, *rows)

        assert await _replay_identity(overlapped) == await _replay_identity(clean), (
            "the marked turn stays ON the record, so both boots digest the "
            "same span — only what gets SUMMARISED differs"
        )
