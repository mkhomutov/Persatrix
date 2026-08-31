"""RFC 0020 episode routing + close-path orchestration.

Extracted from :mod:`agents.persona_runtime.state_persistence` so that
module stays under the 500-line file-size cap enforced by
``scripts/checks/file_size.py --strict``.  Houses the per-event routing
path (single-turn vs. multi-turn vs. unknown), the close-path two-phase
write, the background-summary drain, and the ``closing``-row janitor
entry point.

This mixin is composed onto ``_LLMPersonaAgent`` alongside
:class:`~agents.persona_runtime.state_persistence._StatePersistenceMixin`
(state + memory-tier lifecycle).  Both mixins read the same agent
attributes (``_episodic_memory``, ``_interaction_tracker``,
``_pending_summarize_tasks``, ``config``, ``agent_id``); the split is
mechanical — the routing surface is large enough that combining the two
busts the file-size cap.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from ..persona_types import AgentAction, AgentEvent
    from . import MemoryNamespace

from ..memory.boundary_detectors import (
    DEFAULT_CLOSING_GRACE_SEC,
    REASON_STRUCTURAL,
)
from ..memory.episodic import EpisodicMemory
from ..memory.interactions import (
    SCOPE_TICK,
    Interaction,
    InteractionTracker,
    cleanup_closing_interactions,
    scope_for_channel_event,
)
from ..persona_types import EventType
from ..session_id import current_session_id
from .close_path import (
    close_stale_records,
    persist_closed_interaction,
    persist_fanned_closes,
)
from .finalize_close import drain_pending_summary_tasks
from .interaction_boundary import (
    is_session_end_event,
    scope_wire_anchor,
    wire_admits_record,
)
from .replay_sweep import close_replayed_scopes
from .turn_payload import build_turn_payload, frozen_open_capture
from .vote_close import PendingVoteClose, park_end_vote_close

logger = logging.getLogger(__name__)

__all__ = ["_EpisodeRoutingMixin"]


class _EpisodeRoutingMixin:
    """RFC 0020 routing + close-path orchestration for ``_LLMPersonaAgent``.

    Expects the following attributes on ``self`` (provided by the
    concrete ``_LLMPersonaAgent`` class and ``PersonaAgent`` base):

    - ``agent_id: str``
    - ``config: dict[str, Any]``
    - ``_episodic_memory: EpisodicMemory``
    - ``_lock: asyncio.Lock``
    - ``_interaction_tracker: InteractionTracker``
    - ``_llm_client: LLMClient``
    - ``_memory_ns: MemoryNamespace``
    - ``_pending_summarize_tasks: set[asyncio.Task[None]]``
    """

    # Attribute declarations for type checkers — set by __init__.
    agent_id: str
    config: dict[str, Any]
    _episodic_memory: EpisodicMemory
    _lock: asyncio.Lock
    _interaction_tracker: InteractionTracker
    # PR-4 review #25 (slice 7): tight ``LLMClient`` (no ``| None``)
    # keeps the dead silent-drop in ``_persist_closed_interaction``
    # gone.  MRO conflict silenced at :class:`_LLMPersonaAgent`'s
    # re-declaration; see :class:`_ActionLoopMixin` for the rationale.
    _llm_client: LLMClient
    _memory_ns: MemoryNamespace
    # RFC 0020 PR 4: in-flight background summary tasks (PR #229 Must-Fix #1).
    _pending_summarize_tasks: set[asyncio.Task[None]]
    # PR 607 finding 5: parked vote closes by channel id (:mod:`.vote_close`).
    _pending_vote_closes: dict[str, PendingVoteClose]
    # PR 607 second pass: the wire id each scope last vote-closed — the
    # local mirror of Go's vote dedup (:mod:`.vote_close`, re-vote guard).
    _vote_closed_wire_ids: dict[str, str]
    # RFC 0031 Phase 1: per-process operator-namespace tag stamped on
    # every ``store_episode`` / ``record_interaction`` call.  Set in
    # ``PersonaAgent.__init__`` (``agents/persona.py``) from
    # ``PERSATRIX_SESSION_ID``; defaults to ``"legacy"`` when unset.
    _session_id: str

    # RFC 0020 §G event-type routing.  Both sets are *positive lists* —
    # PR-215 review (Should-Fix #2): unknown event types hit the legacy
    # path with a warning so a new ``EventType`` requires a conscious
    # choice about which set it belongs to (rather than silently
    # routing through the tracker as a ``tick``-scoped row).
    _MULTI_TURN_EVENT_TYPES: frozenset[EventType] = frozenset({
        EventType.CHANNEL_MESSAGE,
        EventType.MENTION,
    })
    _SINGLE_TURN_EVENT_TYPES: frozenset[EventType] = frozenset({
        EventType.TICK,
        EventType.TASK_ASSIGNED,
        EventType.SUB_AGENT_COMPLETED,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESPONSE,
        EventType.AGENT_JOINED,
        EventType.AGENT_LEFT,
    })

    @property
    def _active_write_session_id(self) -> str:
        """Session to tag *fresh* writes with (ISSUE-0081 PR 2).

        Call-time precedence: a per-request ``session_scope`` (entered by
        :meth:`_LLMPersonaAgent.on_event`) wins over the construction-time
        ``_session_id`` snapshot, so a row written while handling
        conversation A's event lands under A even when a sibling
        conversation shares this process.  Falls back to ``_session_id``
        when no scope is active (tick / boot / single-session paths), which
        is exactly the pre-PR-2 behaviour.

        This is the *open*-time session.  The close path
        (:meth:`_persist_closed_interaction`) deliberately uses the
        interaction's frozen ``session_id`` instead — see that method and
        :class:`agents.memory.interactions.Interaction` for the
        sibling-mislabel guard.
        """
        return current_session_id() or self._session_id

    async def _store_event_episode(
        self, event: AgentEvent, actions: list[AgentAction],
    ) -> None:
        """Persist the episode for a completed event (RFC 0020 PR 2/PR 3).

        Routes single-turn paths through :class:`InteractionTracker`
        and accumulates multi-turn paths into the open interaction;
        closure is driven by the :mod:`.interaction_boundary` triggers
        (session-end metadata, the RFC 0030 end-of-interaction vote,
        the wire interaction-id rotation), the idle gap, or the PR-4
        janitor.  See PR-215 review for scope-labelling rationale
        (single-turn rows carry the event-type value, not
        :data:`SCOPE_TICK`).
        """
        summary = (
            f"Event: {event.event_type.value} → "
            f"Actions: {[a.action_type.value for a in actions]}"
        )
        ctx = {"event": event.payload, "sender": event.sender_id}
        # Step 1: flush any interaction whose idle window expired since the
        # last event.  Runs unconditionally so single-turn paths also drive
        # the cross-scope janitor without waiting for PR 4.
        #
        # PR-3 review #13: the loop sits OUTSIDE the outer try below, with a
        # per-iteration guard, so a flush failure logs the stale scope's own
        # identity and never swallows the current event's processing.
        for closed in self._interaction_tracker.idle_check():
            try:
                await self._persist_closed_interaction(closed)
            except Exception:
                logger.warning(
                    "Failed to flush idle interaction for agent %s "
                    "(scope=%s, interaction_id=%s)",
                    self.agent_id, closed.scope, closed.interaction_id,
                    exc_info=True,
                )
        try:
            if event.event_type in self._MULTI_TURN_EVENT_TYPES:
                await self._handle_multi_turn_event(event, summary, ctx, actions)
                return
            if event.event_type not in self._SINGLE_TURN_EVENT_TYPES:
                # Defensive fallback: a new EventType was introduced
                # without updating the routing table.  Land the row in
                # the legacy shape (no interaction columns) rather than
                # silently mislabelling it under SCOPE_TICK, and warn
                # so the gap is noticed in tests / logs.
                logger.warning(
                    "Event type %s is not classified for RFC 0020 routing; "
                    "falling back to legacy episode shape. Update "
                    "_EpisodeRoutingMixin._{MULTI,SINGLE}_TURN_EVENT_TYPES.",
                    event.event_type.value,
                )
                await self._episodic_memory.store_episode(
                    summary=summary, context=ctx,
                    session_id=self._active_write_session_id,
                    # ISSUE-0131: a lone event's sender IS its speaker;
                    # senderless stays NULL (the boundary normalizes "").
                    speaker_id=event.sender_id)
                return
            scope = (
                SCOPE_TICK
                if event.event_type is EventType.TICK
                else event.event_type.value
            )
            # ``payload=None`` per PR-215 review (Should-Fix #4): the
            # open/close pair runs in one call — a single-turn payload is
            # dead bytes.  ISSUE-0131: the sender is the speaker key half.
            single_turn = self._interaction_tracker.add_turn(
                scope, payload=None,
                session_id=self._active_write_session_id,
                speaker_id=event.sender_id,
            )
            # Distinct name from the idle-flush loop's binding (mypy).
            # ``close_record`` so the open and close halves address ONE
            # object — re-deriving the key could diverge from ``add_turn``'s.
            structural_close = self._interaction_tracker.close_record(
                single_turn, reason=REASON_STRUCTURAL,
            )
            if structural_close is None:
                # PR 6 review #7: no silent fallback — ``add_turn`` just
                # opened this record under the agent's lock, so
                # ``close_record`` MUST return it; raise (not ``assert``,
                # stripped under ``-O``) so a contract change surfaces.
                raise RuntimeError(
                    f"InteractionTracker.close_record({scope!r}) returned "
                    "None for an interaction that was just opened in the "
                    "same call — tracker invariant violated.",
                )
            await self._episodic_memory.store_episode(
                # Carry the close trigger so ``GetClosedInteractions`` can
                # report it — mirror of the multi-turn shape (PR-583 review:
                # the single-turn path used to drop it).
                summary=summary,
                context={**ctx, "close_reason": structural_close.close_reason},
                interaction_id=structural_close.interaction_id,
                # ISSUE-0131: the speaker half of the key this record was
                # opened under — ``""`` (a tick) → NULL.
                speaker_id=structural_close.speaker_id or None,
                started_at=structural_close.started_at,
                closed_at=structural_close.closed_at,
                turn_count=structural_close.turn_count,
                scope=structural_close.scope,
                # Single-turn open+close in one call: the interaction's
                # frozen session equals ``_active_write_session_id`` here,
                # but use the interaction's value so the open and close
                # writes share one source of truth (sibling-mislabel guard).
                session_id=structural_close.session_id,
            )
        except Exception:
            # PR 6 slice 4 #6 + slice 5 #13: this try guards the CURRENT
            # event's processing (the idle flush above sits outside it so a
            # stale-scope failure logs its own identity).  In scope: the
            # single-turn ``store_episode`` raising after ``close`` popped
            # the scope and fired the counter (``event_type`` lets operators
            # correlate the increment to the missing row), and tracker
            # programming errors.  Log-and-continue per the pre-RFC
            # contract: a single failed episode must not crash the loop.
            logger.warning(
                "Failed to store episode for agent %s (event_type=%s)",
                self.agent_id,
                event.event_type.value,
                exc_info=True,
            )

    # ─── Multi-turn aggregation (RFC 0020 PR 3) ───────────────

    # The close-trigger predicates (session-end metadata, the RFC 0030
    # end-of-interaction vote, the wire interaction-id rotation) live in
    # :mod:`agents.persona_runtime.interaction_boundary` — extracted to
    # keep this module under the 500-line cap.

    def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None:
        """RFC 0020 §G scope routing — see :func:`scope_for_channel_event`."""
        payload = event.payload or {}
        return scope_for_channel_event(
            self.agent_id,
            channel_id=event.channel_id,
            sender_id=event.sender_id,
            thread_id=event.thread_id,
            channel_type=payload.get("channel_type"),
            on_unknown=lambda raw, cid: logger.warning(
                "Agent %s: CHANNEL_MESSAGE has unrecognised channel_type=%r "
                "and unknown channel_id prefix %r; falling back to thread scope",
                self.agent_id, raw, cid,
            ),
        )

    async def _handle_multi_turn_event(
        self,
        event: AgentEvent,
        summary: str,
        ctx: dict[str, Any],
        actions: list[AgentAction],
    ) -> None:
        """Append a turn to the open interaction; close on session end or
        wire interaction-id rotation, and PARK the close for an
        end-of-interaction vote (publish-confirmed — :mod:`.vote_close`).

        The per-turn ``summary`` / ``ctx`` ride the turn payload so the
        PR 4 summariser sees the legacy single-row episode fields.
        ``actions`` are the turn's decided actions, consulted for the
        Layer 4 vote trigger; empty (salience-gate silent paths) means
        no vote this turn.
        """
        scope = self._scope_for_multi_turn_event(event)
        if scope is None:
            # PR-216 review (Should-Fix #4): an under-populated multi-turn
            # event falls back to the legacy NULL-interaction shape — warn
            # so malformed ingress is visible.
            logger.warning(
                "Agent %s: multi-turn event %s has neither channel_id nor "
                "sender_id; storing as legacy NULL-interaction episode",
                self.agent_id, event.event_type.value,
            )
            await self._episodic_memory.store_episode(
                summary=summary, context=ctx,
                session_id=self._active_write_session_id)
            return
        # The turn payload construction lives in :mod:`.turn_payload`
        # (v0.3.15 residuals PR 3) — shared with the close-notification
        # room fan, which must land the closing message on every open
        # record, not just the sender's.
        payload = build_turn_payload(event, summary)
        # The wire anchor this scope answers to: the rotation boundary
        # below, the admission conjunct on the session-end fan, and the
        # wallet-lease read must all resolve the SAME id (PR #716 review),
        # and thread scopes must resolve blank (PR 607 finding 1) — both
        # rules live in ``scope_wire_anchor`` since PR #846 review, which
        # found the carve-out inline at three sites and dropped at one.
        wire_id = scope_wire_anchor(scope, event)
        # The ingest-time boundary fan (wire rotation + the ISSUE-0130
        # catch-up split) lives in :func:`close_path.close_stale_records`
        # — a close+guarded-persist fan, per record since the re-key.
        await close_stale_records(
            self._interaction_tracker, scope, event, wire_id=wire_id,
            persist=self._persist_closed_interaction,
        )
        # PR #846 review REMOVED a recordless-session-end short circuit
        # here.  It skipped this ingest when the terminator held no record
        # of its own, to avoid "fabricating" a 1-turn ended record — but a
        # session end is a channel MESSAGE with a body, not the
        # contentless control event the close-notification no-open branch
        # declines to mirror, and the in-memory turn is the only place
        # that body lives (persistence strips ``text``).  So it dropped a
        # real message from memory entirely, and inconsistently: a
        # terminator WITH a record kept its turn, an EMPTY scope minted
        # the very record it called fabricated, and its ambient-principal
        # ``get`` beside a principal-blind sibling check lost a speaker's
        # own closing message on a tenant change.  The terminator's turn
        # lands in the terminator's record — minted if absent, like every
        # other multi-turn event — and the fan below closes it too.
        interaction = self._interaction_tracker.add_turn(
            scope, payload=payload,
            session_id=self._active_write_session_id,
            # The frozen-at-open capture set — RFC 0037 §C classification
            # + channel, the ISSUE-0130 replay pair, and the ISSUE-0131
            # speaker key half.  One named set in ``turn_payload`` since
            # v0.3.15 PR B2, where the rule they share is stated.
            **frozen_open_capture(event),
        )
        # Stamp the wire id the interaction was opened under (first turn
        # that carries one wins) plus its known predecessor — the
        # late-delivery defence ``wire_rotation_closes`` compares against.
        if wire_id and interaction.is_open and not interaction.wire_interaction_id:
            # Stamp the record with the id its opening turn actually
            # carried, even when a sibling has already rotated past it.
            # PR #846 review reversed the earlier suppression here: a
            # straggler record left BLANK is not neutral, because blank
            # is the universally-admitted state in all three close fans
            # and in the close-notification wire-id backfill — so the
            # retired conversation's turn was closed as, cross-referenced
            # to, and (on a bounded close) billed to the SUCCESSOR.  An
            # honest retired stamp makes every one of those conjuncts
            # skip it correctly.  The cost is the straggler fragmentation
            # the PR already accepts and documents: the record closes as
            # its own 1-turn fragment on the next rotation.
            interaction.wire_interaction_id = wire_id
            interaction.predecessor_wire_id = str(
                event.metadata.get("previous_interaction_id", "") or "",
            )
        # PR-3 review #12: ``add_turn`` closes inline at the MaxTurns cap;
        # persist immediately.  The cap outranks a same-event structural
        # close for THIS record only (``close_scope`` no-ops on it): a
        # session end riding the cap-th turn still fans the siblings
        # (v0.3.15 PR 3 review fix).  A cap-closed record parks no vote.
        if not interaction.is_open:
            # Guarded (PR #846 review): a raise here would skip the
            # session-end fan below and leak siblings to an idle relabel.
            await persist_fanned_closes(
                (interaction,), self._persist_closed_interaction,
            )
        if is_session_end_event(event):
            # A session end is a ROOM event (ISSUE-0123 part 3): fan the
            # structural close over every ``(principal, speaker)`` record
            # open in the scope, or the siblings leak open until idle
            # relabels the ended conversation.  Guarded per record — the
            # fan pops ALL its records before the first persist runs.
            #
            # Per-record wire-id admission (PR #846 review) — the
            # conjunct the other three fans already applied.  This was the
            # un-anchored one, so a straggler ``chat_end`` of a RETIRED
            # conversation buried the live successor's records as "ended";
            # the stale loop above cannot pre-clear them, since
            # ``wire_rotation_closes`` spares a successor precisely when
            # the straggler's id is its ``predecessor_wire_id``.  A blank
            # anchor keeps the tolerant scope-keyed behaviour.
            await persist_fanned_closes(
                self._interaction_tracker.close_scope(
                    scope, reason=REASON_STRUCTURAL,
                    admit=lambda record: wire_admits_record(record, wire_id),
                ),
                self._persist_closed_interaction,
            )
        elif interaction.is_open:
            # PR 607 finding 5: a vote close is PARKED for the executor's
            # publish-outcome callback (:mod:`.vote_close` owns the gates).
            park_end_vote_close(
                self, event, scope=scope, interaction=interaction,
                actions=actions,
            )

    async def _persist_closed_interaction(self, interaction: Interaction) -> None:
        """RFC 0020 PR 4 close-path orchestrator — see
        :func:`agents.persona_runtime.close_path.persist_closed_interaction`.

        Thin seam over the extracted two-phase write so this module stays
        under the file-size cap; tests patch / call this method directly.
        """
        await persist_closed_interaction(
            episodic=self._episodic_memory,
            llm_client=self._llm_client,
            memory_ns=self._memory_ns,
            agent_id=self.agent_id,
            interaction=interaction,
            pending_tasks=self._pending_summarize_tasks,
            on_finalized=self._tick_auto_reflect_counter,
        )

    async def close_replayed_interactions(
        self, *, derive_channels: frozenset[str] | None = None,
    ) -> int:
        """The catch-up caller's ISSUE-0130 hook — see
        :func:`agents.persona_runtime.close_path.close_replayed_scopes`,
        which states what ``derive_channels`` means (the channels whose
        replay actually finished; ``None`` = derive everything) and what
        the task set is for (bounding the boot burst).
        """
        return await close_replayed_scopes(
            self._interaction_tracker, self._persist_closed_interaction,
            derive_channels=derive_channels,
            pending_tasks=self._pending_summarize_tasks,
        )

    async def drain_pending_summaries(self) -> None:
        """Await in-flight background summary tasks (RFC 0020 PR 4).

        PR 6 review #23 — :func:`drain_pending_summary_tasks`
        snapshots the pending set with ``list(...)``.  :meth:`close_memory`
        runs this drain under ``self._lock`` so no new close path can
        race in and spawn an un-awaited task.  A refactor that moves
        the drain outside the lock MUST switch to a loop-until-empty
        drain or it will silently lose late-arriving tasks.
        """
        await drain_pending_summary_tasks(self._pending_summarize_tasks)

    async def _tick_auto_reflect_counter(self) -> None:
        """Increment the auto-reflect counter on close (RFC 0020 §H).

        Nudges now fire on N closed interactions, not N inbound events.
        Best-effort: counter-store hiccup must not break the close path.
        """
        memory_cfg = self.config.get("memory") or {}
        notes_cfg = memory_cfg.get("notes") or {}
        if int(notes_cfg.get("auto_reflect_after", 0)) <= 0:
            return
        try:
            await self._episodic_memory.increment_interaction_count()
        except Exception:
            logger.debug(
                "auto-reflect counter increment failed for agent %s",
                self.agent_id, exc_info=True,
            )

    async def cleanup_closing_interactions(
        self, *, grace_sec: float = DEFAULT_CLOSING_GRACE_SEC,
        now: float | None = None,
    ) -> int:
        """Public janitor entry point (RFC 0020 PR 4 §C).

        Wires the agent's own DB handle and id into
        :func:`agents.memory.interactions.cleanup_closing_interactions`.
        """
        db = self._episodic_memory._ensure_db()
        return await cleanup_closing_interactions(
            db, self.agent_id, grace_sec=grace_sec, now=now,
        )
