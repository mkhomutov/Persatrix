"""End-vote close notification → RFC 0020 close path (CP3, agent half).

The orchestrator's `end_votes` close suppresses the closing vote's
fanout, so the close must be *delivered*: the close-notification
dispatch (the end-vote-close-propagation amendment, CP1/CP2) re-sends
the closing message to every dispatch-served member with the
``interaction_close_notification`` marker. Receiver-side the marked
event is control, never stimulus — the response gate refuses it
pre-LLM (reason ``close_notification``) and the action loop's suppress
path calls :func:`close_interaction_on_notification` so the channel
scope's open interaction closes NOW with the established ``end_votes``
mapping — :data:`~agents.memory.boundary_detectors.REASON_STRUCTURAL`,
the "ended" render (:mod:`.interaction_boundary`: the quorum close IS
the explicit end the structural label claims) — instead of burying the
converged discussion as "went idle" an idle window later.

Extracted as a free function taking the composed persona agent — the
:mod:`.cost_close` / :mod:`.vote_close` sibling, same extraction idiom,
so ``action_loop.py`` stays a one-liner at the call site and under the
500-line review cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..memory.boundary_detectors import REASON_STRUCTURAL

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker
    from ..persona_types import AgentEvent, EventType

    class _CloseNotificationAgent(Protocol):
        """The composed-agent surface
        :func:`close_interaction_on_notification` needs."""

        _interaction_tracker: InteractionTracker
        _MULTI_TURN_EVENT_TYPES: frozenset[EventType]

        def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None: ...
        async def _persist_closed_interaction(
            self, interaction: Interaction,
        ) -> None: ...


__all__ = ["close_interaction_on_notification"]


async def close_interaction_on_notification(
    agent: _CloseNotificationAgent, event: AgentEvent,
) -> None:
    """Close + persist the open interaction in the notified scope.

    Called from the action loop's gate-suppress path only for the
    dedicated ``close_notification`` refusal. Defence-in-depth re-checks
    here regardless (CP3's strict-bool rule): a truthy non-bool marker on
    the cleartext port must not fabricate a close — burying an active
    discussion is exactly the failure mode the strictness blocks — so an
    impostor is a no-op even if a future caller wires this off a looser
    signal. Scope resolution rides the same multi-turn routing as
    :func:`.cost_close.close_interaction_on_cost`'s channel branch: a
    notification is channel traffic by construction (the orchestrator
    re-dispatches a channel publish), so a non-multi-turn event type has
    no scope to close and returns quietly. No open interaction in the
    scope (the agent's window already idled it out before the
    notification landed) is the ``InteractionTracker.close``
    unknown-scope no-op — the close stands recorded orchestrator-side;
    nothing here invents a record to mirror it.
    """
    marked = event.payload.get("interaction_close_notification") is True
    if not marked:
        return
    if event.event_type not in agent._MULTI_TURN_EVENT_TYPES:
        return
    scope = agent._scope_for_multi_turn_event(event)
    if scope is None:
        return
    closed = agent._interaction_tracker.close(scope, reason=REASON_STRUCTURAL)
    if closed is not None:
        await agent._persist_closed_interaction(closed)
