"""The on-startup catch-up sweep — ISSUE-0130's pass-end close (PR B2).

Split out of :mod:`agents.persona_runtime.close_path` (v0.3.15 PR B2
review), which the review's fixes pushed past the 500-line cap
``scripts/checks/file_size.py --strict`` enforces.  The seam is a real
one rather than a line count: ``close_path`` owns what closing ONE record
costs, while this module owns the BOOT-PATH loop over many of them — the
only close fan in the tree that runs before the agent is serving its
first live turn, and therefore the only one whose cost is measured in
startup latency rather than in per-turn latency.

That is what its two knobs are about (``max_in_flight`` and
``throttle_budget_sec``), and why they live here and not beside the
two-phase write they pace.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..memory.boundary_detectors import REASON_CATCHUP_COMPLETE

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker

__all__ = ["close_replayed_scopes"]

logger = logging.getLogger(__name__)


#: Ceiling on Phase-2 summarise tasks the boot sweep leaves in flight at
#: once.  Until v0.3.15 PR B2 the sweep derived nothing, so it spawned
#: nothing; now every ATTRIBUTED replayed record runs the full two-phase
#: write, and the ``(principal, speaker, scope)`` re-key makes that one
#: record per SENDER per room — so an unbounded sweep fires K×N provider
#: calls the instant a persona boots, unmetered (a catch-up close is not a
#: wire-bounded close, so it draws no wallet lease) and straight into the
#: RFC 0009 limiter that a restart does not reset.  The cap turns the burst
#: into a bounded pipeline; it does not reduce the total work.
_REPLAY_SUMMARIZE_MAX_IN_FLIGHT: int = 4

#: Total wall-clock the sweep may spend WAITING on those tasks, across the
#: whole pass (PR B2 review).  Pacing the burst is worth some boot time;
#: it is not worth unbounded boot time, and the first cut had no bound at
#: all.  ``replay_channel_history`` runs under a 60 s
#: ``asyncio.wait_for`` precisely so a slow orchestrator cannot stall
#: boot — but this sweep runs in the caller's ``finally``, OUTSIDE that
#: budget, and each task it waits on holds a
#: ``SUMMARIZATION_TIMEOUT_SEC`` (30 s) LLM round trip.  At four in
#: flight that made the boot tail grow with the replayed-record count,
#: and ``AgentServer.start`` arms the ISSUE-0125 re-registration watcher
#: only AFTER catch-up returns — so the persona stays unregistered, and
#: silently mute to an orchestrator restart, for the whole of it.
#:
#: When the budget is spent the sweep stops WAITING, not working: every
#: record is still closed and still persisted, and the Phase-2 tasks
#: still run.  Only the pacing is dropped, which is the part that was
#: costing boot.
_REPLAY_SUMMARIZE_THROTTLE_BUDGET_SEC: float = 20.0


async def close_replayed_scopes(
    tracker: InteractionTracker,
    persist: Callable[[Interaction], Awaitable[None]],
    *,
    derive_channels: frozenset[str] | None = None,
    pending_tasks: set[asyncio.Task[None]] | None = None,
    max_in_flight: int = _REPLAY_SUMMARIZE_MAX_IN_FLIGHT,
    throttle_budget_sec: float = _REPLAY_SUMMARIZE_THROTTLE_BUDGET_SEC,
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
    the number of records closed (since the v0.3.15 ``(principal,
    speaker, scope)`` re-key one scope may hold several replay-opened
    records — one per replayed sender).  Best-effort by contract — it runs on the
    boot path, so a per-scope failure is logged and the sweep continues
    rather than propagating; the caller may treat it as non-raising.  Note
    the scope is popped before the persist call, so even a failing persist
    leaves the tracker in the state live traffic depends on.

    Deliberately runs WITHOUT the agent lock: a persona holds ``_lock`` for
    a whole event (LLM calls included), so grabbing it here would stall boot
    behind an in-flight turn.  Safe unlocked because ``replayed`` is frozen
    at open, both this sweep and the ingest-time split tolerate a ``close``
    that returns ``None`` (the other got there first), and a live turn can
    never be inside a flagged span — the split guarantees it opens its own.

    What ``persist`` costs here changed with v0.3.15 PR B2 and the argument
    above no longer covers all of it.  For an ATTRIBUTED replayed span the
    call is a real ``store_episode`` INSERT plus a background summarise /
    extract task, so this sweep now writes and spawns rather than only
    manipulating tracker state.  Two consequences worth naming:

    * ``max_in_flight`` bounds the spawn, because one record per replayed
      SENDER per room otherwise means an unmetered provider burst at boot;
      ``throttle_budget_sec`` bounds what that pacing may cost the boot
      path, because this runs outside the catch-up wall-clock budget.
      The cap counts the tasks THIS SWEEP spawned, not ``pending_tasks``
      itself — that set is shared with the live close path, which is
      serving throughout, so measuring it both blocked the sweep on
      unrelated live closes and released a slot when one of those landed
      while every replay task was still running (PR B2 review).
    * the Phase-2 tasks register into ``pending_tasks`` from OUTSIDE
      ``_lock``, while ``close_memory`` drains that set under it — so a
      shutdown racing this sweep could snapshot the set before a late task
      joins.  Not reachable today (``server_cli`` starts and stops strictly
      in sequence, so a shutdown cannot overlap catch-up); a refactor that
      makes them concurrent must switch the drain to loop-until-empty,
      which ``drain_pending_summaries`` already documents as its condition.

    ``derive_channels`` is which channels' replay actually FINISHED —
    ``None`` means "no completeness information, derive everything" (the
    default every direct caller and test wants).  It is a set rather than
    the boolean the first cut used because completeness is per CHANNEL
    while that boolean was per AGENT, and it was wrong in both directions
    (PR B2 review): a budget overrun in the ninth channel discarded the
    eight windows that had already completed, and a channel whose replay
    aborted mid-window still reported the whole pass complete.
    """
    closed = 0
    # The tasks THIS sweep spawned — see ``max_in_flight`` above for why
    # it is not ``pending_tasks``.
    spawned: set[asyncio.Task[None]] = set()
    throttle_deadline = time.monotonic() + throttle_budget_sec
    # Per RECORD since the ``(principal, speaker, scope)`` re-key
    # (v0.3.15 residuals PR 3): replay can open several records in one
    # scope — one per replayed sender — and every one of them carries
    # the ``replayed`` flag, so the sweep walks records, not scopes.
    for interaction in list(tracker.open_records()):
        if not interaction.replayed:
            continue
        try:
            popped = tracker.close_record(
                interaction, reason=REASON_CATCHUP_COMPLETE,
            )
            if popped is None:
                continue
            closed += 1
            # ``persist`` is the full two-phase write for an ATTRIBUTED
            # replayed span as of v0.3.15 PR B2 — a ``store_episode``
            # INSERT plus a background summarise/extract task.  It is a
            # no-op only for the unattributable ones (which still skip on
            # ``replay_attributed``) and for a span an earlier boot
            # already derived.  One close→persist contract either way.
            if derive_channels is not None and (
                popped.source_channel_id or ""
            ) not in derive_channels:
                # This channel's replay did not FINISH, so the record
                # holds a PREFIX of its window.  Pop without persisting:
                # the span identity is built from the turns the record
                # holds, so deriving a prefix claims an id no later boot
                # can recompute and the next complete boot re-derives the
                # whole window on top of it.  Catch-up re-reads the same
                # window every boot anyway (no watermark, RFC 0011 OQ #8),
                # so this costs one boot's derivation, not the memory.
                # A record with no ``source_channel_id`` is treated the
                # same way — unattributable to a channel is unattributable
                # to a completed one.
                continue
            before = set(pending_tasks) if pending_tasks is not None else set()
            await persist(popped)
            if pending_tasks is not None:
                spawned |= set(pending_tasks) - before
        except Exception:
            logger.warning(
                "Catch-up close failed for replayed scope %s",
                interaction.scope, exc_info=True,
            )
            continue
        # Pace the burst — deliberately OUTSIDE the close/persist ``try``
        # (PR B2 review).  A failure here is a throttle failure, not a
        # close failure: the record above was closed AND persisted, and
        # reporting it as "Catch-up close failed" pointed an operator at
        # the wrong half of the sweep.
        spawned = {task for task in spawned if not task.done()}
        if not spawned or len(spawned) < max_in_flight:
            continue
        remaining = throttle_deadline - time.monotonic()
        if remaining <= 0:
            # Budget spent.  Keep closing and persisting; just stop
            # paying boot time to pace.
            continue
        try:
            await asyncio.wait(
                spawned, timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            logger.warning(
                "Catch-up summarise throttle failed; continuing unpaced",
                exc_info=True,
            )
    return closed
