"""A replayed derivation that FAILED can be retried; one that ran cannot.

Added by the v0.3.15 PR B2 review.  ISSUE-0130 (b) claims its content
digest at Phase 1 — the ``store_episode`` INSERT — while the summary is
written by a background Phase 2 that may never run.  Because the digest is
boot-stable, counting a failed row as "already derived" made one transient
failure permanent: every later boot recomputed the same id, matched the
tombstone, and declined, while the janitor only rewrites the sentinel and
``update_episode_summary`` refuses to overwrite its verdict.  Nothing ever
retried the summary, and the turns only ever lived in memory.

That is the losing direction :mod:`agents.persona_runtime.replay_identity`
refuses everywhere else, so the guard now ignores the janitor's terminal
sentinel and the retry deletes the row it is retrying.  The rest of this
suite pins the three surrounding bounds that make abandoning a Phase 2
safe and cheap.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.interaction_tracker import InteractionTracker
from agents.memory.interactions import (
    SUMMARY_PENDING_TEXT,
    SUMMARY_UNAVAILABLE_TEXT,
)
from agents.persona_runtime.finalize_close import (
    DRAIN_TIMEOUT_SEC,
    drain_pending_summary_tasks,
)
from agents.persona_runtime.replay_sweep import (
    REPLAY_SUMMARIZE_MAX_IN_FLIGHT,
    gated_replay_finalize,
    replay_summarize_gate,
)

DIGEST = "replay-" + "a" * 64


async def _store(memory: EpisodicMemory, summary: str) -> None:
    await memory.store_episode(
        summary=summary, context={"scope": "group:planning"},
        interaction_id=DIGEST, scope="group:planning",
    )


@pytest.mark.asyncio
class TestAFailedDerivationIsRetriable:
    async def test_the_janitors_verdict_does_not_count_as_derived(
        self, memory: EpisodicMemory,
    ) -> None:
        await _store(memory, SUMMARY_UNAVAILABLE_TEXT)
        assert await memory.has_episode_for_interaction(DIGEST) is False, (
            "a tombstone is a record that the derivation FAILED, not that "
            "it happened — counting it made one boot-path hiccup cost the "
            "span its memory on every later boot"
        )

    async def test_the_retry_deletes_the_row_it_retries(
        self, memory: EpisodicMemory,
    ) -> None:
        """Or the tombstones accumulate under one digest.

        ``update_episode_summary`` matches ``(agent_id, interaction_id)``
        with no ``LIMIT`` and reports ``rowcount``, so a digest has to stay
        effectively single-rowed.
        """
        await _store(memory, SUMMARY_UNAVAILABLE_TEXT)
        assert await memory.clear_failed_episode(DIGEST) == 1
        assert await memory.clear_failed_episode(DIGEST) == 0, "idempotent"

        db = memory._ensure_db()
        async with db.execute(
            "SELECT COUNT(*) FROM episodes WHERE interaction_id = ?",
            (DIGEST,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None and row[0] == 0

    async def test_a_real_summary_still_blocks_re_derivation(
        self, memory: EpisodicMemory,
    ) -> None:
        """The guard's whole job, unchanged."""
        await _store(memory, "they agreed on the Q3 budget")
        assert await memory.has_episode_for_interaction(DIGEST) is True
        assert await memory.clear_failed_episode(DIGEST) == 0, (
            "a successful derivation is never deleted"
        )

    async def test_a_pending_row_still_blocks_until_the_janitor_rules(
        self, memory: EpisodicMemory,
    ) -> None:
        """Deliberately conservative.

        ``[summary pending]`` is either a Phase 2 in flight or a boot that
        died before one, and the two are indistinguishable from here — so
        the guard waits for ``cleanup_closing_interactions`` to convert it.
        Recovery costs one extra boot instead of racing a live writer.
        """
        await _store(memory, SUMMARY_PENDING_TEXT)
        assert await memory.has_episode_for_interaction(DIGEST) is True
        assert await memory.clear_failed_episode(DIGEST) == 0


