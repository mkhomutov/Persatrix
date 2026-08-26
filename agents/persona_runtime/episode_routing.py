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

from ..channel_event_classification import wire_channel_classification
from ..channel_wire_metadata import wire_interaction_id
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
    is_thread_scope,
    scope_for_channel_event,
)
from ..persona_types import EventType
from ..session_id import current_session_id
from .close_path import close_replayed_scopes, persist_closed_interaction
from .finalize_close import drain_pending_summary_tasks
from .interaction_boundary import is_session_end_event, stale_close_reason
from .turn_payload import build_turn_payload
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
        # PR-3 review #13: the flush loop sits OUTSIDE the outer try/except
        # below.  Each ``_persist_closed_interaction`` call carries its own
        # inner ``try`` around ``store_episode``, but a rare programming error
        # in ctx-construction (or an ``asyncio.CancelledError``, which
        # ``Exception`` does not catch) could escape it.  The earlier nesting
        # let that escape propagate into the outer handler, which logged
        # ``event_type=<current event>`` — misattributing the failure to the
        # in-flight event rather than the stale scope that actually owned it.
        # Pulling the loop out and wrapping each iteration in its own
        # scope-aware ``try/except`` fixes the attribution and prevents a
        # flush failure from swallowing the current event's processing.
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
                    session_id=self._active_write_session_id)
                return
            scope = (
                SCOPE_TICK
                if event.event_type is EventType.TICK
                else event.event_type.value
            )
            # ``payload=None`` per PR-215 review (Should-Fix #4): the
            # open/close pair runs in one call, so the summariser never
            # reads a single-turn payload — the dict was dead bytes.
            # ISSUE-0131: the sender is the speaker half of the tracker
            # key (``None`` → the no-speaker key for tick / senderless
            # events); the principal half resolves ambient in the tracker.
            single_turn = self._interaction_tracker.add_turn(
                scope, payload=None,
                session_id=self._active_write_session_id,
                speaker_id=event.sender_id,
            )
            # Distinct local name from the ``for closed in idle_check()``
            # loop above so mypy sees a single binding type for each name.
            # ``close_record`` (not the keyed ``close``) so the open and
            # close halves address ONE object — re-deriving the key here
            # could diverge from the one ``add_turn`` resolved.
            structural_close = self._interaction_tracker.close_record(
                single_turn, reason=REASON_STRUCTURAL,
            )
            if structural_close is None:
                # PR 6 review #7: replace the prior ``... or interaction``
                # fallback that silently masked an invariant violation.
                # ``add_turn`` just opened this record under the agent's
                # ``asyncio.Lock``, so ``close_record`` MUST return it.
                # A future contract change would have produced NULL
                # interaction columns under the old fallback; raise instead so
                # the regression surfaces.  Explicit guard, not ``assert``
                # (stripped under ``python -O``, PR 6 review #21 precedent).
                raise RuntimeError(
                    f"InteractionTracker.close_record({scope!r}) returned "
                    "None for an interaction that was just opened in the "
                    "same call — tracker invariant violated.",
                )
            await self._episodic_memory.store_episode(
                # Carry the close trigger in the context blob so the read
                # surface (``GetClosedInteractions``) can report it.  The
                # two-phase multi-turn close path
                # (:func:`close_path.persist_closed_interaction`) has always
                # persisted ``close_reason``; the single-turn path used to drop
                # it, so every tick/task/approval row surfaced an empty trigger
                # (PR-583 review).  Mirror the multi-turn shape here.
                summary=summary,
                context={**ctx, "close_reason": structural_close.close_reason},
                interaction_id=structural_close.interaction_id,
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
            # event's processing — multi-turn handling, single-turn
            # ``add_turn``/``close``, and the legacy fallback's
            # ``store_episode``.  The cross-scope idle flush above is
            # intentionally outside it so a stale-scope failure logs the
            # failed scope's identity rather than the in-flight
            # ``event_type`` (slice 5 #13).  Two operator-visible failure
            # modes remain in scope:
            #
            # 1. The single-turn path's :meth:`store_episode` raising
            #    *after* ``close`` popped the scope and fired the
            #    ``interactions.closed.by_structural`` counter — the common
            #    case.  ``event_type`` in the warning lets operators
            #    correlate the metric increment to the missing row.
            # 2. Tracker programming errors from ``add_turn`` / ``close``
            #    or :meth:`_handle_multi_turn_event` raising past its own
            #    inner try (rare; pinned by the explicit guard further down
            #    and PR 6 review #21 precedent).
            #
            # Log-and-continue per the pre-RFC contract: a single failed
            # episode must not crash the event loop.
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
            # PR-216 review (Low / Should-Fix #4): an under-populated
            # multi-turn event (no ``channel_id`` / ``sender_id``) falls
            # back to the legacy NULL-interaction shape — warn so
            # malformed ingress is visible; a dedicated counter stays
            # deferred until production data warrants one.
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
        # RFC 0030 interaction-id producer: the channel conversation's
        # wire id rotating means the previous conversation ended (vote
        # quorum or idle) — close the stale local interaction so the new
        # turn opens a fresh one, labelled by the wire-carried close cause
        # (producer plan OQ 5).  Full rationale on the two predicates.
        #
        # Thread scopes are wire-UNTRACKED (PR 607 review finding 1): a
        # threaded reply carries the parent FLOOR's id (the resolver keys on
        # ``msg.ChannelID``), so that id rotating says nothing about the
        # thread — the resolver's IP3 rule ("the thread IS the interaction");
        # thread-scoped locals keep idle / session-end closes only.
        # PR #716 review: the shared drift-pinned reader, not an inline copy —
        # this read and the wallet-lease read must resolve the SAME id or
        # spend bills under an id the rotation boundary never keys.
        wire_id = "" if is_thread_scope(scope) else wire_interaction_id(event)
        # ISSUE-0130: the predicate also owns the catch-up boundary — a LIVE
        # turn landing on a replay-opened scope splits there, or the live
        # conversation would inherit the replayed span's no-derivation flag.
        # ISSUE-0123 part 3: both boundaries are ROOM events and a re-keyed
        # room holds N records, so the check FANS — evaluated per record
        # because the inputs are per record (wire id, predecessor, replay
        # flag), closing every stale record, not just the current sender's.
        for record in self._interaction_tracker.records_for_scope(scope):
            stale_reason = stale_close_reason(record, event, wire_id=wire_id)
            if stale_reason is None:
                continue
            split = self._interaction_tracker.close_record(
                record, reason=stale_reason,
            )
            if split is not None:
                await self._persist_closed_interaction(split)
        interaction = self._interaction_tracker.add_turn(
            scope, payload=payload,
            session_id=self._active_write_session_id,
            # RFC 0037 §C (PR 3): the interaction-open capture pair —
            # verbatim wire classification (the shared drift-pinned reader)
            # + acting channel id; honoured only when this turn OPENS the
            # interaction (frozen-at-open, the ``session_id`` rule).
            classification=wire_channel_classification(event),
            source_channel_id=event.channel_id or None,
            # ISSUE-0130: frozen-at-open like the pair above; the close path
            # skips derivation for a replayed span (no principal to attribute).
            replayed=event.metadata.get("replay_mode") is True,
            # ISSUE-0131: the speaker half of the key — the turn lands in
            # ITS sender's record; the principal half resolves ambient
            # (the ``on_event`` request scope) inside the tracker.
            speaker_id=event.sender_id,
        )
        # Stamp the wire id the interaction was opened under (first turn
        # that carries one wins) plus its known predecessor — the
        # late-delivery defence ``wire_rotation_closes`` compares against.
        if wire_id and interaction.is_open and not interaction.wire_interaction_id:
            interaction.wire_interaction_id = wire_id
            interaction.predecessor_wire_id = str(
                event.metadata.get("previous_interaction_id", "") or "",
            )
        # PR-3 review #12: ``add_turn`` now closes inline when the MaxTurns
        # cap fires.  The returned interaction is the cap-closed one (with
        # ``close_reason == REASON_MAX_TURNS``); persist it immediately so the
        # closed-interaction episode row exists before the next event arrives.
        # A subsequent session-end on the same scope would no-op (scope
        # already popped), which is the correct contract — the cap closure
        # takes precedence over a same-event structural close.
        if not interaction.is_open:
            await self._persist_closed_interaction(interaction)
            return
        if is_session_end_event(event):
            # A session end is a ROOM event (ISSUE-0123 part 3): fan the
            # structural close over every ``(principal, speaker)`` record
            # open in the scope, not just this sender's, or the siblings
            # leak open until idle relabels the ended conversation.
            for closed in self._interaction_tracker.close_scope(
                scope, reason=REASON_STRUCTURAL,
            ):
                await self._persist_closed_interaction(closed)
        else:
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

    async def close_replayed_interactions(self) -> int:
        """The catch-up caller's ISSUE-0130 hook — see
        :func:`agents.persona_runtime.close_path.close_replayed_scopes`."""
        return await close_replayed_scopes(
            self._interaction_tracker, self._persist_closed_interaction,
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
