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
    SUMMARY_PENDING_TEXT,
    Interaction,
    InteractionTracker,
    cleanup_closing_interactions,
    scope_for_channel_event,
)
from ..persona_types import EventType
from .summarize_close import (
    drain_pending_summary_tasks,
    finalize_closed_interaction,
)

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

    async def _store_event_episode(
        self, event: AgentEvent, actions: list[AgentAction],
    ) -> None:
        """Persist the episode for a completed event (RFC 0020 PR 2/PR 3).

        Routes single-turn paths through :class:`InteractionTracker`
        and accumulates multi-turn paths into the open interaction;
        closure is driven by ``chat_end`` metadata, idle gap, or the
        PR-4 janitor.  See PR-215 review for scope-labelling
        rationale (single-turn rows carry the event-type value, not
        :data:`SCOPE_TICK`).
        """
        summary = (
            f"Event: {event.event_type.value} → "
            f"Actions: {[a.action_type.value for a in actions]}"
        )
        ctx = {"event": event.payload, "sender": event.sender_id}
        # Step 1: flush any interaction whose idle window expired since
        # the last event.  Runs unconditionally so single-turn paths
        # also drive the cross-scope janitor without waiting for PR 4.
        #
        # PR-3 review #13: the flush loop sits OUTSIDE the outer
        # try/except below.  Each ``_persist_closed_interaction`` call
        # carries its own inner ``try`` around ``store_episode``, but a
        # rare programming error in ctx-construction (or an
        # ``asyncio.CancelledError``, which ``Exception`` does not
        # catch) could escape it.  The earlier nesting let that escape
        # propagate into the outer handler, which logged
        # ``event_type=<current event>`` — misattributing the failure
        # to the in-flight event rather than the stale scope that
        # actually owned it.  Pulling the loop out and wrapping each
        # iteration in its own scope-aware ``try/except`` fixes the
        # attribution and prevents a flush failure from swallowing the
        # current event's processing entirely.
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
                await self._handle_multi_turn_event(event, summary, ctx)
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
                await self._episodic_memory.store_episode(summary=summary, context=ctx)
                return
            scope = (
                SCOPE_TICK
                if event.event_type is EventType.TICK
                else event.event_type.value
            )
            # ``payload=None`` per PR-215 review (Should-Fix #4): for
            # single-turn rows the open/close pair runs in one call so
            # the PR 4 summariser will never read this payload (it will
            # short-circuit on ``turn_count == 1``).  Passing the
            # duplicate dict was dead bytes on the hot path.
            self._interaction_tracker.add_turn(scope, payload=None)
            # Distinct local name from the ``for closed in idle_check()``
            # loop above so mypy sees a single binding type for each name.
            structural_close = self._interaction_tracker.close(
                scope, reason=REASON_STRUCTURAL,
            )
            if structural_close is None:
                # PR 6 review #7: replace the prior ``... or interaction``
                # fallback that silently masked an invariant violation.
                # ``add_turn`` just opened ``scope`` under the agent's
                # ``asyncio.Lock``, so ``close`` MUST return that
                # interaction.  A future contract change (e.g. ``close``
                # returning ``None`` for already-closed scopes) would
                # have produced NULL interaction columns under the old
                # fallback; raise instead so the regression surfaces.
                # Explicit guard, not ``assert`` (stripped under
                # ``python -O`` per PR 6 review #21 precedent).
                raise RuntimeError(
                    f"InteractionTracker.close({scope!r}) returned None "
                    "for an interaction that was just opened in the same "
                    "call — tracker invariant violated.",
                )
            await self._episodic_memory.store_episode(
                summary=summary,
                context=ctx,
                interaction_id=structural_close.interaction_id,
                started_at=structural_close.started_at,
                closed_at=structural_close.closed_at,
                turn_count=structural_close.turn_count,
                scope=structural_close.scope,
            )
        except Exception:
            # PR 6 slice 4 #6 + slice 5 #13: this try guards the
            # CURRENT event's processing — multi-turn handling,
            # single-turn ``add_turn``/``close``, and the legacy
            # fallback's ``store_episode``.  The cross-scope idle
            # flush above is intentionally outside this block so a
            # stale-scope failure logs the failed scope's identity
            # rather than the in-flight ``event_type`` (slice 5 #13).
            # Two operator-visible failure modes remain in scope:
            #
            # 1. The single-turn path's :meth:`store_episode` raising
            #    *after* ``close`` already popped the scope and fired
            #    the ``interactions.closed.by_structural`` counter —
            #    the common case.  ``event_type`` in the warning lets
            #    operators correlate the metric increment to the
            #    missing row.
            # 2. Tracker programming errors from ``add_turn`` / ``close``
            #    or :meth:`_handle_multi_turn_event` raising past its
            #    own inner try (rare; pinned by the explicit guard
            #    further down and PR 6 review #21 precedent).
            #
            # Log-and-continue per the pre-RFC contract: a single
            # failed episode must not crash the event loop.
            logger.warning(
                "Failed to store episode for agent %s (event_type=%s)",
                self.agent_id,
                event.event_type.value,
                exc_info=True,
            )

    # ─── Multi-turn aggregation (RFC 0020 PR 3) ───────────────

    # Metadata keys that signal an explicit session end on a multi-turn
    # event.  Either spelling is accepted so RFC 0016 ("chat_end") and
    # RFC 0011 ("session_end") emit a structural close without a
    # second adapter layer.
    _SESSION_END_METADATA_KEYS: frozenset[str] = frozenset({
        "chat_end",
        "session_end",
    })

    # Strings accepted as ``True`` for session-end metadata flags.
    # PR-216 review (High #3): a bare ``bool(meta.get(k))`` truthiness
    # check would close on any non-empty string, including ``"false"``
    # / ``"0"`` / ``"no"`` — a footgun for any channel adapter that
    # JSON-stringifies booleans (a common interop pattern).  Restrict
    # the accepted truthy strings to a small canonical allowlist so
    # ``metadata={"chat_end": "false"}`` no longer closes the
    # interaction.
    _SESSION_END_TRUTHY_STRINGS: frozenset[str] = frozenset({
        "true", "1", "yes", "y", "on",
    })

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

    def _is_session_end_event(self, event: AgentEvent) -> bool:
        """Strict-truthy check for session-end metadata flags.

        PR-216 review (High #3): ``bool("false")`` is ``True``, so the
        prior ``bool(meta.get(k))`` accepted any non-empty string — a
        channel adapter that stringifies booleans would have closed
        every multi-turn interaction unexpectedly.  Accept only:

        * the ``bool`` value ``True`` (and only ``True`` — not any
          truthy non-bool such as a list/object),
        * a non-zero numeric value,
        * a string whose lowercase form is in
          :attr:`_SESSION_END_TRUTHY_STRINGS`.

        Anything else (``False``, ``0``, ``None``, ``"false"``,
        ``"0"``, empty string, missing key) is treated as not-end.
        """
        meta = event.metadata or {}
        for key in self._SESSION_END_METADATA_KEYS:
            if key not in meta:
                continue
            val = meta[key]
            if val is True:
                return True
            if isinstance(val, str):
                if val.strip().lower() in self._SESSION_END_TRUTHY_STRINGS:
                    return True
                continue
            # ``bool`` is a subclass of ``int``; ``True`` is handled
            # above and ``False`` falls through to the int branch as
            # ``0`` (correctly evaluating not-end).
            if isinstance(val, (int, float)) and val != 0:
                return True
        return False

    async def _handle_multi_turn_event(
        self,
        event: AgentEvent,
        summary: str,
        ctx: dict[str, Any],
    ) -> None:
        """Append a turn to the open interaction; close on session end.

        The per-turn ``summary`` / ``ctx`` are stashed on the turn
        payload so the PR 4 summariser has the same fields it would
        have written to a legacy single-row episode — PR 4 swaps the
        placeholder summary for an LLM-generated one without changing
        this call site.
        """
        scope = self._scope_for_multi_turn_event(event)
        if scope is None:
            # PR-216 review (Low / Should-Fix #4): an under-populated
            # multi-turn event (no ``channel_id`` and no ``sender_id``)
            # silently falls back to the legacy NULL-interaction shape.
            # Surface it as a warning so operators can spot malformed
            # ingress before it manifests as a downstream episode-shape
            # regression.  Kept as a log line rather than a new
            # counter to avoid expanding the metrics surface mid-RFC;
            # PR 5 (channel-aware routing) is the right place for a
            # dedicated ``agent.interactions.scope_unresolved`` counter
            # if production data warrants one.
            logger.warning(
                "Agent %s: multi-turn event %s has neither channel_id nor "
                "sender_id; storing as legacy NULL-interaction episode",
                self.agent_id, event.event_type.value,
            )
            await self._episodic_memory.store_episode(summary=summary, context=ctx)
            return
        # PR-216 review (High #1): RFC 0020 §D pins
        # *"Per-turn message text is not stored in episodes"*, and PR 1's
        # ``Turn`` dataclass docstring repeats the constraint.  An
        # earlier draft stashed the full ``ctx`` (which carries
        # ``event.payload`` — i.e. the message body for
        # ``CHANNEL_MESSAGE`` / ``MENTION``) on the turn, so the
        # closed-interaction ``context_json`` ended up embedding every
        # message body for the lifetime of the row.  PR 4's LLM
        # summariser only needs the per-turn structural envelope plus
        # the deterministic ``summary`` text — it does not need the
        # raw body.  Keep only the structural fields here so the
        # spec-vs-code drift is closed; if a future PR genuinely needs
        # the body, RFC 0020 §D must be amended in the same change.
        # PR-3 review #18: ``ctx`` above is annotated ``dict[str, Any]``
        # (the same dict ends up in persisted JSON via
        # ``_persist_closed_interaction``).  Match the annotation here
        # so a future caller that stashes a non-``object`` value (e.g.
        # nested ``Any``-typed metadata read from a payload) doesn't
        # need a cast at this site.
        payload: dict[str, Any] = {
            "summary": summary,
            "event_type": event.event_type.value,
            "sender": event.sender_id,
            "channel_id": event.channel_id,
            "timestamp": event.timestamp,
            # RFC 0020 PR 4: stash sender's participant_type so the
            # close-path ``record_interaction`` can carry the correct
            # ``other_participant_type`` (defaults "agent" downstream).
            "participant_type": event.metadata.get(
                "sender_participant_type", "agent",
            ),
        }
        interaction = self._interaction_tracker.add_turn(scope, payload=payload)
        # PR-3 review #12: ``add_turn`` now closes inline when the
        # MaxTurns cap fires.  The returned interaction is the
        # cap-closed one (with ``close_reason == REASON_MAX_TURNS``);
        # persist it immediately so the closed-interaction episode
        # row exists before the next event arrives.  A subsequent
        # session-end on the same scope would no-op (scope already
        # popped), which is the correct contract — the cap closure
        # takes precedence over a same-event structural close.
        if not interaction.is_open:
            await self._persist_closed_interaction(interaction)
            return
        if self._is_session_end_event(event):
            closed = self._interaction_tracker.close(
                scope, reason=REASON_STRUCTURAL,
            )
            if closed is not None:
                await self._persist_closed_interaction(closed)

    async def _persist_closed_interaction(self, interaction: Interaction) -> None:
        """RFC 0020 PR 4 close-path orchestrator (two-phase write).

        Phase 1 (sync, under ``_lock``): INSERT a ``closing`` row with
        :data:`SUMMARY_PENDING_TEXT` so the row exists before any LLM
        call and the janitor can sweep it on crash recovery.  Phase 2
        (background): :func:`finalize_closed_interaction` summarises
        and ``UPDATE``s outside the lock.  See PR #229 deep-review
        Must-Fix #1 + Should-Fix #1.
        """
        if interaction.turn_count == 0:
            return  # idle no-turn scope — nothing to persist.
        # PR-4 review #25 (slice 7): dead ``or self._llm_client is None``
        # clause removed; mixin annotation is now ``LLMClient``.
        if interaction.interaction_id is None:
            logger.warning(
                "Closed interaction for agent %s has no interaction_id "
                "(scope=%s); skipping persistence",
                self.agent_id, interaction.scope,
            )
            return
        ctx: dict[str, Any] = {
            "scope": interaction.scope,
            "close_reason": interaction.close_reason,
            "turn_count": interaction.turn_count,
            "turns": [{"at": t.at, "payload": t.payload} for t in interaction.turns],
        }
        try:
            await self._episodic_memory.store_episode(
                summary=SUMMARY_PENDING_TEXT, context=ctx,
                interaction_id=interaction.interaction_id,
                started_at=interaction.started_at,
                closed_at=interaction.closed_at,
                turn_count=interaction.turn_count, scope=interaction.scope,
            )
        except Exception:
            logger.warning(
                "Failed to persist closed interaction for agent %s (scope=%s)",
                self.agent_id, interaction.scope, exc_info=True,
            )
            return
        # Phase 2: background summarise + finalise.  add_done_callback
        # auto-cleans the tracking set so references don't accumulate.
        task: asyncio.Task[None] = asyncio.create_task(
            finalize_closed_interaction(
                llm_client=self._llm_client, memory_ns=self._memory_ns,
                episodic=self._episodic_memory, agent_id=self.agent_id,
                interaction=interaction,
                on_finalized=self._tick_auto_reflect_counter,
            ),
        )
        self._pending_summarize_tasks.add(task)
        task.add_done_callback(self._pending_summarize_tasks.discard)

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
