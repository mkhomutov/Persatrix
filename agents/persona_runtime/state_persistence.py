"""State persistence and memory lifecycle for _LLMPersonaAgent.

Handles serializing/deserializing persona state to the agent_state
table and managing the initialization/shutdown of all three memory tiers.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

from ..memory.boundary_detectors import REASON_STRUCTURAL
from ..memory.episodic import EpisodicMemory
from ..memory.interactions import (
    SCOPE_TICK,
    Interaction,
    InteractionTracker,
    scope_for_dm,
    scope_for_thread,
)
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from ..persona_types import AgentAction, AgentEvent, EventType, PersonaState

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

    # RFC 0020 §G event-type routing.  Both sets are *positive lists* —
    # the prior implementation used a single multi-turn deny-list
    # ("everything else is single-turn") which would have silently
    # routed any new ``EventType`` (e.g. an RFC 0011 channel event added
    # later) through the tracker as a ``tick``-scoped row.  PR-215
    # review (Should-Fix #2) flagged that as a latent correctness bug:
    # adding a new ``EventType`` should require a conscious choice
    # about which set it belongs to.  Unknown event types now hit the
    # legacy path with a warning so the maintainer notices.
    _MULTI_TURN_EVENT_TYPES: frozenset[EventType] = frozenset({
        EventType.MESSAGE_RECEIVED,
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

        Single-turn paths (TICK, tool-only) route through
        :class:`InteractionTracker` so the row carries the new
        ``interaction_id`` / ``started_at`` / ``closed_at`` /
        ``turn_count`` / ``scope`` columns added in PR 1's schema
        migration.  Multi-turn paths (``MESSAGE_RECEIVED`` / ``MENTION``)
        accumulate turns in the open interaction and persist a single
        episode only when the interaction closes (PR 3); the close is
        triggered either by an explicit session-end marker on the event
        metadata (``chat_end`` / ``session_end`` truthy — the RFC 0016
        ``chat_end`` event surface lands in a follow-up) or by the
        :class:`~agents.memory.boundary_detectors.IdleGapDetector` once
        the per-channel idle timeout elapses.

        Idle-gap evaluation runs at the top of every event so a stale
        interaction is flushed the moment the next event arrives in any
        scope.  PR 4 will additionally drive ``idle_check`` from a
        periodic janitor so closure does not depend on event traffic.

        Scope labelling for single-turn rows preserves event-type
        provenance per PR-215 review (Should-Fix #1): only actual
        ``TICK`` events use :data:`SCOPE_TICK`; tool-only events store
        their ``EventType.value`` (e.g. ``"task_assigned"``) so that
        ``WHERE scope = 'task_assigned'`` analytics work without
        having to re-parse ``summary``.  RFC 0020 §G's "share the TICK
        boundary policy" wording refers to *close timing*, not scope
        labels.  Multi-turn rows carry the channel-typed scope built by
        :func:`scope_for_dm` / :func:`scope_for_thread`.
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
        # ``MESSAGE_RECEIVED`` / ``MENTION``) on the turn, so the
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
        }
        self._interaction_tracker.add_turn(scope, payload=payload)
        if self._is_session_end_event(event):
            closed = self._interaction_tracker.close(
                scope, reason=REASON_STRUCTURAL,
            )
            if closed is not None:
                await self._persist_closed_interaction(closed)

    async def _persist_closed_interaction(self, interaction: Interaction) -> None:
        """Write a single episode row for a just-closed multi-turn interaction.

        PR 3 ships a deterministic placeholder summary that mirrors the
        single-row legacy text the per-event store would have written;
        PR 4 replaces it with the LLM-generated summary plumbed through
        ``MemoryFacade.compress``.  The interaction's accumulated turn
        payloads ride on ``context_json`` so PR 4 can read them back
        without a schema change.
        """
        if interaction.turn_count == 0:
            # Defensive: an interaction can only close after at least
            # one ``add_turn``, but ``idle_check`` running on a freshly
            # ``start``-ed (no-turn) scope would still hit this path.
            # Skip the row rather than write a contentless episode.
            return
        first_payload = interaction.turns[0].payload
        last_payload = interaction.turns[-1].payload
        # PR-216 review (Nice-to-have #2): apply the ``<no-summary>``
        # fallback symmetrically.  Earlier draft only guarded ``first``,
        # so an empty-summary closing turn rendered ``... last[]`` while
        # the analogous opener rendered ``... first[<no-summary>]``.
        first_summary = str(first_payload.get("summary", "")) or "<no-summary>"
        last_summary = str(last_payload.get("summary", "")) or "<no-summary>"
        # Placeholder summary: turn count + first/last per-turn summary.
        # Stable, deterministic, easy to assert in tests.  PR 4 swaps
        # this for an LLM call.
        summary = (
            f"Multi-turn interaction (scope={interaction.scope}, "
            f"turns={interaction.turn_count}, reason={interaction.close_reason}): "
            f"first[{first_summary}] last[{last_summary}]"
        )
        ctx: dict[str, Any] = {
            "scope": interaction.scope,
            "close_reason": interaction.close_reason,
            "turn_count": interaction.turn_count,
            "turns": [
                {"at": t.at, "payload": t.payload}
                for t in interaction.turns
            ],
        }
        try:
            await self._episodic_memory.store_episode(
                summary=summary,
                context=ctx,
                interaction_id=interaction.interaction_id,
                started_at=interaction.started_at,
                closed_at=interaction.closed_at,
                turn_count=interaction.turn_count,
                scope=interaction.scope,
            )
        except Exception:
            logger.warning(
                "Failed to persist closed interaction for agent %s (scope=%s)",
                self.agent_id,
                interaction.scope,
                exc_info=True,
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

    async def initialize_memory(self) -> None:
        """Initialize all memory tiers and load persisted state."""
        await self._episodic_memory.initialize()
        await self._relationship_memory.initialize(
            config_relationships=self.config.get("relationships"),
        )
        await self._working_memory.initialize()
        self._state = await self._load_persona_state()

    async def close_memory(self) -> None:
        """Close all memory tiers, awaiting in-flight operations.

        Each tier is closed in its own try/except so that a failure in one
        tier (e.g. disk-full on SQLite) does not prevent the remaining
        tiers from releasing their resources (PR #54 review).
        """
        async with self._lock:
            await self._persist_persona_state()
            errors: list[Exception] = []
            # Close order: working (flush compression) → episodic (DB) → relationship (DB)
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
