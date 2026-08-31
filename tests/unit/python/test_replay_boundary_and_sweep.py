"""ISSUE-0130 (b) PR B2 review — the catch-up BOUNDARY and the boot SWEEP.

Two seams the review found defects in, both about reach rather than about
what they decide:

* :func:`~agents.persona_runtime.interaction_boundary.stale_close_reason`
  — WHICH records a replay/live disagreement may close, and what counts
  as a disagreement.
* :func:`~agents.persona_runtime.replay_sweep.close_replayed_scopes` —
  what the pass-end sweep may spend on the boot path, and which of the
  tasks in flight it is actually measuring.

The attribution half and the span digest live in
:mod:`tests.unit.python.test_catchup_replay_principal_leak` and
:mod:`tests.unit.python.test_replay_span_identity`; the end-to-end arc is
:mod:`tests.integration.test_catchup_replay_attribution`.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.epoch_id import epoch_scope, resolve_epoch_id_silent
from agents.memory.boundary_detectors import (
    REASON_CATCHUP_COMPLETE,
    REASON_STRUCTURAL,
)
from agents.memory.interactions import Interaction, InteractionTracker
from agents.persona_runtime.close_path import (
    close_stale_records,
    persist_closed_interaction,
)
from agents.persona_runtime.interaction_boundary import stale_close_reason
from agents.persona_runtime.replay_sweep import close_replayed_scopes
from agents.persona_types import AgentEvent, EventType
from agents.principal_id import EVENT_PRINCIPAL_METADATA_KEY

SCOPE = "group:planning"


def _event(
    *, sender: str, replay: bool = False, principal: str | None = None,
) -> AgentEvent:
    metadata: dict = {}
    if replay:
        metadata["replay_mode"] = True
    if principal is not None:
        metadata[EVENT_PRINCIPAL_METADATA_KEY] = principal
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "hi"},
        channel_id=SCOPE,
        sender_id=sender,
        metadata=metadata,
    )


# ─── The boundary's REACH (PR B2 review finding 1) ─────────────────────


class TestTheCatchupSplitIsKeyScoped:
    """A replayed turn must not close conversations it could never join.

    The fan applies :func:`stale_close_reason` to EVERY record in the
    scope, because wire rotation is a room event — the channel's
    conversation ended, so every record in it is stale.  The catch-up
    boundary is not a room event: a turn can only merge into the record
    under its own ``(principal, speaker, scope)`` key.  Fanning it
    room-wide meant one replayed row closed every unrelated live
    conversation in the room, chopping each into a one-turn episode and
    firing an unmetered summarise for it — on the boot path, while
    dispatch was serving.
    """

    def test_a_replayed_event_leaves_another_speakers_live_record_alone(
        self,
    ) -> None:
        tracker = InteractionTracker()
        bob_live = tracker.add_turn(SCOPE, speaker_id="bob")
        assert not bob_live.replayed

        # Alice's replayed row cannot land in bob's record.
        assert stale_close_reason(
            bob_live, _event(sender="alice", replay=True), wire_id="",
            is_target_record=False,
        ) is None

    def test_a_live_event_leaves_another_speakers_replayed_record_alone(
        self,
    ) -> None:
        """The original direction, narrowed the same way.

        The pass-end sweep closes every replay-opened record anyway, so
        nothing is left dangling — but a live turn from ``bob`` has no
        business retiring ``alice``'s replayed span.
        """
        tracker = InteractionTracker()
        alice_replay = tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True, replay_attributed=True,
        )
        assert stale_close_reason(
            alice_replay, _event(sender="bob"), wire_id="",
            is_target_record=False,
        ) is None

    @pytest.mark.parametrize(
        ("record_replayed", "event_replay"),
        [(True, False), (False, True)],
        ids=["live-onto-replay", "replay-onto-live"],
    )
    def test_the_target_record_still_splits_in_both_directions(
        self, record_replayed: bool, event_replay: bool,
    ) -> None:
        """Narrowing is only safe if the merge it exists to stop still
        fires — that record is the one the arriving turn would join."""
        tracker = InteractionTracker()
        record = tracker.add_turn(
            SCOPE, speaker_id="alice",
            replayed=record_replayed, replay_attributed=record_replayed,
        )
        assert stale_close_reason(
            record,
            _event(
                sender="alice", replay=event_replay,
                principal="alice-person" if event_replay else None,
            ),
            wire_id="", is_target_record=True,
        ) == REASON_CATCHUP_COMPLETE

    @pytest.mark.asyncio
    async def test_the_fan_closes_only_the_events_own_record(self) -> None:
        """The finding as the fan actually reaches it.

        ``close_stale_records`` resolves the arriving turn's key itself,
        so this pins the wiring and not just the predicate.
        """
        tracker = InteractionTracker()
        bob_live = tracker.add_turn(SCOPE, speaker_id="bob")
        carol_live = tracker.add_turn(SCOPE, speaker_id="carol")
        alice_replay = tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True, replay_attributed=True,
        )

        persisted: list[Interaction] = []

        async def _persist(interaction: Interaction) -> None:
            persisted.append(interaction)

        # A replayed row from alice arrives while dispatch is serving.
        await close_stale_records(
            tracker, SCOPE,
            _event(sender="alice", replay=True, principal="alice-person"),
            wire_id="", persist=_persist,
        )

        assert bob_live.is_open and carol_live.is_open, (
            "a replayed row must not chop unrelated live conversations"
        )
        assert persisted == [], "and must not derive them either"
        assert alice_replay.is_open, (
            "alice's own replayed record agrees with the arriving turn, so "
            "it is not stale — replay→replay is not a split"
        )


# ─── What counts as a disagreement (PR B2 review finding 6) ────────────


class TestAttributionIsHalfOfTheDisagreement:
    """A seeded ``"local"`` and an unseeded default are the SAME key.

    So a row that cannot name its tenant otherwise joins a span opened by
    one that can, and its content is summarised into the shared tenant
    under the opener's attribution — the ISSUE-0130 leak, through the one
    field ``replay_attributed`` was supposed to answer.  Freezing the flag
    at open only decides the OPENING turn; the boundary is what keeps the
    rest of the span honest.
    """

    def test_an_unattributable_row_splits_off_an_attributed_span(
        self,
    ) -> None:
        tracker = InteractionTracker()
        attributed = tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True, replay_attributed=True,
        )
        # A pre-v12 row, or one whose principal the seed rejected: it
        # resolves the ambient default, which is the same record key.
        assert stale_close_reason(
            attributed, _event(sender="alice", replay=True),
            wire_id="", is_target_record=True,
        ) == REASON_CATCHUP_COMPLETE

    def test_an_attributed_row_splits_off_an_unattributable_span(
        self,
    ) -> None:
        """The other direction, which the old code also merged — and
        merging it was the LOSING kind: the attributed row's content was
        skipped along with the span it joined."""
        tracker = InteractionTracker()
        unattributed = tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True,
        )
        assert stale_close_reason(
            unattributed,
            _event(sender="alice", replay=True, principal="local"),
            wire_id="", is_target_record=True,
        ) == REASON_CATCHUP_COMPLETE

    def test_two_rows_that_agree_share_their_span(self) -> None:
        tracker = InteractionTracker()
        attributed = tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True, replay_attributed=True,
        )
        assert stale_close_reason(
            attributed,
            _event(sender="alice", replay=True, principal="alice-person"),
            wire_id="", is_target_record=True,
        ) is None

    def test_a_live_event_never_reads_as_attributed(self) -> None:
        """The conjunct that keeps the pair meaningful.

        The live gRPC ingress seeds the same principal key, so without
        ``replayed and ...`` every authenticated live turn would disagree
        with the live record it belongs to and split on every turn.
        """
        tracker = InteractionTracker()
        live = tracker.add_turn(SCOPE, speaker_id="alice")
        assert stale_close_reason(
            live, _event(sender="alice", principal="alice-person"),
            wire_id="", is_target_record=True,
        ) is None


# ─── What the sweep may spend (PR B2 review findings 2, 8, 9) ──────────


class _SpawningTracker(InteractionTracker):
    """A tracker preloaded with ``n`` attributed replayed records."""

    def __init__(self, n: int, *, channel: str = SCOPE) -> None:
        super().__init__()
        for i in range(n):
            self.add_turn(
                SCOPE, speaker_id=f"s{i}", replayed=True,
                replay_attributed=True, source_channel_id=channel,
            )


@pytest.mark.asyncio
class TestTheSweepBoundsWhatItCostsBoot:
    async def test_the_throttle_gives_up_rather_than_stalling_boot(
        self,
    ) -> None:
        """The sweep runs in ``replay_for_persona_agents``'s ``finally``,
        OUTSIDE the 60 s catch-up ``wait_for`` — and each task it paces
        holds a 30 s LLM round trip.  With no budget the boot tail grew
        with the replayed-record count, and ``AgentServer.start`` arms the
        ISSUE-0125 re-registration watcher only after catch-up returns.
        """
        tracker = _SpawningTracker(6)
        pending: set[asyncio.Task[None]] = set()

        async def _never() -> None:
            await asyncio.Event().wait()  # a hung provider

        async def _persist(interaction: Interaction) -> None:
            task = asyncio.create_task(_never())
            pending.add(task)
            task.add_done_callback(pending.discard)

        loop = asyncio.get_running_loop()
        started = loop.time()
        closed = await close_replayed_scopes(
            tracker, _persist, pending_tasks=pending,
            max_in_flight=1, throttle_budget_sec=0.05,
        )
        elapsed = loop.time() - started

        for task in list(pending):
            task.cancel()
        assert closed == 6, "every record is still closed and persisted"
        assert elapsed < 1.0, (
            "the throttle must give up its budget rather than hold boot "
            f"behind tasks that never land (took {elapsed:.2f}s)"
        )

    async def test_it_paces_against_its_own_tasks_not_the_shared_set(
        self,
    ) -> None:
        """``pending_tasks`` is shared with the live close path, which is
        serving throughout catch-up.  Measuring it blocked the sweep on
        unrelated live closes — here, four of them, already in flight and
        never landing, with ``max_in_flight=2``."""
        tracker = _SpawningTracker(3)
        pending: set[asyncio.Task[None]] = set()

        async def _never() -> None:
            await asyncio.Event().wait()

        live_tasks = [asyncio.create_task(_never()) for _ in range(4)]
        for task in live_tasks:
            pending.add(task)
            task.add_done_callback(pending.discard)

        swept: list[Interaction] = []

        async def _persist(interaction: Interaction) -> None:
            swept.append(interaction)  # derives without spawning

        loop = asyncio.get_running_loop()
        started = loop.time()
        await close_replayed_scopes(
            tracker, _persist, pending_tasks=pending,
            max_in_flight=2, throttle_budget_sec=30.0,
        )
        elapsed = loop.time() - started

        for task in live_tasks:
            task.cancel()
        assert len(swept) == 3
        assert elapsed < 1.0, (
            "four live tasks in flight must not make the sweep wait: the "
            f"cap counts the tasks THIS sweep spawned (took {elapsed:.2f}s)"
        )

    async def test_a_throttle_failure_is_not_reported_as_a_close_failure(
        self, caplog,
    ) -> None:
        """The throttle used to sit inside the per-record ``try``, so any
        failure there was logged as ``Catch-up close failed`` for a record
        that had in fact been closed AND persisted — pointing an operator
        at the wrong half of the sweep."""
        tracker = _SpawningTracker(2)
        pending: set[asyncio.Task[None]] = set()
        persisted: list[Interaction] = []

        async def _persist(interaction: Interaction) -> None:
            persisted.append(interaction)

        with caplog.at_level("WARNING"):
            closed = await close_replayed_scopes(
                tracker, _persist, pending_tasks=pending,
                # A degenerate cap: the guard must not turn an empty task
                # set into ``asyncio.wait(set())`` -> ValueError.
                max_in_flight=0,
            )

        assert closed == 2
        assert len(persisted) == 2
        assert "Catch-up close failed" not in caplog.text

    async def test_derive_channels_gates_per_channel(self) -> None:
        tracker = InteractionTracker()
        tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True,
            replay_attributed=True, source_channel_id="group:done",
        )
        tracker.add_turn(
            SCOPE, speaker_id="bob", replayed=True,
            replay_attributed=True, source_channel_id="group:cut-short",
        )
        derived: list[str] = []

        async def _persist(interaction: Interaction) -> None:
            derived.append(interaction.speaker_id)

        closed = await close_replayed_scopes(
            tracker, _persist, derive_channels=frozenset({"group:done"}),
        )
        assert closed == 2, "both records are popped either way"
        assert derived == ["alice"], (
            "only the channel whose replay FINISHED may derive — a record "
            "holding a prefix would claim a span identity no later boot "
            "can recompute"
        )

    async def test_a_record_with_no_channel_never_derives_under_a_gate(
        self,
    ) -> None:
        tracker = InteractionTracker()
        tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True, replay_attributed=True,
        )
        derived: list[str] = []

        async def _persist(interaction: Interaction) -> None:
            derived.append(interaction.speaker_id)

        await close_replayed_scopes(
            tracker, _persist, derive_channels=frozenset({"group:done"}),
        )
        assert derived == [], (
            "unattributable to a channel is unattributable to a completed one"
        )


# ─── The record's own epoch at close (PR B2 review finding 3) ──────────


class _EpochSpyEpisodic:
    """Records the AMBIENT epoch each write would resolve."""

    def __init__(self) -> None:
        self.stamped: list[str] = []

    def active_epoch_id(self) -> str:
        return resolve_epoch_id_silent()

    async def has_episode_for_interaction(self, interaction_id: str) -> bool:
        return False

    async def store_episode(self, **kwargs: object) -> str:
        self.stamped.append(resolve_epoch_id_silent())
        return "ep-1"


@pytest.mark.asyncio
async def test_the_close_binds_the_records_own_epoch_not_the_closers(
) -> None:
    """The epoch twin of the ISSUE-0123 principal binding.

    Every tier resolves its epoch AMBIENT, and since the catch-up split a
    record is routinely closed from inside ANOTHER request's scope — a
    replayed event carries no epoch key at all.  Without this binding
    ``store_episode`` stamped whatever epoch the closer happened to be
    under, and epoch recall filters with strict equality and no carve-out,
    so the conversation became unreadable from the epoch that produced it.
    """
    tracker = InteractionTracker()
    with epoch_scope("job-7"):
        record = tracker.add_turn(
            SCOPE, payload={"summary": "s", "text": "hello"},
            speaker_id="bob",
        )
    assert record.epoch_id == "job-7", "frozen at open"

    closed = tracker.close_record(record, reason=REASON_STRUCTURAL)
    assert closed is not None

    episodic = _EpochSpyEpisodic()
    pending: set[asyncio.Task[None]] = set()

    async def _on_finalized() -> None:
        return None

    # Closed from a DIFFERENT request's scope, exactly as the room fans,
    # ``idle_check`` and the catch-up split all do.
    with epoch_scope("job-9"):
        await persist_closed_interaction(
            episodic=episodic,  # type: ignore[arg-type]
            llm_client=None,  # type: ignore[arg-type]
            memory_ns=None,  # type: ignore[arg-type]
            agent_id="ember-owl", interaction=closed,
            pending_tasks=pending, on_finalized=_on_finalized,
        )
    for task in list(pending):
        task.cancel()

    assert episodic.stamped == ["job-7"], (
        "the row must be stamped with the epoch the record was OPENED "
        "under, not the one whichever request closed it happened to hold"
    )
