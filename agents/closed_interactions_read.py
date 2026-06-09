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
            )
            for ep in episodes
        ],
    )
