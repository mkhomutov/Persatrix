"""Persona state persistence + memory-tier lifecycle for ``_LLMPersonaAgent``.

Handles serialising/deserialising the persona state row plus the
initialise/close lifecycle for the three memory tiers (episodic,
relationship, working).  RFC 0020 episode routing + close-path
orchestration was extracted to
:mod:`agents.persona_runtime.episode_routing` so this module stays
under the 500-line file-size cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..memory.episodic import EpisodicMemory
from ..memory.relationship import RelationshipMemory
from ..memory.working import WorkingMemory
from ..persona_types import PersonaState

logger = logging.getLogger(__name__)

__all__ = ["_StatePersistenceMixin"]


class _StatePersistenceMixin:
    """Mixin providing state persistence and memory lifecycle for ``_LLMPersonaAgent``.

    Expects the following attributes on ``self`` (provided by the
    concrete ``_LLMPersonaAgent`` class and ``PersonaAgent`` base):

    - ``agent_id: str``
    - ``config: dict[str, Any]``
    - ``_state: PersonaState``
    - ``_episodic_memory: EpisodicMemory``
    - ``_relationship_memory: RelationshipMemory``
    - ``_working_memory: WorkingMemory``
    - ``_lock: asyncio.Lock``

    Cooperates with
    :class:`~agents.persona_runtime.episode_routing._EpisodeRoutingMixin`
    via :meth:`drain_pending_summaries` (provided by that mixin) which
    :meth:`close_memory` calls under ``self._lock`` before tearing the
    DB handles down.
    """

    # Attribute declarations for type checkers — set by __init__.
    agent_id: str
    config: dict[str, Any]
    _state: PersonaState
    _episodic_memory: EpisodicMemory
    _relationship_memory: RelationshipMemory
    _working_memory: WorkingMemory
    _lock: asyncio.Lock
    # RFC 0031 Phase 1: resolved from PERSATRIX_SESSION_ID in
    # PersonaAgent.__init__ (see agents/persona.py).
    _session_id: str

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

        RFC 0031 PR plan PR 4 finding #2: ``_session_id`` is threaded
        into :meth:`RelationshipMemory.initialize` so a config-seeded
        peer row carries the active session's tag from the start.
        Without this, the seed inserted ``"legacy"`` and the
        first-seen-wins contract on ``record_interaction`` prevented a
        later overwrite — MT-SESSION-001 Step 7 silently failed.
        """
        await self._episodic_memory.initialize()
        await self._relationship_memory.initialize(
            config_relationships=self.config.get("relationships"),
            session_id=self._session_id,
        )
        await self._working_memory.initialize()
        self._state = await self._load_persona_state()

    async def close_memory(self) -> None:
        """Close all memory tiers, awaiting in-flight operations.

        Each tier closes in its own try/except so a failure in one
        does not prevent the rest from releasing resources (PR #54).
        RFC 0020 PR 4: drains pending background summary tasks first
        so they don't outlive the EpisodicMemory DB handle.
        ``drain_pending_summaries`` is provided by
        :class:`~agents.persona_runtime.episode_routing._EpisodeRoutingMixin`.
        """
        async with self._lock:
            await self._persist_persona_state()
            errors: list[Exception] = []
            try:
                await self.drain_pending_summaries()  # type: ignore[attr-defined]
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
