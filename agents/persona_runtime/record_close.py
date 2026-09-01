"""Relationship-recording helpers for the persona close path.

Split out of :mod:`agents.persona_runtime.summarize_close` to keep that
module under the 500-line code-file size cap
(``scripts/checks/file_size.py``).  These two helpers form the
relationship-memory half of interaction close — distinct from the
summarisation half that stays in ``summarize_close``:

* :func:`record_closed_interaction` — bumps the relationship row for a
  DM-scoped closed interaction; best-effort.
* :func:`extract_peer_from_interaction` — recovers ``(peer_id,
  peer_participant_type)`` from a ``dm:<a>:<b>`` scope.

Both are module-level and ``self``-free; the module imports nothing
from ``summarize_close`` so the dependency runs one way
(``summarize_close`` → ``record_close``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.interactions import Interaction
    from . import MemoryNamespace

logger = logging.getLogger(__name__)

# Maximum characters of the summary persisted to ``record_interaction``
# as the relationship outcome.  Mirrors the relationship-memory write
# path's defensive truncation so an unusually long LLM summary cannot
# blow up the relationship row's outcome column.
RECORD_INTERACTION_OUTCOME_CHARS: int = 200


async def record_closed_interaction(
    memory_ns: MemoryNamespace,
    agent_id: str,
    interaction: Interaction,
    summary: str,
    summary_failed: bool,
    *,
    session_id: str = "legacy",
) -> None:
    """Bump the relationship row for a DM-scoped closed interaction.

    Skipped for non-DM scopes (thread / group / tick) and for
    interactions whose first turn payload does not carry a sender —
    those have no single peer to anchor a relationship row on.
    Channel-aware recording for thread / group scopes lands jointly
    with RFC 0011 P3 in PR 5.

    Also skipped for a REPLAYED span (v0.3.15 PR B2 review).  The peer's
    participant type is recovered from the first turn's payload, which
    the live ingress fills from ``sender_participant_type`` wire metadata
    — and ``channelMessageResponse`` (the REST history shape catch-up
    replays from) carries no such field, so there is nothing to seed and
    :func:`extract_peer_from_interaction` falls to its ``"agent"``
    default.  Since ``other_participant_type`` is part of the
    relationships row's conflict key, deriving a replayed DM span would
    mint a SECOND, ``agent``-typed relationship row for a real person
    rather than bump their existing one.  Unreachable until this release:
    shape (a) returned before Phase 2 for every replayed span, so the
    default was never exercised on this path.  Recording a peer type the
    replay cannot establish is the same trade ISSUE-0130 refuses
    everywhere else, so the relationship bump waits for a wire field to
    carry the type.
    """
    if not interaction.scope.startswith("dm:"):
        return
    if interaction.replayed:
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
            session_id=session_id,
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
