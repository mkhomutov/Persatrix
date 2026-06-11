"""Publish-confirmed local close for the END_INTERACTION_VOTE voter.

PR 607 review finding 5, implemented with producer plan OQ 5: the voter's
local interaction used to close at *decide* time — inside
``_EpisodeRoutingMixin._handle_multi_turn_event``, which runs at action-loop
step 6, BEFORE the executor publishes the vote.  The persona's judgement was
real either way, but the publish can fail (timeout, ``ChannelsDisabledError``,
no REST publisher on the legacy in-process path), and a vote that never
reached the orchestrator counts toward no quorum — the decide-time close left
an early "ended" record for a conversation that, on the wire, never ended.

The seam is now two-phase:

* **Decide time** (``episode_routing``): when the turn's actions vote to end
  the event's conversation (:func:`.interaction_boundary.ends_interaction_by_vote`,
  gates unchanged), the close is PARKED — a :class:`PendingVoteClose` keyed by
  the vote's channel id — instead of executed.
* **Publish outcome** (``agents/action_executor.py``): after
  ``publish_end_interaction_vote`` returns, the executor calls back into the
  voter (``_LLMPersonaAgent.resolve_end_vote_publish`` →
  :func:`discharge_end_vote_publish`).  ``status == "published"`` closes the
  parked scope with ``REASON_STRUCTURAL`` and persists, exactly what the
  decide-time close did; any failure status drops the park — the record stays
  open and closes later through the ordinary boundaries (the wire id rotation
  once a real quorum forms, or the idle gap).

Staleness guards: the park stores the open interaction's id, and the
discharge closes only when the scope still holds that same open interaction —
a max-turns inline close, an idle flush, or a wire rotation between decide
and discharge wins, and the discharge no-ops.  A park that is never
discharged (the executor never ran — e.g. a ``dispatch(execute_actions=False)``
caller) is overwritten by the channel's next vote and is inert meanwhile, by
the same guard.  At most one park per channel, bounded by channels the agent
votes in.

Free function over the agent (the ``run_salience_gate`` /
``handle_llm_call_exception_with_cost_close`` convention) so
``episode_routing`` stays under the 500-line review cap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from ..memory.boundary_detectors import REASON_STRUCTURAL

if TYPE_CHECKING:
    from .episode_routing import _EpisodeRoutingMixin

logger = logging.getLogger(__name__)

__all__ = ["PendingVoteClose", "discharge_end_vote_publish"]


class PendingVoteClose(NamedTuple):
    """One parked vote close: the scope the voter's record lives under and
    the open interaction's id at decide time (the staleness guard)."""

    scope: str
    interaction_id: str


async def discharge_end_vote_publish(
    agent: _EpisodeRoutingMixin, channel_id: str, *, published: bool,
) -> None:
    """Discharge the parked vote close for ``channel_id`` (module doc).

    Runs under the agent's lock: the park/discharge pair brackets the
    executor's publish, which runs *outside* ``on_event``'s lock, so a
    concurrent event for the same scope could otherwise race the close.
    Idempotent — the park is popped first, so a duplicate callback (or a
    callback for a channel that never parked: a DM/unbound vote the
    decide-time gates skipped) is a no-op.
    """
    async with agent._lock:
        pending = agent._pending_vote_closes.pop(channel_id, None)
        if pending is None:
            return
        if not published:
            # The honest outcome of finding 5: no publish → no close. INFO,
            # not WARNING — the publish failure itself already logged at
            # warning in end_vote_action; this line records the memory-side
            # consequence (the record stays open for the ordinary closes).
            logger.info(
                "Agent %s: end-vote publish to %s did not complete; the "
                "local interaction record stays open (scope=%s)",
                agent.agent_id, channel_id, pending.scope,
            )
            return
        open_record = agent._interaction_tracker.get(pending.scope)
        if (
            open_record is None
            or not open_record.is_open
            or open_record.interaction_id != pending.interaction_id
        ):
            # The scope moved on between decide and publish (max-turns
            # inline close, idle flush, wire rotation) — that close already
            # told the truth; do not close its successor.
            return
        closed = agent._interaction_tracker.close(
            pending.scope, reason=REASON_STRUCTURAL,
        )
        if closed is not None:
            await agent._persist_closed_interaction(closed)
