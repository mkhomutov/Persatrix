"""The on-startup catch-up sweep — ISSUE-0130's pass-end close (PR B2).

Split out of :mod:`agents.persona_runtime.close_path` (v0.3.15 PR B2
review), which the review's fixes pushed past the 500-line cap
``scripts/checks/file_size.py --strict`` enforces.  The seam is a real
one rather than a line count: ``close_path`` owns what closing ONE record
costs, while this module owns the BOOT-PATH loop over many of them — the
only close fan in the tree that runs before the agent is serving its
first live turn, and therefore the only one whose cost is measured in
startup latency rather than in per-turn latency.

That is why :data:`REPLAY_SUMMARIZE_MAX_IN_FLIGHT` lives here and not
beside the two-phase write it bounds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

from ..memory.boundary_detectors import REASON_CATCHUP_COMPLETE

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker

__all__ = [
    "REPLAY_SUMMARIZE_MAX_IN_FLIGHT",
    "close_replayed_scopes",
    "gated_replay_finalize",
    "replay_summarize_gate",
]

logger = logging.getLogger(__name__)


#: Ceiling on Phase-2 summarise calls a replayed close may have IN FLIGHT
#: at once, process-wide.  Until v0.3.15 PR B2 the sweep derived nothing,
#: so it spawned nothing; now every DERIVABLE replayed record runs the full
#: two-phase write, and the ``(principal, speaker, scope)`` re-key makes
#: that one record per SENDER per room — so an unbounded boot fires K×N
#: provider calls the instant a persona starts, unmetered (a catch-up close
#: is not a wire-bounded close, so it draws no wallet lease) and straight
#: into the RFC 0009 limiter that a restart does not reset.
REPLAY_SUMMARIZE_MAX_IN_FLIGHT: int = 4

_gate: asyncio.Semaphore | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None


def replay_summarize_gate() -> asyncio.Semaphore:
    """The process-wide cap on in-flight replay-derived summarisations.

    Held by the Phase-2 TASK, not by the sweep loop — and that is the
    whole correction (PR B2 review).  The first cut enforced the cap by
    ``await``\\ ing inside the sweep, which was wrong twice over:

    * it made the cap cost BOOT TIME.  ``replay_channel_history`` runs
      under a 60 s ``asyncio.wait_for`` precisely so a slow orchestrator
      cannot stall boot, but the sweep runs in that caller's ``finally``,
      OUTSIDE the budget, and each task it waited on holds a
      ``SUMMARIZATION_TIMEOUT_SEC`` (30 s) LLM round trip.  Measured: 5 s
      of blocked boot at four replayed records, 10 s at eight, and the
      whole 20 s ceiling at twenty — during which ``AgentServer.start``
      has not yet armed the ISSUE-0125 re-registration watcher, so an
      orchestrator restart in that window goes unnoticed.
    * and to bound that cost it took a wall-clock budget, which then
      DEFEATED the cap.  The budget was anchored at sweep start and so
      counted time spent working — a ``store_episode`` INSERT per record —
      not only time spent waiting; once it expired the pacing was dropped
      entirely rather than degraded.  Measured at the shipped defaults:
      87 concurrent summarise calls against a cap of 4.  A slow disk alone
      was enough, with no slow provider involved at all.

    A semaphore the task itself acquires has neither failure mode.  The
    sweep never blocks, so boot pays only its INSERTs (measured ~0.4 ms
    each); the cap holds for the whole pass because nothing can expire it;
    and the queue behind it is cheap — a coroutine parked on a semaphore,
    not an open provider connection.  ``drain_pending_summaries`` still
    drains them all at shutdown, four at a time.

    Lazily built because this module is imported at process start, and
    rebuilt when the RUNNING LOOP changes (PR B2 review).  A cached
    ``asyncio.Semaphore`` is not loop-portable: CPython's
    ``_LoopBoundMixin._get_loop()`` latches the loop on the first
    CONTENDED acquire and every later loop then raises ``RuntimeError:
    ... is bound to a different event loop``, while the leaked counter
    strands the next loop's waiters.  Latching only under contention is
    what makes it dangerous — it survives every low-concurrency test and
    fails exactly under the boot burst this cap exists to bound, as an
    exception inside a Phase-2 task that nobody retrieves.  Keying on the
    loop also lets a test patch :data:`REPLAY_SUMMARIZE_MAX_IN_FLIGHT`
    and get the new ceiling instead of the first one ever built.

    ``AgentServer`` warns that it supports one agent per process, so
    process-wide and per-persona are the same ceiling today; where a
    process does host several personas this is the fleet-wide budget,
    which is the more useful of the two.
    """
    global _gate, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate is None or _gate_loop is not loop:
        _gate = asyncio.Semaphore(REPLAY_SUMMARIZE_MAX_IN_FLIGHT)
        _gate_loop = loop
    return _gate


async def gated_replay_finalize(
    gated: bool, phase_two: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    """Run Phase 2, holding the replay summarise gate when it applies.

    ``phase_two`` is a FACTORY, not a coroutine object.  Passing the object
    would build it at the call site, and a task cancelled or dropped before
    it ever reaches the ``await`` would leave that coroutine un-awaited —
    a ``RuntimeWarning`` and, worse, a silently skipped Phase 2 that looks
    like a completed one.  Building it only once the slot is granted makes
    "created" and "will run" the same event.

    Only a REPLAYED close is gated: live closes are wire-bounded and
    metered, and stalling one behind a boot backlog would be a regression.

    The gate acquire is GUARDED, because this coroutine is the task's
    outermost frame and ``finalize_closed_interaction``'s own top-level
    guard therefore no longer covers everything the task can raise (PR B2
    review).  That guard exists so a Phase-2 failure "does not surface as
    ``Task exception was never retrieved`` at GC"; wrapping it moved the
    acquire outside it, and ``close_path``'s ``add_done_callback`` only
    discards the task without reading its exception — so anything raised
    here would have been a silently skipped Phase 2 on a span whose
    re-derivation digest is already claimed.  ``CancelledError`` is
    re-raised: a cancelled shutdown drain is not a failure to swallow.
    """
    if not gated:
        await phase_two()
        return
    try:
        async with replay_summarize_gate():
            await phase_two()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "ISSUE-0130: replay Phase 2 did not run — the summarise gate "
            "failed; the span's row stays pending for the janitor and the "
            "next boot re-derives it",
            exc_info=True,
        )


async def close_replayed_scopes(
    tracker: InteractionTracker,
    persist: Callable[[Interaction], Awaitable[None]],
    *,
    derive_channels: frozenset[str] | None = None,
    speaker_gaps: frozenset[tuple[str, str]] | None = None,
) -> int:
    """Close every scope the on-startup catch-up replay opened (ISSUE-0130).

    Replay events open :class:`InteractionTracker` scopes but carry no
    session-end signal, and catch-up has no synthetic completion event of
    its own (RFC 0011 OQ #8 "lifecycle bleed"), so the scope stayed open
    until the idle timer and the next live turn appended to it.  A merged
    span is wrong in both of the ways the two halves differ: the live half
    would be dropped along with an unattributable replayed one, and the
    replayed half would escape the re-derivation guard, which only a
    ``replayed`` record consults.  The ingest-time split
    (:func:`~agents.persona_runtime.interaction_boundary.stale_close_reason`)
    is the other door on the same boundary; this is the one for scopes no
    live turn reaches.

    Called at the end of the catch-up pass
    (:func:`agents.channel_catchup.replay_for_persona_agents`).  Returns
    the number of records closed (since the v0.3.15 ``(principal, speaker,
    scope)`` re-key one scope may hold several replay-opened records — one
    per replayed sender).  Best-effort by contract — it runs on the boot
    path, so a per-scope failure is logged and the sweep continues rather
    than propagating; the caller may treat it as non-raising.  Note the
    scope is popped before the persist call, so even a failing persist
    leaves the tracker in the state live traffic depends on.

    Deliberately runs WITHOUT the agent lock: a persona holds ``_lock`` for
    a whole event (LLM calls included), so grabbing it here would stall boot
    behind an in-flight turn.  Safe unlocked because ``replayed`` is frozen
    at open, both this sweep and the ingest-time split tolerate a ``close``
    that returns ``None`` (the other got there first), and a live turn can
    never be inside a flagged span — the split guarantees it opens its own.

    **This is the pass-end door onto** ``Interaction
    .replay_window_complete``, the flag ``persist_closed_interaction``
    reads.  One other door may set it — the replay-internal segmentation in
    :func:`~agents.persona_runtime.close_path.close_stale_records`, which
    closes a whole wire conversation mid-pass and applies the same
    ``replay_record_compromised`` test this loop does.  Every REMAINING
    door leaves the default standing.  The first cut kept the decision
    inline here instead, which left it enforced on this door alone: the
    ingest-time split, a wire rotation reaching a non-target record, and
    ``idle_check`` all reach the same two-phase write, and all three derived
    PREFIXES unguarded (PR B2 review).  A prefix claims a span id no later
    boot can recompute, so the next complete boot derives the whole window
    on top of it — the growth curve shape (b) exists to bound, through the
    doors its gate did not cover.  Putting the decision on the record moves
    it to the one chokepoint all four doors funnel through, and makes the
    default (``False``) the safe answer for every door that cannot know.

    FOUR conditions have to hold for a record to derive, and each is a
    different way of not holding a whole window.  All four are in the
    expression below — the fourth used to be in this list only:

    * the record must NAME a channel.  Unattributable to a channel is
      unattributable to a completed one, and the sweep can make no
      completeness claim about a record it cannot match to a pass.
    * ``derive_channels`` — which channels' replay actually FINISHED.
      ``None`` means "no completeness information, derive everything" (the
      default every direct caller and test wants).  It is a set rather than
      the boolean the first cut used because completeness is per CHANNEL
      while that boolean was per AGENT, and it was wrong in both directions:
      a budget overrun in the ninth channel discarded the eight windows that
      had already completed, and a channel whose replay aborted mid-window
      still reported the whole pass complete.
    * ``tracker.replay_record_compromised(channel, speaker)`` — whether this
      window was already cut short by some other door, or holed by a row
      that raised on the way in.  If it was, what is still open is the
      REMAINDER of a cut window, no more derivable than the prefix that was
      cut off it.  Keyed ``(channel, speaker)`` since the PR B2 review: the
      channel-wide spelling let one live turn racing catch-up in a busy room
      refuse every OTHER speaker's complete window there, on every boot.
      Read LIVE rather than snapshotted, which the flag-before-close
      handoff below makes safe.
    * ``speaker_gaps`` — the same raised-row fact as the catch-up pass
      recorded it, kept as a parameter for direct callers that drive the
      sweep without a tracker-fed pass.  That hole is in one speaker's
      record and no one else's, since records are keyed per speaker;
      disqualifying the whole channel for it meant one deterministically
      raising row cost every other speaker in that room their derivation,
      on every boot (PR B2 review round 3).  A gap the catch-up loop could
      not attribute to a sender takes the channel out of ``derive_channels``
      instead.

    Refusing costs one boot's derivation, which catch-up re-reads on the next
    boot anyway (no watermark, RFC 0011 OQ #8).  Deriving a prefix costs a
    duplicate episode nothing can ever match again.
    """
    closed = 0
    # Per RECORD since the ``(principal, speaker, scope)`` re-key
    # (v0.3.15 residuals PR 3): replay can open several records in one
    # scope — one per replayed sender — and every one of them carries
    # the ``replayed`` flag, so the sweep walks records, not scopes.
    for interaction in list(tracker.open_records()):
        if not interaction.replayed:
            continue
        try:
            channel = interaction.source_channel_id or ""
            # Decided BEFORE the close and handed to it, so the record
            # cannot self-register as a truncation on the way out and the
            # compromised set can be read LIVE (PR B2 review).  The
            # previous shape — close, then assign — needed a snapshot
            # taken before the loop to stay correct, which in turn could
            # not see a truncation landing during this loop's own
            # ``await persist(...)`` while dispatch was serving.
            derivable = (
                # Unattributable to a channel is unattributable to a
                # COMPLETED channel: the sweep can make no completeness
                # claim about a record that names none.  Stated in this
                # docstring since the first cut, but enforced only by
                # ``"" in derive_channels`` being false — which said
                # nothing at all on the documented ``None`` path.
                bool(channel)
                and (derive_channels is None or channel in derive_channels)
                and not tracker.replay_record_compromised(
                    channel, interaction.speaker_id,
                )
                and (
                    speaker_gaps is None
                    or (channel, interaction.speaker_id) not in speaker_gaps
                )
            )
            popped = tracker.close_record(
                interaction, reason=REASON_CATCHUP_COMPLETE,
                replay_window_complete=derivable,
            )
            if popped is None:
                continue
            closed += 1
            # ``persist`` is the full two-phase write for a DERIVABLE
            # attributed replayed span as of v0.3.15 PR B2 — a
            # ``store_episode`` INSERT plus a background summarise/extract
            # task, the latter paced by :func:`replay_summarize_gate`.  It
            # is a no-op for the unattributable ones (which still skip on
            # ``replay_attributed``), for the ones this loop just marked
            # incomplete, and for a span an earlier boot already derived.
            # One close→persist contract for all four.
            await persist(popped)
        except Exception:
            logger.warning(
                "Catch-up close failed for replayed scope %s",
                interaction.scope, exc_info=True,
            )
            continue
    # Both facts are scoped to ONE PASS: this sweep's own closes must not
    # disqualify the next catch-up's windows (a reconnect-triggered
    # re-catch-up in the same process is exactly that, RFC 0011 OQ #8), and
    # once catch-up is over no turn can be a replayed duplicate.
    tracker.clear_replay_pass_state()
    return closed
