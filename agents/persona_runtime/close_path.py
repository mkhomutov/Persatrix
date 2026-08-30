"""RFC 0020 PR 4 close-path two-phase write (extracted helper).

Houses the body of ``_EpisodeRoutingMixin._persist_closed_interaction`` as a
free function so :mod:`agents.persona_runtime.episode_routing` stays under the
500-line file-size cap enforced by ``scripts/checks/file_size.py --strict``.
The mixin keeps a thin delegating method (the public seam tests patch / call);
all the orchestration lives here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from ..memory.boundary_detectors import REASON_CATCHUP_COMPLETE
from ..memory.episodic import EpisodicMemory
from ..memory.interactions import (
    SUMMARY_PENDING_TEXT,
    Interaction,
    InteractionTracker,
)
from ..principal_id import principal_scope
from .classification import normalize_for_stamp
from .close_entries import own_turn_items
from .finalize_close import finalize_closed_interaction
from .interaction_boundary import stale_close_reason

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from ..persona_types import AgentEvent
    from . import MemoryNamespace

logger = logging.getLogger(__name__)

__all__ = [
    "close_replayed_scopes",
    "close_stale_records",
    "persist_closed_interaction",
    "persist_fanned_closes",
]


async def persist_fanned_closes(
    closed_records: Iterable[Interaction],
    persist: Callable[[Interaction], Awaitable[None]],
) -> None:
    """Persist every record a room-wide close fan just closed — one
    guard per record (v0.3.15 PR 3 review fix).

    The ISSUE-0123 part 3 fans (``close_scope`` at the session-end,
    cost, end-vote and close-notification sites) pop ALL their records
    from the tracker before the first persist runs, so an exception
    escaping one record's persist would silently discard the rest —
    closed, gone from the open map, and with no idle sweep left to find
    them.  Mirror of the per-iteration guard on the idle-flush loops
    (``episode_routing._store_event_episode``, PR-3 review #13): each
    record gets its own ``try`` so a failure is logged with the
    identity of the record that owned it and the fan keeps persisting
    the siblings.
    """
    for interaction in closed_records:
        try:
            await persist(interaction)
        except Exception:
            logger.warning(
                "Failed to persist fanned close (scope=%s, "
                "interaction_id=%s, close_reason=%s)",
                interaction.scope, interaction.interaction_id,
                interaction.close_reason, exc_info=True,
            )


async def close_stale_records(
    tracker: InteractionTracker,
    scope: str,
    event: AgentEvent,
    *,
    wire_id: str,
    persist: Callable[[Interaction], Awaitable[None]],
) -> None:
    """Close every record in ``scope`` this event makes stale, then persist.

    The ingest-time boundary fan, extracted from
    ``_EpisodeRoutingMixin._handle_multi_turn_event`` (PR #846 review —
    that module is at the 500-line cap, and this is a close+guarded-persist
    fan, which is this module's subject).

    :func:`~agents.persona_runtime.interaction_boundary.stale_close_reason`
    owns WHICH boundary fired: the RFC 0030 wire-id rotation (the previous
    channel conversation ended, so the new turn must open fresh under the
    wire-carried cause — producer plan OQ 5) and the ISSUE-0130 catch-up
    boundary (a LIVE turn landing on a replay-opened record splits there,
    or the live conversation inherits the replayed span's no-derivation
    flag).  ISSUE-0123 part 3: both are ROOM events and a re-keyed room
    holds N records, so this FANS — per record, because every input is per
    record (wire id, predecessor, replay flag), closing every stale record
    rather than only the current sender's.  Persists behind
    :func:`persist_fanned_closes`, so one failure neither aborts the
    remaining splits nor the caller's own ingest.
    """
    stale_splits: list[Interaction] = []
    for record in tracker.records_for_scope(scope):
        stale_reason = stale_close_reason(record, event, wire_id=wire_id)
        if stale_reason is None:
            continue
        split = tracker.close_record(record, reason=stale_reason)
        if split is not None:
            stale_splits.append(split)
    await persist_fanned_closes(stale_splits, persist)


async def persist_closed_interaction(
    *,
    episodic: EpisodicMemory,
    llm_client: LLMClient,
    memory_ns: MemoryNamespace,
    agent_id: str,
    interaction: Interaction,
    pending_tasks: set[asyncio.Task[None]],
    on_finalized: Callable[[], Awaitable[None]],
) -> None:
    """RFC 0020 PR 4 close-path orchestrator (two-phase write).

    Phase 1 (sync, the caller holds ``_lock``): INSERT a ``closing`` row with
    :data:`SUMMARY_PENDING_TEXT` so the row exists before any LLM call and the
    janitor can sweep it on crash recovery.  Phase 2 (background):
    :func:`finalize_closed_interaction` summarises and ``UPDATE``s outside the
    lock.  See PR #229 deep-review Must-Fix #1 + Should-Fix #1.
    """
    if interaction.turn_count == 0:
        return  # idle no-turn scope — nothing to persist.
    if interaction.replayed:
        # ISSUE-0130 — the leak-stopper.  This interaction was OPENED by an
        # on-startup catch-up replay, whose turns carry no principal: the
        # orchestrator's ``messages`` table has no principal column, so
        # ``_build_replay_event`` has nothing to seed and the persona binds
        # its default (``local``).  Summarising and extracting facts from
        # such a span writes one authenticated person's content into the
        # shared tenant, where every unauthenticated caller resolves —
        # unbounded, because catch-up has no watermark and re-ingests the
        # window on every boot (RFC 0011 OQ #8).
        #
        # Skipping is bounded to spans that are ENTIRELY replay: the
        # catch-up boundary (:func:`close_replayed_scopes` at pass end, and
        # the replay→live split in ``_handle_multi_turn_event``) closes a
        # replay-opened scope before any live turn can join it, so a live
        # conversation resumed after a restart opens its own unflagged
        # interaction and derives normally under its own principal.  Without
        # that boundary this flag would eat the first post-restart
        # conversation in every replayed scope — catch-up opens scopes and
        # never closes them on its own (RFC 0011 OQ #8).
        #
        # This is the v0.3.14 leak-stopper, not the whole fix: it cannot
        # tell "no principal because the deployment is single-tenant"
        # (where ``local`` is CORRECT) from "no principal because replay
        # lost it".  v0.3.15 persists the principal on the message row and
        # seeds it here, at which point this skip narrows to genuinely
        # unattributable spans.
        logger.debug(
            "ISSUE-0130: skipping close-path derivation for replayed "
            "interaction (agent=%s scope=%s interaction_id=%s turns=%d) — "
            "a replayed span has no principal to attribute memory to",
            agent_id, interaction.scope, interaction.interaction_id,
            interaction.turn_count,
        )
        return
    # PR-4 review #25 (slice 7): dead ``or llm_client is None`` clause removed;
    # the mixin annotation is now ``LLMClient`` (non-optional).
    if interaction.interaction_id is None:
        logger.warning(
            "Closed interaction for agent %s has no interaction_id "
            "(scope=%s); skipping persistence",
            agent_id, interaction.scope,
        )
        return
    # ISSUE-0131 (PR #849 review round 3): every persisted turn field is
    # derived from the record's OWN turns — ``own_turn_items``, the §G
    # chokepoint in ``close_entries``.  A FOREIGN room-close turn is
    # dropped from persistence, not just from the derivation input: its
    # sender and envelope would otherwise ride ``context_json``, an
    # FTS-indexed column recall searches, on a row stamped with another
    # speaker's ``speaker_id``.  The turn survives only on the closer's
    # own record, where it is native.  ``turn_count`` (context AND the
    # queryable column below) counts the same post-exclusion turns the
    # row actually holds — the record's raw count would over-report on a
    # fanned close, admitting a single-native-turn sibling past
    # ``min_turns`` filters and multi-turn gates, the same over-report
    # the prompt header's ``shown_turns`` fix corrects in
    # ``summarize_close`` (identical whenever nothing was excluded).
    own_turns = own_turn_items(interaction)
    ctx: dict[str, Any] = {
        "scope": interaction.scope,
        "close_reason": interaction.close_reason,
        "turn_count": len(own_turns),
        # ISSUE-0102: persist the RFC 0030 governance interaction id this
        # episode was opened under (``wire_interaction_id``, otherwise
        # in-memory-only) so the read surface can expose it alongside the
        # agent-side ``interaction_id``. The two segment on independent clocks,
        # so a single governance interaction can map to several episode ids;
        # carrying it here makes the channel-side id — the one the end-vote
        # close logs carry — cross-referenceable. Empty for a DM / thread /
        # non-channel interaction that never carried a governance id; omitted
        # from the surface in that case. PR 2 also writes it to the queryable
        # ``governance_interaction_id`` column below (the read filter matches
        # that); this context copy stays as the read-side fallback that covers
        # a column-NULL row written by an older agent process.
        "governance_interaction_id": interaction.wire_interaction_id,
        # ISSUE-0054 / RFC 0020 §D — strip the inbound message ``text`` the
        # multi-turn path stashes for the RFC 0026 extractor: Phase 2 reads it
        # off the in-memory interaction, so the persisted ``context_json``
        # stays body-free.
        "turns": [
            {"at": t.at, "payload": {
                k: v for k, v in t.payload.items() if k != "text"}}
            for _, t in own_turns
        ],
    }
    # ISSUE-0123 R-1 (PR #846 review): bind the record's OWN frozen
    # principal for the whole derivation, the tenant twin of the
    # ``session_id`` guard below.
    #
    # Every storage tier resolves its tenant from the AMBIENT
    # ``principal_scope`` (``resolve_active_principal`` in ``episodic`` /
    # ``facts`` / ``relationship``), which the persona binds per EVENT.
    # That was right while a scope held one record, because the record
    # and the event were the same tenant's.  Since the re-key a room
    # holds one record per ``(principal, speaker)``, and both the
    # room-wide fans and ``idle_check`` close OTHER tenants' records
    # inside whichever tenant's request scope happened to trigger them —
    # so without this the closer's principal was stamped on every row the
    # close derived.  With strict-equality recall and no carve-out
    # (``_principal_filter``) that inverts the boundary the re-key exists
    # to draw: the speaker's own conversation becomes invisible to the
    # speaker and readable by whoever closed the room.  Idle is the worse
    # half — it fires on each record's own timer from the per-event hot
    # path, so a foreign-tenant write was ordinary room behaviour, not
    # just a close-time event.
    #
    # Spans BOTH phases deliberately.  ``asyncio.create_task`` snapshots
    # the context at creation, so opening the block around the task
    # construction puts Phase 2's facts and relationship writes under the
    # same tenant as the Phase-1 episode; binding Phase 1 alone would
    # leave a row whose derived facts live in a different tenant — worse
    # than the bug, since RFC 0049 Phase 1 facts are cross-room.
    #
    # Single-tenant deployments are unaffected: the frozen value was
    # resolved through the same precedence the tiers use (task-local
    # scope → ``PERSATRIX_PRINCIPAL_ID`` → ``local``), so it equals what
    # the ambient read would have produced.  A replayed span never
    # reaches here — the ISSUE-0130 skip returns above, so an
    # unattributable record cannot bind a tenant it does not have.
    #
    # This closes the principal half of the ISSUE-0123 boundary.  The
    # SPEAKER half is projected below (``speaker_id=``): migration 18's
    # column is written from the record key, not judged per turn — sound
    # because the one RFC 0020 §G room-close turn a record can hold
    # (``ROOM_CLOSE_TURN_KEY``) never leaves ``close_entries.own_turn_items``
    # (the §G chokepoint; that module states the single-speaker
    # argument): not into the derivation input, not into the persisted
    # turn context above.
    with principal_scope(interaction.principal_id):
        try:
            await episodic.store_episode(
                summary=SUMMARY_PENDING_TEXT, context=ctx,
                interaction_id=interaction.interaction_id,
                # ISSUE-0102 PR 2: the queryable governance-id column (v15).
                # Empty ``wire_interaction_id`` (DM / thread) → NULL.
                governance_interaction_id=interaction.wire_interaction_id or None,
                started_at=interaction.started_at,
                closed_at=interaction.closed_at,
                # Post-§G-exclusion count — the turns the row holds (the
                # rationale is on ``own_turns`` above).
                turn_count=len(own_turns), scope=interaction.scope,
                # ISSUE-0081 PR 2 sibling-mislabel guard: tag with the session
                # the interaction was *born* under, not the scope bound now —
                # ``idle_check`` may be flushing a sibling conversation's stale
                # interaction while another conversation's event holds the scope.
                session_id=interaction.session_id,
                # RFC 0037 §C (PR 3): the episode inherits the interaction's
                # frozen-at-open capture — ``normalize_for_stamp`` is the §A
                # rule-(a) owner (absent/unknown → ``internal``, never
                # ``public``).  Dark until the PR 4 §D gate reads it.
                protection_level=normalize_for_stamp(interaction.classification),
                source_channel_id=interaction.source_channel_id,
                # ISSUE-0131 (migration 18): the record key's speaker
                # half — the §G soundness argument is above.  ``""``
                # (tick / single-turn scope) → NULL, the honest
                # "no speaker" rather than an empty attribution.
                speaker_id=interaction.speaker_id or None)
        except Exception:
            logger.warning(
                "Failed to persist closed interaction for agent %s "
                "(scope=%s, principal=%s)",
                agent_id, interaction.scope, interaction.principal_id,
                exc_info=True,
            )
            return
        # Phase 2: background summarise + finalise.  add_done_callback
        # auto-cleans the tracking set so references don't accumulate.
        task: asyncio.Task[None] = asyncio.create_task(
            finalize_closed_interaction(
                llm_client=llm_client, memory_ns=memory_ns,
                episodic=episodic, agent_id=agent_id,
                interaction=interaction,
                on_finalized=on_finalized,
                # Phase-2 facts + relationship writes must match the Phase-1
                # row's session (sibling-mislabel guard) — the interaction's
                # frozen session, not the bound scope.  Its principal twin
                # rides the context this block binds.
                session_id=interaction.session_id,
            ),
        )
    pending_tasks.add(task)
    task.add_done_callback(pending_tasks.discard)


async def close_replayed_scopes(
    tracker: InteractionTracker,
    persist: Callable[[Interaction], Awaitable[None]],
) -> int:
    """Close every scope the on-startup catch-up replay opened (ISSUE-0130).

    Replay events open :class:`InteractionTracker` scopes but carry no
    session-end signal, and catch-up has no synthetic completion event of
    its own (RFC 0011 OQ #8 "lifecycle bleed"), so the scope stayed open
    until the idle timer and the next live turn appended to it.  That was
    harmless while the merged span still derived; it is not harmless now
    that :func:`persist_closed_interaction` skips derivation for a
    replay-opened span, because the live half of the merge would be
    dropped with it.

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
            popped = tracker.close_record(
                interaction, reason=REASON_CATCHUP_COMPLETE,
            )
            if popped is None:
                continue
            closed += 1
            # ``persist`` is a no-op for these spans today (it skips on the
            # same flag); calling it keeps ONE close→persist contract, so a
            # revision that makes replayed spans attributable needs no new
            # wiring here.
            await persist(popped)
        except Exception:
            logger.warning(
                "Catch-up close failed for replayed scope %s",
                interaction.scope, exc_info=True,
            )
    return closed
