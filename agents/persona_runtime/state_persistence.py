"""State persistence and memory lifecycle for _LLMPersonaAgent.

Handles serializing/deserializing persona state to the agent_state
table and managing the initialization/shutdown of all three memory tiers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..llm_client import LLMClient
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
    scope_for_dm,
    scope_for_thread,
)
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from ..persona_types import AgentAction, AgentEvent, EventType, PersonaState
from .summarize_close import (
    drain_pending_summary_tasks,
    finalize_closed_interaction,
)

logger = logging.getLogger(__name__)

__all__ = ["_StatePersistenceMixin"]


class _StatePersistenceMixin:
    """Mixin providing state persistence and memory lifecycle for _LLMPersonaAgent.

    Expects the following attributes on ``self`` (provided by the
    concrete ``_LLMPersonaAgent`` class and ``PersonaAgent`` base):

    - ``agent_id: str``
    - ``config: dict[str, Any]``
    - ``_state: PersonaState``
    - ``_episodic_memory: EpisodicMemory``
    - ``_relationship_memory: RelationshipMemory``
    - ``_working_memory: WorkingMemory``
    - ``_lock: asyncio.Lock``
    """

    # Attribute declarations for type checkers — set by __init__.
    agent_id: str
    config: dict[str, Any]
    _state: PersonaState
    _episodic_memory: EpisodicMemory
    _relationship_memory: RelationshipMemory
    _working_memory: WorkingMemory
    _lock: asyncio.Lock
    _interaction_tracker: InteractionTracker
    _llm_client: LLMClient | None
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
        try:
            # Step 1: flush any interaction whose idle window expired
            # since the last event.  Runs unconditionally so single-turn
            # paths also drive the cross-scope janitor without waiting
            # for PR 4.
            for closed in self._interaction_tracker.idle_check():
                await self._persist_closed_interaction(closed)

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
                    "_StatePersistenceMixin._{MULTI,SINGLE}_TURN_EVENT_TYPES.",
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
            interaction = self._interaction_tracker.add_turn(scope, payload=None)
            # ``close`` returns ``None`` only if the scope is empty; we
            # just opened it, so the fallback keeps the type checker
            # honest without changing runtime behavior.
            closed = self._interaction_tracker.close(
                scope, reason=REASON_STRUCTURAL,
            ) or interaction
            await self._episodic_memory.store_episode(
                summary=summary,
                context=ctx,
                interaction_id=closed.interaction_id,
                started_at=closed.started_at,
                closed_at=closed.closed_at,
                turn_count=closed.turn_count,
                scope=closed.scope,
            )
        except Exception:
            # PR-215 review nice-to-have #3: include the open scope so
            # operators can correlate an ``interactions.closed.by_structural``
            # counter increment with the missing episode row when
            # ``store_episode`` fails after ``close`` already popped the
            # scope and incremented the counter.  ``_open`` may already
            # be empty (close ran), so this best-effort grabs whatever
            # is still tracked under the event's scope.
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
        """Compute the InteractionTracker scope for a multi-turn event.

        Returns ``None`` when the event carries neither a ``channel_id``
        nor a ``sender_id`` — callers fall back to the legacy NULL-
        interaction shape so an under-populated event does not leak
        into a half-keyed scope.

        Channel-aware routing (group / thread distinction) lands jointly
        with RFC 0011 P3 in PR 5; for PR 3 the runtime treats every
        ``channel_id`` as a thread scope so existing chat traffic
        aggregates correctly.
        """
        if event.channel_id:
            return scope_for_thread(event.channel_id)
        if event.sender_id:
            return scope_for_dm(self.agent_id, event.sender_id)
        return None

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
        payload: dict[str, object] = {
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
        self._interaction_tracker.add_turn(scope, payload=payload)
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
        if interaction.turn_count == 0 or self._llm_client is None:
            return  # idle no-turn scope, or test bootstrap path.
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
        """Await in-flight background summary tasks (RFC 0020 PR 4)."""
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

    # ─── State persistence ─────────────────────────────

    async def _persist_persona_state(self) -> None:
        """Serialize persona state to the agent_state table.

        Uses EpisodicMemory's public ``persist_agent_state()`` API rather
        than reaching into its private DB handle (review finding #3).
        """
        try:
            state_json = json.dumps(self._state.to_dict())
            await self._episodic_memory.persist_agent_state(
                self.agent_id, state_json,
            )
        except Exception:
            logger.warning(
                "Failed to persist persona state for agent %s",
                self.agent_id,
                exc_info=True,
            )

    async def _load_persona_state(self) -> PersonaState:
        """Load persona state from the agent_state table, or return defaults.

        Uses EpisodicMemory's public ``load_agent_state()`` API.
        """
        try:
            state_json = await self._episodic_memory.load_agent_state(
                self.agent_id,
            )
            if state_json:
                return PersonaState.from_dict(json.loads(state_json))
        except Exception:
            logger.warning(
                "Failed to load persona state for agent %s, using defaults",
                self.agent_id,
                exc_info=True,
            )
        return PersonaState()

    # ─── Memory lifecycle ──────────────────────────────

    async def initialize_memory(self, *, shared_pools=None) -> None:  # type: ignore[no-untyped-def]
        """Initialize all memory tiers and load persisted state.

        ``shared_pools`` is accepted for signature parity with
        :meth:`BaseAgent.initialize_memory` (RFC 0008 PR plan PR 4) but
        is currently ignored by the persona runtime — persona agents do
        not yet route through the shared-pool API.  Wiring lands in a
        follow-on PR; the kwarg is accepted now so callers can pass it
        unconditionally.
        """
        await self._episodic_memory.initialize()
        await self._relationship_memory.initialize(
            config_relationships=self.config.get("relationships"),
        )
        await self._working_memory.initialize()
        self._state = await self._load_persona_state()

    async def close_memory(self) -> None:
        """Close all memory tiers, awaiting in-flight operations.

        Each tier closes in its own try/except so a failure in one
        does not prevent the rest from releasing resources (PR #54).
        RFC 0020 PR 4: drains pending background summary tasks first
        so they don't outlive the EpisodicMemory DB handle.
        """
        async with self._lock:
            await self._persist_persona_state()
            errors: list[Exception] = []
            try:
                await self.drain_pending_summaries()
            except Exception as exc:
                errors.append(exc)
                logger.warning(
                    "Failed to drain pending summaries on close: %s", exc,
                )
            # working (flush compression) → episodic (DB) → relationship (DB)
            for tier in (self._working_memory, self._episodic_memory, self._relationship_memory):
                try:
                    await tier.close()
                except Exception as exc:
                    errors.append(exc)
                    logger.warning("Failed to close memory tier: %s", exc)
            if errors:
                logger.error(
                    "Memory close for agent %s completed with %d error(s)",
                    self.agent_id,
                    len(errors),
                )
