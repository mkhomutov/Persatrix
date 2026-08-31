"""What a persona does with an interaction once it is CLOSED.

Split out of :mod:`agents.persona_runtime.episode_routing` (v0.3.15 PR B2
review round 3), which crossed the 500-line cap
``scripts/checks/file_size.py --strict`` enforces.  The seam is not
arbitrary: ``episode_routing`` owns the per-event ROUTING surface, all of
it on the dispatch hot path, while everything here runs AFTER a record is
closed and on entirely different schedules — the two-phase write and its
background tail, the boot-path catch-up sweep, the shutdown drain, and the
``closing``-row janitor.

``_EpisodeRoutingMixin`` INHERITS this rather than the concrete agent
composing both.  That keeps ``_LLMPersonaAgent``'s base list unchanged (it
has its own headroom test), and it keeps the room-close fans in
:mod:`.vote_close` and :mod:`.close_notification` — which are typed against
``_EpisodeRoutingMixin`` and call ``_persist_closed_interaction`` — working
without an intersection type, which Python cannot express.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..llm_client import LLMClient
from ..memory.boundary_detectors import DEFAULT_CLOSING_GRACE_SEC
from ..memory.episodic import EpisodicMemory
from ..memory.interactions import InteractionTracker, cleanup_closing_interactions
from .close_path import persist_closed_interaction
from .finalize_close import drain_pending_summary_tasks
from .replay_sweep import close_replayed_scopes

if TYPE_CHECKING:
    from ..memory.interactions import Interaction
    from . import MemoryNamespace

logger = logging.getLogger(__name__)

__all__ = ["_CloseLifecycleMixin"]


class _CloseLifecycleMixin:
    """The close-path tail of ``_LLMPersonaAgent`` — see module docstring.

    Expects the same agent attributes ``_EpisodeRoutingMixin`` does; they
    are re-declared here because this is the class that reads them.
    """

    agent_id: str
    config: dict[str, Any]
    _episodic_memory: EpisodicMemory
    _interaction_tracker: InteractionTracker
    _llm_client: LLMClient
    _memory_ns: MemoryNamespace
    _pending_summarize_tasks: set[asyncio.Task[None]]

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
        self, *,
        derive_channels: frozenset[str] | None = None,
        speaker_gaps: frozenset[tuple[str, str]] | None = None,
    ) -> int:
        """The catch-up caller's ISSUE-0130 hook — see
        :func:`agents.persona_runtime.replay_sweep.close_replayed_scopes`,
        which states what ``derive_channels`` and ``speaker_gaps`` mean, the
        third condition a replayed record must meet to derive, and why the
        boot summarise burst is bounded by the Phase-2 tasks themselves.
        """
        return await close_replayed_scopes(
            self._interaction_tracker, self._persist_closed_interaction,
            derive_channels=derive_channels, speaker_gaps=speaker_gaps,
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