@pytest.mark.asyncio
class TestTheBoundsThatMakeAbandoningPhase2Safe:
    async def test_the_gate_is_rebuilt_for_a_new_event_loop(self) -> None:
        """A cached ``asyncio.Semaphore`` is not loop-portable.

        CPython latches the loop on the first CONTENDED acquire, so a
        module-global cache survives every low-concurrency test and then
        fails exactly under the boot burst it exists to bound — as an
        exception inside a Phase-2 task that nobody retrieves.
        """
        first = replay_summarize_gate()
        assert replay_summarize_gate() is first, "stable within one loop"

        # A second loop must not inherit the first loop's semaphore.
        second = await asyncio.get_running_loop().run_in_executor(
            None, lambda: asyncio.run(_gate_in_a_fresh_loop()),
        )
        assert second != id(first), (
            "a new loop gets its own gate; sharing one strands its waiters"
        )

    async def test_a_gate_failure_does_not_escape_the_phase_2_task(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``finalize_closed_interaction`` is guarded; the acquire was not.

        Wrapping it made ``gated_replay_finalize`` the task's outermost
        frame, so anything the acquire raised bypassed that guard — and
        ``close_path``'s ``add_done_callback`` only discards the task
        without reading its exception, making it a silently skipped Phase 2
        on a span whose digest is already claimed.
        """
        class _Exploding:
            async def __aenter__(self):
                raise RuntimeError("bound to a different event loop")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            "agents.persona_runtime.replay_sweep.replay_summarize_gate",
            lambda: _Exploding(),
        )
        ran: list[bool] = []

        async def _phase_two() -> None:
            ran.append(True)

        # Must not raise — the task is fire-and-forget.
        await gated_replay_finalize(True, _phase_two)
        assert ran == [], "Phase 2 never ran, and that is now visible"

    async def test_a_gate_failure_still_propagates_cancellation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancelled shutdown drain is not a failure to swallow."""
        class _Cancelling:
            async def __aenter__(self):
                raise asyncio.CancelledError

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            "agents.persona_runtime.replay_sweep.replay_summarize_gate",
            lambda: _Cancelling(),
        )
        with pytest.raises(asyncio.CancelledError):
            await gated_replay_finalize(True, _never)

    async def test_the_shutdown_drain_is_bounded(self) -> None:
        """``close_memory`` awaits this while holding the agent lock.

        Until this release the sweep derived nothing and so spawned
        nothing; it now takes one Phase-2 task per replayed speaker per
        room, paced ``REPLAY_SUMMARIZE_MAX_IN_FLIGHT`` at a time, so an
        unbounded drain blocked ``on_event`` for as long as the backlog
        took.  Bounding it is safe because an abandoned Phase 2 leaves a
        ``[summary pending]`` row the janitor converts and the guard above
        now lets the next boot re-derive.
        """
        assert DRAIN_TIMEOUT_SEC > 0
        stuck = asyncio.create_task(_never())
        pending = {stuck}

        await asyncio.wait_for(
            drain_pending_summary_tasks(pending, timeout=0.05), timeout=2.0,
        )
        assert stuck.cancelled(), (
            "the drain cancels what it abandons, so nothing can still "
            "touch the DB handle the caller is about to close"
        )

    async def test_an_unbounded_drain_is_still_available(self) -> None:
        """``timeout=None`` keeps the pre-review behaviour for callers
        that genuinely must wait (and for the existing tests of it)."""
        done = asyncio.create_task(asyncio.sleep(0))
        await drain_pending_summary_tasks({done}, timeout=None)
        assert done.done() and not done.cancelled()


class TestTheLiveIdSetIsBoundedByThePass:
    """``_live_message_ids`` answers one question, for one interval.

    A replayed turn asks whether this boot already ingested that wire id
    live.  Once the pass is over nothing replayed can arrive again, so the
    set has no reader — but the WRITER used to run for the process
    lifetime, retaining one id per message the persona had ever ingested,
    with nothing to bound or free it.
    """

    def test_live_ids_are_recorded_while_the_pass_is_open(self) -> None:
        tracker = InteractionTracker()
        assert tracker.observe_wire_message("m1", replayed=False) is False
        assert tracker.observe_wire_message("m1", replayed=True) is True, (
            "the same-boot live/replay overlap is still detected"
        )

    def test_live_ids_are_not_recorded_once_the_pass_closes(self) -> None:
        tracker = InteractionTracker()
        tracker.clear_replay_pass_state()

        for i in range(1000):
            tracker.observe_wire_message(f"m{i}", replayed=False)

        assert tracker._live_message_ids == set(), (
            "steady-state traffic must not accumulate in a set nothing "
            "will read again"
        )


async def _gate_in_a_fresh_loop() -> int:
    """``id()`` of the gate a brand-new event loop builds."""
    import agents.persona_runtime.replay_sweep as sweep

    assert sweep.REPLAY_SUMMARIZE_MAX_IN_FLIGHT == REPLAY_SUMMARIZE_MAX_IN_FLIGHT
    return id(sweep.replay_summarize_gate())


async def _never() -> None:
    await asyncio.Event().wait()
