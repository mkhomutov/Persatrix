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
                _add(payload.get("sender"))
    else:
        _add(ctx.get("sender"))
    return out


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
            )
            for ep in episodes
        ],
    )
