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
from ..memory.interactions import SCOPE_TICK, InteractionTracker
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

    # RFC 0020 §G — multi-turn paths handled by PR 3; everything else is
    # routed through the InteractionTracker as a single-turn interaction.
    _MULTI_TURN_EVENT_TYPES: frozenset[EventType] = frozenset({
        EventType.MESSAGE_RECEIVED,
        EventType.MENTION,
    })

    async def _store_event_episode(
        self, event: AgentEvent, actions: list[AgentAction],
    ) -> None:
        """Persist the episode for a completed event (RFC 0020 PR 2).

        Single-turn paths (TICK, tool-only) route through
        :class:`InteractionTracker` so the row carries the new
        ``interaction_id`` / ``started_at`` / ``closed_at`` /
        ``turn_count`` / ``scope`` columns added in PR 1's schema
        migration.  Multi-turn paths (``MESSAGE_RECEIVED`` / ``MENTION``)
        keep the legacy NULL-interaction shape until PR 3 wires
        aggregation; the parity test in
        ``test_interaction_single_turn_parity.py`` pins this boundary.
        """
        try:
            summary = (
                f"Event: {event.event_type.value} → "
                f"Actions: {[a.action_type.value for a in actions]}"
            )
            ctx = {"event": event.payload, "sender": event.sender_id}
            if event.event_type in self._MULTI_TURN_EVENT_TYPES:
                await self._episodic_memory.store_episode(summary=summary, context=ctx)
                return
            interaction = self._interaction_tracker.add_turn(
                SCOPE_TICK,
                payload={
                    "event_type": event.event_type.value,
                    "actions": [a.action_type.value for a in actions],
                },
            )
            # ``close`` returns ``None`` only if the scope is empty; we
            # just opened it, so the fallback keeps the type checker
            # honest without changing runtime behavior.
            closed = self._interaction_tracker.close(
                SCOPE_TICK, reason=REASON_STRUCTURAL,
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
            logger.warning(
                "Failed to store episode for agent %s",
                self.agent_id, exc_info=True,
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
