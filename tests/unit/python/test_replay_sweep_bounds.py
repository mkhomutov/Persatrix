"""What the ISSUE-0130 boot sweep may cost, and what it may not.

Split out of ``test_replay_boundary_and_sweep.py`` (v0.3.15 PR B2 review)
when that file crossed the 500-line cap ``scripts/checks/file_size.py``
enforces.  The seam matches the modules under test: the boundary suite
pins :mod:`agents.persona_runtime.interaction_boundary` — WHERE a replayed
span ends — while this one pins :mod:`agents.persona_runtime.replay_sweep`
— what closing the survivors costs the boot path, and which of them may
derive at all.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.memory.interaction_tracker import InteractionTracker
from agents.memory.interaction_types import Interaction
from agents.persona_runtime.replay_sweep import (
    REPLAY_SUMMARIZE_MAX_IN_FLIGHT,
    close_replayed_scopes,
    gated_replay_finalize,
    replay_summarize_gate,
)

SCOPE = "group:planning"


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
    """What the boot sweep may cost, and what it may not.

    The sweep runs in ``replay_for_persona_agents``'s ``finally``, OUTSIDE
    the 60 s catch-up ``wait_for``, and ``AgentServer.start`` arms the
    ISSUE-0125 re-registration watcher only after catch-up returns — so
    every second the sweep spends is a second the persona cannot notice an
    orchestrator restart.  The first cut enforced its concurrency cap by
    WAITING in this loop, which paid exactly that (measured 5 s of blocked
    boot at four records, 20 s at twenty), and then took a wall-clock
    budget to bound the damage — which, being anchored at sweep start,
    counted time spent WORKING and expired before it had waited at all,
    dropping the cap entirely (measured 87 concurrent calls against a cap
    of 4).  The cap now lives on the Phase-2 task instead.
    """

    async def test_the_sweep_never_waits_on_the_tasks_it_spawns(
        self,
    ) -> None:
        """No amount of hung provider work may hold boot."""
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
        closed = await close_replayed_scopes(tracker, _persist)
        elapsed = loop.time() - started

        for task in list(pending):
            task.cancel()
        assert closed == 6, "every record is still closed and persisted"
        assert elapsed < 0.5, (
            "the sweep must not pace by waiting — boot pays only its "
            f"INSERTs (took {elapsed:.2f}s)"
        )

    async def test_the_gate_bounds_concurrent_summarisations(self) -> None:
        """The cap the sweep no longer enforces is enforced by the task.

        ``REPLAY_SUMMARIZE_MAX_IN_FLIGHT`` is a real ceiling for the whole
        pass, not a target that a budget can expire: the semaphore is held
        by the Phase-2 coroutine, so nothing can hand out a fifth slot.
        """
        gate = replay_summarize_gate()
        live = 0
        peak = 0
        release = asyncio.Event()

        async def _phase_two() -> None:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await release.wait()
            live -= 1

        tasks = [
            asyncio.create_task(gated_replay_finalize(True, _phase_two))
            for _ in range(40)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        observed = peak
        release.set()
        await asyncio.gather(*tasks)

        assert observed <= REPLAY_SUMMARIZE_MAX_IN_FLIGHT, (
            f"{observed} summarise calls in flight against a cap of "
            f"{REPLAY_SUMMARIZE_MAX_IN_FLIGHT} — the boot burst goes "
            "unmetered into the RFC 0009 limiter a restart does not reset"
        )
        assert gate.locked() is False, "every slot is returned"

    async def test_a_live_close_is_not_paced_behind_the_boot_backlog(
        self,
    ) -> None:
        """Only a REPLAYED close is gated.

        Live closes are wire-bounded and metered; stalling one behind a
        boot backlog of replayed summarisations would be a regression, so
        ``gated_replay_finalize`` takes the flag rather than gating all.
        """
        release = asyncio.Event()
        ran = False

        async def _blocker() -> None:
            await release.wait()

        async def _live() -> None:
            nonlocal ran
            ran = True

        held = [
            asyncio.create_task(gated_replay_finalize(True, _blocker))
            for _ in range(REPLAY_SUMMARIZE_MAX_IN_FLIGHT)
        ]
        await asyncio.sleep(0)
        await gated_replay_finalize(False, _live)
        assert ran, "a live close must not queue behind replayed ones"

        release.set()
        await asyncio.gather(*held)

    async def test_derive_channels_gates_per_channel(self) -> None:
        """The sweep DECIDES; ``persist_closed_interaction`` enforces.

        Since the PR B2 review the decision is a frozen field on the record
        rather than a ``continue`` in this loop, because the sweep is only
        one of four doors onto a replayed record and the other three were
        deriving prefixes unguarded.  So the assertion is on the flag, not
        on whether ``persist`` was reached: every record still goes through
        the one close→persist contract.
        """
        tracker = InteractionTracker()
        tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True,
            replay_attributed=True, source_channel_id="group:done",
        )
        tracker.add_turn(
            SCOPE, speaker_id="bob", replayed=True,
            replay_attributed=True, source_channel_id="group:cut-short",
        )
        seen: dict[str, bool] = {}

        async def _persist(interaction: Interaction) -> None:
            seen[interaction.speaker_id] = interaction.replay_window_complete

        closed = await close_replayed_scopes(
            tracker, _persist, derive_channels=frozenset({"group:done"}),
        )
        assert closed == 2, "both records are popped either way"
        assert seen == {"alice": True, "bob": False}, (
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
        seen: list[bool] = []

        async def _persist(interaction: Interaction) -> None:
            seen.append(interaction.replay_window_complete)

        await close_replayed_scopes(
            tracker, _persist, derive_channels=frozenset({"group:done"}),
        )
        assert seen == [False], (
            "unattributable to a channel is unattributable to a completed one"
        )

    async def test_a_channel_another_door_already_cut_does_not_derive(
        self,
    ) -> None:
        """The TAIL half of the same hazard (PR B2 review).

        When a live turn splits a replay-opened record mid-window, the
        prefix is refused by the completeness gate — but replay then opens
        a SECOND record for the rest of the window, and at pass end its
        channel is legitimately in ``derive_channels``.  Deriving that tail
        claims a digest no uninterrupted boot recomputes, exactly like the
        prefix.  The tracker counts replayed closes per channel so the
        sweep can tell "nothing has cut this window" from "I am looking at
        what is left of one".
        """
        tracker = InteractionTracker()
        prefix = tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True,
            replay_attributed=True, source_channel_id="group:planning",
        )
        # The ingest-time split: a live turn closes the prefix mid-window.
        assert tracker.close_record(prefix, reason="catchup_complete")
        # Replay resumes and opens the tail under the same key.
        tracker.add_turn(
            SCOPE, speaker_id="alice", replayed=True,
            replay_attributed=True, source_channel_id="group:planning",
        )
        seen: list[bool] = []

        async def _persist(interaction: Interaction) -> None:
            seen.append(interaction.replay_window_complete)

        await close_replayed_scopes(
            tracker, _persist,
            derive_channels=frozenset({"group:planning"}),
        )
        assert seen == [False], (
            "the remainder of a window that was already cut is no more "
            "derivable than the prefix cut off it"
        )



# ─── The record's own epoch at close (PR B2 review finding 3) ──────────


@pytest.mark.asyncio
class TestCompletenessIsPerSpeakerNotPerRoom:
    """A raising row leaves a hole in ONE speaker's record.

    Records are keyed `(principal, speaker, scope)`, so a row that raised
    inside ``on_event`` can only have gapped the record its own sender's
    turns land in. The first cut disqualified the whole CHANNEL for it —
    and because such a failure is deterministic (the ``except`` calls
    reaching it "a programming error somewhere upstream"), the same row
    raised on every boot, so that room's replayed memory was never derived
    for anyone, with nothing to distinguish it from replay having stopped.
    """

    async def test_a_gap_blocks_only_its_own_speaker(self) -> None:
        tracker = InteractionTracker()
        for speaker in ("alice", "bob"):
            tracker.add_turn(
                SCOPE, speaker_id=speaker, replayed=True,
                replay_attributed=True, source_channel_id="group:planning",
            )
        seen: dict[str, bool] = {}

        async def _persist(interaction: Interaction) -> None:
            seen[interaction.speaker_id] = interaction.replay_window_complete

        await close_replayed_scopes(
            tracker, _persist,
            derive_channels=frozenset({"group:planning"}),
            speaker_gaps=frozenset({("group:planning", "alice")}),
        )
        assert seen == {"alice": False, "bob": True}, (
            "alice's window has a hole and must not derive; bob's replayed "
            "cleanly and must not be punished for it"
        )


def test_an_unattributable_gap_takes_the_channel_down() -> None:
    """The one case that still costs the whole room.

    ``_replay_channel_history_inner`` can only name the gapped speaker when
    the raising row carries a readable ``sender_id``. Without one the hole
    could be in any record, so the channel leaves ``completed`` instead —
    the conservative direction, and the only one available.
    """
    from agents.channel_replay_outcome import ReplayPassOutcome

    outcome = ReplayPassOutcome()
    assert outcome.completed == set()
    assert outcome.speaker_gaps == set()
    outcome.speaker_gaps.add(("group:planning", "alice"))
    outcome.completed.add("group:planning")
    assert ("group:planning", "alice") in outcome.speaker_gaps, (
        "the two axes are independent: a channel can finish its window and "
        "still owe one speaker a gap"
    )
