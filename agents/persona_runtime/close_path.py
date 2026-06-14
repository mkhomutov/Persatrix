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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..memory.episodic import EpisodicMemory
from ..memory.interactions import SUMMARY_PENDING_TEXT, Interaction
from .summarize_close import finalize_closed_interaction

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from . import MemoryNamespace

logger = logging.getLogger(__name__)

__all__ = ["persist_closed_interaction"]


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
    # PR-4 review #25 (slice 7): dead ``or llm_client is None`` clause removed;
    # the mixin annotation is now ``LLMClient`` (non-optional).
    if interaction.interaction_id is None:
        logger.warning(
            "Closed interaction for agent %s has no interaction_id "
            "(scope=%s); skipping persistence",
            agent_id, interaction.scope,
        )
        return
    ctx: dict[str, Any] = {
        "scope": interaction.scope,
        "close_reason": interaction.close_reason,
        "turn_count": interaction.turn_count,
        # ISSUE-0102: persist the RFC 0030 governance interaction id this
        # episode was opened under (``wire_interaction_id``, otherwise
        # in-memory-only) so the read surface can expose it alongside the
        # agent-side ``interaction_id``. The two segment on independent clocks,
        # so a single governance interaction can map to several episode ids;
        # carrying it here makes the channel-side id — the one the end-vote
        # close logs carry — cross-referenceable. Empty for a DM / thread /
        # non-channel interaction that never carried a governance id; omitted
        # from the surface in that case.
        "governance_interaction_id": interaction.wire_interaction_id,
        # ISSUE-0054 / RFC 0020 §D — strip the inbound message ``text`` the
        # multi-turn path stashes for the RFC 0026 extractor: Phase 2 reads it
        # off the in-memory interaction, so the persisted ``context_json``
        # stays body-free.
        "turns": [
            {"at": t.at, "payload": {
                k: v for k, v in t.payload.items() if k != "text"}}
            for t in interaction.turns
        ],
    }
    try:
        await episodic.store_episode(
            summary=SUMMARY_PENDING_TEXT, context=ctx,
            interaction_id=interaction.interaction_id,
            started_at=interaction.started_at,
            closed_at=interaction.closed_at,
            turn_count=interaction.turn_count, scope=interaction.scope,
            # ISSUE-0081 PR 2 sibling-mislabel guard: tag with the session the
            # interaction was *born* under, not the scope bound now —
            # ``idle_check`` may be flushing a sibling conversation's stale
            # interaction while a different conversation's event holds the scope.
            session_id=interaction.session_id)
    except Exception:
        logger.warning(
            "Failed to persist closed interaction for agent %s (scope=%s)",
            agent_id, interaction.scope, exc_info=True,
        )
        return
    # Phase 2: background summarise + finalise.  add_done_callback auto-cleans
    # the tracking set so references don't accumulate.
    task: asyncio.Task[None] = asyncio.create_task(
        finalize_closed_interaction(
            llm_client=llm_client, memory_ns=memory_ns,
            episodic=episodic, agent_id=agent_id,
            interaction=interaction,
            on_finalized=on_finalized,
            # Phase-2 facts + relationship writes must match the Phase-1 row's
            # session (sibling-mislabel guard) — use the interaction's frozen
            # session, not the bound scope.
            session_id=interaction.session_id,
        ),
    )
    pending_tasks.add(task)
    task.add_done_callback(pending_tasks.discard)
