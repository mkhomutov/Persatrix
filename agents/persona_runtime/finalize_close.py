"""Two-phase close-path tail for the persona runtime (PR #229 review Must-Fix #1).

Extracted from :mod:`agents.persona_runtime.summarize_close` to keep that
module under the 500-line code-file size cap (``scripts/checks/file_size.py``):
the summariser LLM call and this background tail are the two distinct concerns
the split file's docstring already named, and this half depends on the former
(``finalize_closed_interaction`` runs :func:`summarize_closed_interaction`), so
the dependency is one-way (``finalize_close`` → ``summarize_close``, no cycle).

These helpers run **outside** the agent ``_lock`` so a second inbound event for
the same agent does not queue head-of-line behind the LLM round-trip.  They are
best-effort: the ``[summary pending]`` row is already persisted by Phase 1, so a
failure here just leaves the janitor a row to upgrade rather than losing the
interaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..memory.projections import ENTRY_TIER_EPISODE, replace_entry_projections
from ..observability.metrics import current_agent_id, try_get_instruments
from .fact_extractor import dispatch_facts_from_response
from .record_close import record_closed_interaction
from .summarize_close import summarize_closed_interaction

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from ..memory.episodic import EpisodicMemory
    from ..memory.interactions import Interaction
    from . import MemoryNamespace

logger = logging.getLogger(__name__)

# RFC 0020 PR 4 (PR #229 Should-Fix #2): on-tick janitor cooldown.
JANITOR_INTERVAL_SEC: float = 300.0


async def finalize_closed_interaction(
    *,
    llm_client: LLMClient,
    memory_ns: MemoryNamespace,
    episodic: EpisodicMemory,
    agent_id: str,
    interaction: Interaction,
    on_finalized: Callable[[], Awaitable[None]],
    session_id: str = "legacy",
) -> None:
    """Background tail of the two-phase close path (RFC 0020 PR 4).

    Runs the LLM summariser, ``UPDATE``s the pending row, bumps the
    relationship row, and invokes ``on_finalized`` (used by the mixin
    to tick the auto-reflect counter).  Top-level guarded so a failure
    does not surface as ``Task exception was never retrieved`` at GC.

    PR 6 review #20: when ``update_episode_summary`` returns ``False``
    the janitor has already finalised the row to
    :data:`SUMMARY_UNAVAILABLE_TEXT` — skip the relationship bump and
    the auto-reflect tick so the janitor's verdict is the single
    source of truth and the failure counter cannot double-increment.
    """
    # PR 6 review #21: explicit guard rather than ``assert`` so a future
    # Phase-1 reorder cannot let ``None`` through silently under
    # ``python -O`` (where ``assert`` is stripped).
    if interaction.interaction_id is None:
        logger.warning(
            "Closed interaction for agent %s has no interaction_id "
            "(scope=%s); skipping background finalisation",
            agent_id, interaction.scope,
        )
        return
    try:
        (
            summary, summary_failed, facts_raw, projections,
        ) = await summarize_closed_interaction(
            llm_client, agent_id, interaction,
        )
        try:
            updated = await episodic.update_episode_summary(
                interaction.interaction_id, summary,
            )
        except Exception:
            logger.warning(
                "Failed to update summary for agent %s (interaction_id=%s); "
                "row will be backfilled by the janitor",
                agent_id, interaction.interaction_id, exc_info=True,
            )
            return
        if not updated:
            # Janitor already wrote SUMMARY_UNAVAILABLE_TEXT (or the row
            # vanished); its decision is final.  No relationship bump,
            # no auto-reflect tick — both already accounted for in the
            # janitor sweep that owned the row.
            logger.info(
                "Phase 2 superseded by janitor for agent %s "
                "(interaction_id=%s); skipping relationship + auto-reflect",
                agent_id, interaction.interaction_id,
            )
            return
        # RFC 0026 PR 2 — facts write follows the summary commit so
        # the audit ordering matches the data ordering: summary
        # always exists before any facts.store row pointing back at
        # this ``interaction_id``.  Per-tuple failures (allowlist
        # miss, missing field, certainty range) increment
        # ``agent.facts.extraction_failed`` inside
        # :func:`store_extracted_facts` — one bad tuple does not drop
        # the rest of the batch.  Inner facts-list parse failure
        # bumps the same counter once inside
        # :func:`dispatch_facts_from_response`.  Outer-envelope parse
        # failures are a distinct signal (``envelope_parse_failed``,
        # RFC 0026 PR 5b) emitted at the split catch above.
        if (
            not summary_failed
            and facts_raw is not None
            and memory_ns.facts is not None
        ):
            await dispatch_facts_from_response(
                fact_store=memory_ns.facts,
                facts_raw=facts_raw,
                interaction=interaction,
                agent_id=agent_id,
                session_id=session_id,
            )
        # RFC 0037 §E (PR 6) — persist the declassified projections the
        # combined call returned for a protected interaction, keyed by the
        # interaction id the ``episodes`` row also carries.  Best-effort
        # like the facts half: a failed write leaves the entry
        # blunt-withheld at the gate (the Phase-1 posture), never fails
        # the close.  Non-empty only when the summary succeeded, so no
        # extra guard is needed.
        if projections:
            try:
                await replace_entry_projections(
                    episodic,
                    entry_id=interaction.interaction_id,
                    entry_tier=ENTRY_TIER_EPISODE,
                    projections=projections,
                    created_at=interaction.closed_at or interaction.started_at,
                )
            except Exception:
                logger.warning(
                    "Failed to persist §E projections for agent %s "
                    "(interaction_id=%s)",
                    agent_id, interaction.interaction_id, exc_info=True,
                )
        await record_closed_interaction(
            memory_ns, agent_id, interaction, summary, summary_failed,
            session_id=session_id,
        )
        await on_finalized()
    except Exception:
        logger.warning(
            "Background summary finalisation failed for agent %s "
            "(scope=%s)",
            agent_id, interaction.scope, exc_info=True,
        )


async def drain_pending_summary_tasks(
    pending: set[asyncio.Task[None]],
) -> None:
    """Await every in-flight background summary task.

    Snapshot semantics: a task spawned during the await is picked up
    by the next call rather than this one, which is what callers want
    on shutdown (``close_memory``) and in tests.
    """
    snapshot = list(pending)
    if snapshot:
        await asyncio.gather(*snapshot, return_exceptions=True)


async def maybe_run_janitor(
    cleanup: Callable[[], Awaitable[int]],
    last_monotonic: float | None,
    now_monotonic: float,
    interval_sec: float,
    agent_id: str,
) -> float | None:
    """Run the closing-state janitor if the cooldown has elapsed.

    Returns the new ``last_monotonic`` (caller stores it on the agent).
    Best-effort: any failure is logged and swallowed so a janitor
    hiccup never breaks the tick path.  See PR #229 review Should-Fix
    #2.

    PR 6 review #24 — sweep failures increment
    ``agent.interactions.janitor.failed`` so a persistent outage
    (under which stuck rows accumulate at one cooldown window per
    failure) raises an operator SLO signal instead of silently
    advancing the cooldown.
    """
    if last_monotonic is not None and now_monotonic - last_monotonic < interval_sec:
        return last_monotonic
    try:
        await cleanup()
    except Exception:
        logger.warning(
            "Janitor sweep failed for agent %s",
            agent_id, exc_info=True,
        )
        inst = try_get_instruments()
        if inst is not None:
            inst.interactions_janitor_failed.add(
                1, {"agent_id": current_agent_id()},
            )
    return now_monotonic
