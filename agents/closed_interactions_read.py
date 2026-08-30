"""Closed-interaction summary read handler (v0.3.8 interaction-summary surface).

Houses the body of ``AgentServiceServicer.GetClosedInteractions`` as a
free function so ``agents/server_servicers.py`` stays under the 500-line
review cap enforced by ``scripts/checks/file_size.py --strict`` — the
same extraction precedent as :mod:`agents.chat_reply` (split out of the
servicer in RFC 0011 PR 4a-i).

The RFC 0020 per-interaction summary is *generated* at close (idle,
structural/end-vote, or the RFC 0030 Layer 1 cost ceiling); this read
path only *surfaces* it, so the web console + CLI can show the
synthesised outcome of a converged brainstorm. The
``"[interaction summary unavailable]"`` sentinel is forwarded verbatim
(SS3) — a failed summary is shown honestly, never blanked.
"""

from __future__ import annotations

import logging

import grpc
import grpc.aio

from .base import BaseAgent
from .generated import task_pb2
from .memory.episodic_closed import closed_interactions
from .memory.interaction_types import ROOM_CLOSE_TURN_KEY

__all__ = ["handle_get_closed_interactions", "DEFAULT_CLOSED_INTERACTION_LIMIT"]

logger = logging.getLogger("Persatrix.agent.server")

# Default page size when the request omits ``limit`` (0). The episodic
# query clamps to ``MAX_RECALL_LIMIT`` regardless, so this is only the
# unspecified-request default, not a hard cap.
DEFAULT_CLOSED_INTERACTION_LIMIT = 20


def _participants_from_context(ctx: object) -> list[str]:
    """Distinct participant ids (senders) from a persisted episode context.

    Two persisted context shapes carry the sender(s) (see
    :func:`agents.persona_runtime.close_path.persist_closed_interaction`
    for the multi-turn shape and the single-turn branch of
    :meth:`_EpisodeRoutingMixin._store_event_episode`):

    * multi-turn — ``{"turns": [{"payload": {"sender": ...}}, ...]}``: one
      id per turn, deduplicated in first-seen order.
    * single-turn — ``{"sender": "..."}``: the bare event sender.

    Empty / missing / non-string senders are skipped, so a legacy row that
    predates turn capture (or an autonomous TICK with no sender) yields an
    empty list rather than a ``[""]`` artefact.

    The RFC 0020 §G room-close turn is skipped (PR #846 review).  Since
    the v0.3.15 ``(principal, speaker, scope)`` re-key a record holds one
    speaker's turns, and the single exception is the close-notification
    fan's closing message, which lands on EVERY sibling record — so
    without this every closed record in a room named its own speaker plus
    the closer, a participant of that record's conversation only in the
    sense that it ended it.  Keyed off the producer's
    :data:`~agents.memory.interaction_types.ROOM_CLOSE_TURN_KEY`
    stamp, which survives persistence, rather than re-deriving
    ``sender`` ≠ the record's speaker here.

    Deliberately a DIFFERENT rule from the write-side
    :func:`agents.persona_runtime.close_entries.is_foreign_room_close_turn`
    (PR #849 review): that predicate keeps the NATIVE close turn (the
    closer's own record genuinely contains it), while participants drop
    EVERY stamped turn — a close event is not a conversational
    participation even on the closer's record — and this spelling must
    also cover pre-#849 rows whose ``context_json`` still holds the
    foreign turn the write side now filters out.  Do not "unify" the
    two without re-reading both rationales.
    """
    if not isinstance(ctx, dict):
        return []
    out: list[str] = []

    def _add(value: object) -> None:
        if isinstance(value, str) and value and value not in out:
            out.append(value)

    turns = ctx.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            payload = turn.get("payload") if isinstance(turn, dict) else None
            if isinstance(payload, dict):
                if payload.get(ROOM_CLOSE_TURN_KEY) is True:
                    continue  # the §G room-close turn — not a participant
                _add(payload.get("sender"))
    else:
        _add(ctx.get("sender"))
    return out


def _governance_id(ep: object) -> str:
    """The RFC 0030 governance interaction id for a closed-interaction row.

    ISSUE-0102: PR 2 promoted this to the ``governance_interaction_id`` column
    (migration v15); read it from there, falling back to the PR-1 context-blob
    key only when the column is empty (a row written by an older agent process
    after the v15 schema landed, not yet rewritten/backfilled). Returns ""
    when neither carries one — never ``None`` (the proto field is a string).
    """
    column = getattr(ep, "governance_interaction_id", None)
    if isinstance(column, str) and column:
        return column
    ctx = getattr(ep, "context", None)
    if isinstance(ctx, dict):
        value = ctx.get("governance_interaction_id")
        if isinstance(value, str):
            return value
    return ""


async def handle_get_closed_interactions(
    agents: dict[str, BaseAgent],
    request: task_pb2.ClosedInteractionsRequest,
    context: grpc.aio.ServicerContext,
) -> task_pb2.ClosedInteractionsResponse:
    """Read closed-interaction summaries for one agent (read-only).

    Returns an empty response (not an error) when the agent exists but
    has no episodic store — a task agent or a memory-disabled persona
    simply has no interactions to surface.
    """
    agent_id = request.agent_id
    if not agent_id:
        context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
        context.set_details("agent_id is required")
        return task_pb2.ClosedInteractionsResponse()

    agent = agents.get(agent_id)
    if agent is None:
        context.set_code(grpc.StatusCode.NOT_FOUND)
        context.set_details(f"Agent not found: {agent_id}")
        return task_pb2.ClosedInteractionsResponse()

    # Persona agents expose ``memory`` as a ``MemoryNamespace`` whose
    # ``.episodic`` is the ``EpisodicMemory`` tier; task agents expose a
    # bare ``MemoryStore`` with no ``episodic`` attribute (→ empty).
    episodic = getattr(getattr(agent, "memory", None), "episodic", None)
    if episodic is None:
        return task_pb2.ClosedInteractionsResponse()

    episodes = await closed_interactions(
        episodic,
        limit=request.limit or DEFAULT_CLOSED_INTERACTION_LIMIT,
        scope=request.scope or None,
        interaction_id=request.interaction_id or None,
        # 0 / unset → 1 (everything); a caller passes 2 to drop the
        # degenerate single-turn rows from an unscoped list.
        min_turns=request.min_turns or 1,
    )
    return task_pb2.ClosedInteractionsResponse(
        interactions=[
            task_pb2.ClosedInteraction(
                interaction_id=ep.interaction_id or "",
                scope=ep.scope or "",
                started_at=ep.started_at or 0.0,
                closed_at=ep.closed_at or 0.0,
                turn_count=ep.turn_count or 0,
                # The RFC 0020 close trigger rides in the persisted
                # context blob (no dedicated column); legacy rows that
                # predate close-reason capture surface an empty string.
                close_reason=(
                    str(ep.context.get("close_reason", ""))
                    if isinstance(ep.context, dict)
                    else ""
                ),
                summary=ep.summary,
                participants=_participants_from_context(ep.context),
                # ISSUE-0102: surface the RFC 0030 governance interaction id.
                # PR 2 promoted it to the queryable ``governance_interaction_id``
                # column (migration v15) — read it from there. Falls back to the
                # PR-1 context-blob key for mixed-version safety: a row written by
                # an older agent process after the v15 schema landed (column NULL,
                # id only in context) still surfaces. Empty when neither carries
                # one (DM / thread / non-channel / legacy row).
                governance_interaction_id=_governance_id(ep),
            )
            for ep in episodes
        ],
    )
