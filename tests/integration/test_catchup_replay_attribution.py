"""ISSUE-0130 shape (b) — the replayed write, attributed, and derived once.

The v0.3.14 leak-stopper (shape (a)) bought the tenant boundary by
deriving nothing at all from an on-startup catch-up replay: a replayed
turn carried no principal, so any episode or RFC 0026 fact it produced
landed in the shared ``local`` tenant.  v0.3.15 PR B1 persisted the
tenant on the message row (channel-store ``v11 → v12``); this suite pins
what PR B2 does with it, from the REAL history JSON through the REAL
replay builder, the REAL ``on_event`` scope binding and the REAL close
path, to the stored ``episodes`` columns.

Four properties, and the last two are what make the first two safe:

1. **A row that names its tenant derives under it.**  Not ``local`` —
   the principal the orchestrator stamped at publish.
2. **A row that does not still derives nothing.**  A pre-v12
   orchestrator's history has no ``principal_id`` key at all, which is
   the one case the narrowed skip must keep refusing.  A present
   ``"local"`` is NOT that case: it is a real answer, and it derives.
3. **The same window replayed twice derives once.**  Catch-up has no
   watermark (RFC 0011 OQ #8) and re-reads the last-N page on every
   boot, so narrowing the skip without this would hand back the growth
   curve shape (a) bounded — ``local`` episodes ``0 → 2 → 5 → 13 → 18``
   across four restarts in the v0.3.14 MT — merely relocated to the
   correct tenant.  This is the release's stated acceptance bar.
4. **…and a window that GREW still derives.**  The guard bounds
   duplication; it must not cost a span the memory of messages no boot
   has seen yet.  Without this control, property 3 would also pass for
   a close path that had simply stopped deriving replays again.

The unit-level halves live in
:mod:`tests.unit.python.test_catchup_replay_principal_leak` (the skip and
its frozen-at-open marker) and
:mod:`tests.unit.python.test_replay_span_identity` (the digest).
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
class TestReplayDerivesUnderTheRowsTenant:
    async def test_attributed_replay_derives_in_its_own_tenant(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent,
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
        )
        assert await _derived(agent) == [
            ("alice-person", "alice", "Multi-turn session summary."),
        ], (
            "the replayed span must derive under the tenant the message row "
            "names — deriving it under `local` is the ISSUE-0130 leak, and "
            "not deriving it at all is the v0.3.14 cost shape (b) removes"
        )

    async def test_two_tenants_in_one_room_derive_separately(self):
        # The ISSUE-0123 re-key applied to the replayed write: one room,
        # one pass, two principals — two records, neither readable by the
        # other's tenant.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent,
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "bob", "noted", "bob-person"),
        )
        assert sorted(row[:2] for row in await _derived(agent)) == [
            ("alice-person", "alice"), ("bob-person", "bob"),
        ]

    async def test_unattributed_replay_still_derives_nothing(self):
        # The leak-stopper, on the only case that still reaches it: a
        # pre-v12 orchestrator, whose history JSON has no `principal_id`.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent, _history_row("m-1", "alice", "Mira turns seven", None),
        )
        assert await _derived(agent) == [], (
            "a row that cannot name its tenant must derive nothing — this "
            "is the v0.3.14 leak-stopper, narrowed but not withdrawn"
        )

    async def test_a_present_local_is_an_answer_and_derives(self):
        # The distinction the record key cannot make and the whole reason
        # `replay_attributed` exists: `"local"` PRESENT means "this
        # publish had no verified tenant" (an agent publish, or the whole
        # deployment under `auth.mode: disabled`), where `local` is the
        # correct attribution — the v0.3.14 cost this PR removes.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent, _history_row("m-1", "ember-owl-peer", "status", "local"),
        )
        assert await _derived(agent) == [
            ("local", "ember-owl-peer", "Multi-turn session summary."),
        ]


@pytest.mark.asyncio
class TestReplayIsIdempotentAcrossPasses:
    async def test_the_same_window_replayed_twice_derives_once(self):
        """The release acceptance bar (v0.3.15 plan, scope lock 4)."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        window = (
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "alice", "she likes dinosaurs", "alice-person"),
        )
        await _replay(agent, *window)
        after_first = await _derived(agent)
        assert len(after_first) == 1, after_first

        # The second boot: catch-up has no watermark, so it re-reads the
        # same page. Same rows, same order, a fresh tracker.
        await _replay(agent, *window)
        assert await _derived(agent) == after_first, (
            "a span replayed twice must derive once — without this the "
            "narrowed skip hands back the unbounded re-derivation shape "
            "(a) bounded, relocated from `local` to the right tenant"
        )

    async def test_a_grown_window_derives_again(self):
        """The control: the guard bounds duplication, it does not stop
        replay from ever deriving again.  A window that gained a message
        while the agent was down is a different span, and its content has
        never been summarised."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        first = _history_row("m-1", "alice", "Mira turns seven", "alice-person")
        await _replay(agent, first)
        assert len(await _derived(agent)) == 1

        await _replay(
            agent, first,
            _history_row("m-2", "alice", "she likes dinosaurs", "alice-person"),
        )
        assert len(await _derived(agent)) == 2, (
            "a replayed window that GREW must derive again — skipping it "
            "would lose the memory of every message that arrived while "
            "the agent was down, which is the cost shape (b) buys back"
        )


@pytest.mark.asyncio
class TestReplayedAndLiveTurnsNeverShareARecord:
    """The catch-up boundary, in BOTH directions (PR B2 review).

    Dispatch is already serving while catch-up runs — ``agents.server``
    self-registers before ``replay_for_persona_agents`` — so for any
    ``(principal, speaker, scope)`` key it is a race which kind of turn
    opens the record.  Only the replay-opened → live-turn direction was
    split; the reverse merged silently, and the merged record's frozen
    ``replayed`` is ``False``, which bypasses BOTH close-path guards.
    """

    @staticmethod
    def _live_turn_from(principal: str) -> object:
        """An AUTHENTICATED live turn, so it resolves the SAME record key
        the replayed rows below do.

        Without a principal the live turn keys on ``local`` and the two
        never meet — the merge this class exists to pin needs the live
        and replayed halves to agree on all three key axes.  Seeded
        through the production helper so a key rename cannot leave this
        green against a key nothing reads.
        """
        event = channel_event("live first", sender="alice")
        seed_principal_metadata(event.metadata, principal)
        return event

    async def test_a_replayed_turn_does_not_join_a_live_opened_record(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        # The live turn wins the race and opens the record for this key.
        await agent.on_event(self._live_turn_from("alice-person"))
        # Catch-up then reaches the same channel and replays alice's
        # history, which resolves the identical record key.
        await agent.on_event(build_replay_event(
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            GROUP_CHANNEL, "all", CHANNEL,
        ))
        open_records = list(agent._interaction_tracker.open_records())
        assert [r.replayed for r in open_records] == [True], (
            "the live record must have closed and the replayed turn opened "
            "its own; merging them leaves one record flagged live that "
            "holds replayed turns, which consults neither ISSUE-0130 guard"
        )

    async def test_the_replayed_half_of_a_lost_race_is_still_guarded(self):
        # The consequence that made the missing direction matter: without
        # the split the replayed window is re-derived on every boot,
        # because only a `replayed` record consults the guard.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        row = _history_row("m-1", "alice", "Mira turns seven", "alice-person")

        await agent.on_event(self._live_turn_from("alice-person"))
        await _replay(agent, row)
        after_first = await _derived(agent)
        assert any(r[0] == "alice-person" for r in after_first), (
            "the replayed span must reach the sweep and derive under its "
            "own tenant — without this the comparison below is vacuous, "
            "since a merged record is not swept and derives nothing at all"
        )

        await _replay(agent, row)
        assert await _derived(agent) == after_first, (
            "the replayed span must be recognised on the next pass even "
            "when a live turn opened the record first"
        )


@pytest.mark.asyncio
class TestTheV12BackfillIsUnattributable:
    async def test_an_empty_principal_derives_nothing(self):
        """Migration v11→v12 backfills ``''``, and ``''`` is not an answer.

        A row that predates the column has no evidence of who caused it.
        The backfill says so with the empty string precisely so this case
        stays on the shape-(a) skip: backfilling ``'local'`` instead would
        make every pre-upgrade row read as ATTRIBUTED on the first
        post-upgrade catch-up and derive an authenticated person's content
        into the shared tenant — the ISSUE-0130 leak, reopened for the
        upgrade window.
        """
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent, _history_row("m-1", "alice", "Mira turns seven", ""),
        )
        assert await _derived(agent) == []

    async def test_a_whitespace_principal_is_not_an_answer_either(self):
        # The record key resolves a whitespace principal back to `local`
        # (`normalize_principal_id` strips), so seeding it verbatim would
        # mark the span attributed while landing its content in the shared
        # tenant.  The write end strips too, so it reads as absent.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent, _history_row("m-1", "alice", "Mira turns seven", "   "),
        )
        assert await _derived(agent) == []


@pytest.mark.asyncio
class TestAnUnfinishedPassDerivesNothing:
    async def test_a_truncated_pass_does_not_derive_its_prefix(self):
        """The wall-clock budget overrun (PR B2 review).

        The pass-end sweep runs in ``replay_for_persona_agents``'s
        ``finally``, so a budget overrun still closes what it ingested —
        a PREFIX of the window.  Deriving that prefix claims an identity
        no later boot can recompute (the digest is over the turns the
        record holds), so the next complete boot derives the whole window
        again on top of it.  The window never moved, so this is not the
        documented residual.  Catch-up re-reads the window every boot
        anyway, so declining costs one boot's derivation, not the memory.
        """
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        rows = [
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "alice", "and she loves whales", "alice-person"),
        ]
        # The pass is cut off after the first row: this channel finished
        # nothing, so it is not in the completed set.
        await agent.on_event(build_replay_event(
            rows[0], GROUP_CHANNEL, "all", CHANNEL,
        ))
        await agent.close_replayed_interactions(derive_channels=frozenset())
        await agent.drain_pending_summaries()
        assert await _derived(agent) == [], "a prefix must not be derived"

        # The next boot completes the window and derives it whole, once.
        await _replay(agent, *rows)
        assert len(await _derived(agent)) == 1

    async def test_a_channel_that_finished_derives_even_when_another_did_not(
        self,
    ):
        """Completeness is per CHANNEL, not per pass (PR B2 review).

        The first cut carried one boolean for the whole agent, so a
        budget overrun in the ninth channel threw away the eight windows
        that had already replayed to completion — memory the boot had
        legitimately earned, discarded for a channel it had nothing to do
        with.  Here the sweep is told exactly one channel finished, and
        that channel's record must still derive.
        """
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        other_channel = "group:other"
        await agent.on_event(build_replay_event(
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            GROUP_CHANNEL, "all", CHANNEL,
        ))
        await agent.on_event(build_replay_event(
            _history_row("m-2", "bob", "half a window", "bob-person"),
            other_channel, "all",
            {"channel_type": "group", "id": other_channel},
        ))

        await agent.close_replayed_interactions(
            derive_channels=frozenset({GROUP_CHANNEL}),
        )
        await agent.drain_pending_summaries()

        assert [row[:2] for row in await _derived(agent)] == [
            ("alice-person", "alice"),
        ], (
            "the completed channel derives; the truncated one waits for a "
            "boot that finishes it"
        )
