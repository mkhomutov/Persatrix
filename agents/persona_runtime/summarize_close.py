"""Summarisation-on-close helpers for the persona runtime.

RFC 0020 PR 4 — extracted from
:mod:`agents.persona_runtime.state_persistence` to keep that module
under the 500-line code-file size cap (``scripts/checks/file_size.py``).

The helpers form the close-path summarisation pipeline:

1. :func:`summarize_closed_interaction` — fast path for single-turn
   interactions, LLM-call (bounded by timeout + ``MemoryFacade.compress``
   token budget) for multi-turn.
2. :func:`record_closed_interaction` — bumps the relationship row for
   DM-scoped interactions; best-effort.
3. :func:`extract_peer_from_interaction` — recovers ``(peer_id,
   peer_participant_type)`` from a ``dm:<a>:<b>`` scope.

All functions are module-level and ``self``-free so the per-call site in
the mixin stays a one-liner that satisfies the file-size guard.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..memory.facade import CompressedView, MemoryEntry, MemoryFacade
from ..memory.interactions import SUMMARY_UNAVAILABLE_TEXT
from ..observability.metrics import current_agent_id, try_get_instruments
from ..optimization import summarization_model
from ..prompt_loader import load_snippet

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from ..memory.episodic import EpisodicMemory
    from ..memory.interactions import Interaction
    from . import MemoryNamespace

logger = logging.getLogger(__name__)

# RFC 0020 PR 4 §"Summarisation hook".  The PR-plan (line ~190) pins
# the per-call timeout small enough to keep the close path responsive.
# 30s mirrors the default ``event_timeout`` for persona events; a
# slower model that exceeds it falls through to the fallback summary
# path so a stuck close never wedges the runtime.
SUMMARIZATION_TIMEOUT_SEC: float = 30.0

# RFC 0020 PR 4 cross-RFC pin: the ``MemoryFacade.compress`` target
# token budget for the per-interaction summarisation context.  RFC 0020
# §Security caps single-interaction context at 2k tokens to bound LLM
# cost; the value is shared with the abstractive path (RFC 0008 PR 5)
# so the contract does not drift across RFCs.
SUMMARIZATION_TARGET_TOKENS: int = 2000

# Maximum characters of the summary persisted to ``record_interaction``
# as the relationship outcome.  Mirrors the relationship-memory write
# path's defensive truncation so an unusually long LLM summary cannot
# blow up the relationship row's outcome column.
RECORD_INTERACTION_OUTCOME_CHARS: int = 200

# RFC 0020 PR 4 (PR #229 Should-Fix #2): on-tick janitor cooldown.
JANITOR_INTERVAL_SEC: float = 300.0


async def summarize_closed_interaction(
    llm_client: LLMClient,
    agent_id: str,
    interaction: Interaction,
) -> tuple[str, bool]:
    """Build an LLM-generated summary of a closed interaction.

    Returns ``(summary_text, failed_bool)``.  ``failed_bool`` is
    ``True`` iff the LLM path failed and the caller is looking at
    the :data:`SUMMARY_UNAVAILABLE_TEXT` fallback.  The ``failed``
    signal feeds the ``record_interaction`` outcome path — a
    relationship row should not anchor on a placeholder string.

    Single-turn interactions short-circuit to the deterministic
    per-turn summary so PR 2's tick fast path does not pay an LLM
    round-trip.  Multi-turn interactions go through
    :meth:`MemoryFacade.compress` to bound the context and then
    through the LLM.
    """
    if interaction.turn_count == 1:
        payload = interaction.turns[0].payload or {}
        single = str(payload.get("summary", "")).strip()
        if single:
            # Multi-turn placeholder shape kept for parity with PR 3
            # so legacy assertions on ``REASON_*`` substring matches
            # in the summary still pass.
            return (
                f"Multi-turn interaction (scope={interaction.scope}, "
                f"turns=1, reason={interaction.close_reason}): "
                f"first[{single}] last[{single}]",
                False,
            )

    entries = _interaction_to_entries(interaction)
    view: CompressedView = MemoryFacade.compress(
        entries,
        target_tokens=SUMMARIZATION_TARGET_TOKENS,
    )
    prompt = _build_summarization_prompt(interaction, view)
    try:
        response = await asyncio.wait_for(
            llm_client.create_message(
                model=summarization_model(),
                messages=[{"role": "user", "content": prompt}],
                system=load_snippet("episode-summarizer"),
                tools=[],
                max_tokens=256,
                temperature=0.2,
            ),
            timeout=SUMMARIZATION_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.warning(
            "Summarisation timed out for agent %s (scope=%s); using fallback",
            agent_id, interaction.scope,
        )
        _emit_summary_failed("timeout")
        return (SUMMARY_UNAVAILABLE_TEXT, True)
    except Exception as exc:
        logger.warning(
            "Summarisation failed for agent %s (scope=%s): %s",
            agent_id, interaction.scope, exc,
        )
        _emit_summary_failed("llm_error")
        return (SUMMARY_UNAVAILABLE_TEXT, True)

    text = (response.text or "").strip()
    if not text:
        logger.warning(
            "Summarisation returned empty text for agent %s (scope=%s); "
            "using fallback",
            agent_id, interaction.scope,
        )
        _emit_summary_failed("empty")
        return (SUMMARY_UNAVAILABLE_TEXT, True)
    return (text, False)


def _interaction_to_entries(interaction: Interaction) -> list[MemoryEntry]:
    """Project per-turn payloads into ``MemoryEntry`` shape for compress().

    Each turn becomes one entry; importance equals the turn ordinal
    normalised into ``(0, 1]`` so later turns weigh slightly more
    than openers when the compressor has to drop entries.
    """
    total = max(interaction.turn_count, 1)
    entries: list[MemoryEntry] = []
    for idx, turn in enumerate(interaction.turns, start=1):
        payload = turn.payload or {}
        content_parts: list[str] = []
        summary = str(payload.get("summary", "")).strip()
        if summary:
            content_parts.append(summary)
        sender = str(payload.get("sender", "")).strip()
        if sender:
            content_parts.append(f"sender={sender}")
        if not content_parts:
            content_parts.append(
                f"event_type={payload.get('event_type', 'unknown')}",
            )
        entries.append(MemoryEntry(
            id=f"turn-{idx}",
            content=" | ".join(content_parts),
            importance=idx / total,
            tags=(),
            created_at=turn.at,
            score=0.0,
        ))
    return entries


def _build_summarization_prompt(
    interaction: Interaction, view: CompressedView,
) -> str:
    """Render the summarisation prompt body.

    Mirrors the structure of
    :func:`agents.memory.episodic_retention.summarize_old_episodes` so
    the two LLM-summary call sites stay consistent.
    """
    return (
        load_snippet("interaction-summarizer") + "\n\n"
        f"Scope: {interaction.scope}\n"
        f"Turns: {interaction.turn_count}\n"
        f"Close reason: {interaction.close_reason}\n"
        f"Tokens (before compression / after): "
        f"{view.tokens_before} / {view.tokens_after}\n"
        f"Entries dropped during compression: {view.entries_dropped}\n\n"
        f"Compressed turns:\n{view.summary}\n"
    )


def _emit_summary_failed(reason: str) -> None:
    inst = try_get_instruments()
    if inst is None:
        return
    inst.interactions_summary_failed.add(
        1, {"agent_id": current_agent_id(), "reason": reason},
    )


async def record_closed_interaction(
    memory_ns: MemoryNamespace,
    agent_id: str,
    interaction: Interaction,
    summary: str,
    summary_failed: bool,
) -> None:
    """Bump the relationship row for a DM-scoped closed interaction.

    Skipped for non-DM scopes (thread / group / tick) and for
    interactions whose first turn payload does not carry a sender —
    those have no single peer to anchor a relationship row on.
    Channel-aware recording for thread / group scopes lands jointly
    with RFC 0011 P3 in PR 5.
    """
    if not interaction.scope.startswith("dm:"):
        return
    peer_id, peer_type = extract_peer_from_interaction(agent_id, interaction)
    if not peer_id:
        return
    if summary_failed:
        outcome: str | None = None
    else:
        stripped = summary.strip()
        outcome = (
            stripped[:RECORD_INTERACTION_OUTCOME_CHARS]
            if stripped else None
        )
    try:
        await memory_ns.relationship.record_interaction(
            other_id=peer_id,
            interaction_type="conversation",
            outcome=outcome,
            other_participant_type=peer_type,
        )
    except Exception:
        logger.warning(
            "Failed to record interaction for agent %s with peer %s",
            agent_id, peer_id, exc_info=True,
        )


def extract_peer_from_interaction(
    agent_id: str, interaction: Interaction,
) -> tuple[str | None, str]:
    """Recover ``(peer_id, peer_participant_type)`` from a DM scope.

    DM scopes are formatted ``dm:<a>:<b>`` with the two ids sorted
    lexicographically (see :func:`agents.memory.interactions.scope_for_dm`).
    The peer is the id that is not ``agent_id``.  Participant type
    defaults to ``agent`` and is upgraded to whatever the first turn
    payload's ``participant_type`` field carries (set by the chat
    servicer for human inbound turns).
    """
    body = interaction.scope[len("dm:"):]
    parts = body.split(":", 1)
    if len(parts) != 2:
        return (None, "agent")
    a, b = parts
    peer = b if a == agent_id else a if b == agent_id else None
    if peer is None:
        return (None, "agent")
    # PR #229 review Should-Fix #4: defensive self-DM guard.  A
    # ``dm:<id>:<id>`` scope (where both sides equal the agent's own
    # id) would otherwise return ``(agent_id, ...)`` and let
    # ``record_interaction`` write a self-relationship row.  The
    # current ``scope_for_dm`` sorts but does not de-duplicate, so
    # this is reachable if a future caller passes
    # ``self.agent_id`` as ``other_id`` either intentionally
    # (self-talk) or via a routing bug.  Treat as "no peer".
    if peer == agent_id:
        return (None, "agent")
    peer_type = "agent"
    if interaction.turns:
        first_payload = interaction.turns[0].payload or {}
        raw = first_payload.get("participant_type")
        if isinstance(raw, str) and raw in {"agent", "user"}:
            peer_type = raw
    return (peer, peer_type)


# ─── Two-phase close-path tail (PR #229 review Must-Fix #1) ─────────
#
# Extracted from ``_StatePersistenceMixin`` so the mixin module stays
# under the 500-line code-file size cap (``scripts/checks/file_size.py``).
# These helpers run **outside** the agent ``_lock`` so a second inbound
# event for the same agent does not queue head-of-line behind the LLM
# round-trip.  They are best-effort: the ``[summary pending]`` row is
# already persisted by Phase 1, so a failure here just leaves the
# janitor a row to upgrade rather than losing the interaction.


async def finalize_closed_interaction(
    *,
    llm_client: LLMClient,
    memory_ns: MemoryNamespace,
    episodic: EpisodicMemory,
    agent_id: str,
    interaction: Interaction,
    on_finalized: Callable[[], Awaitable[None]],
) -> None:
    """Background tail of the two-phase close path (RFC 0020 PR 4).

    Runs the LLM summariser, ``UPDATE``s the pending row, bumps the
    relationship row, and invokes ``on_finalized`` (used by the mixin
    to tick the auto-reflect counter).  Top-level guarded so a failure
    does not surface as ``Task exception was never retrieved`` at GC.
    """
    try:
        assert interaction.interaction_id is not None
        summary, summary_failed = await summarize_closed_interaction(
            llm_client, agent_id, interaction,
        )
        try:
            updated = await episodic.update_episode_summary(
                interaction.interaction_id, summary,
            )
            if not updated:
                logger.warning(
                    "Pending-summary row not found for agent %s "
                    "(interaction_id=%s); janitor will not see this row",
                    agent_id, interaction.interaction_id,
                )
        except Exception:
            logger.warning(
                "Failed to update summary for agent %s (interaction_id=%s); "
                "row will be backfilled by the janitor",
                agent_id, interaction.interaction_id, exc_info=True,
            )
        await record_closed_interaction(
            memory_ns, agent_id, interaction, summary, summary_failed,
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
    return now_monotonic
