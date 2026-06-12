"""The action loop's gate-suppress path (RFC 0011 PR 4b / PR 5 + CP3).

What happens to a CHANNEL_MESSAGE the response gate refused: the gated
counter fires, the event still ingests memory (suppression decides
*whether to respond*, not *whether to remember*), and — since the
end-vote-close-propagation amendment — the dedicated
``close_notification`` refusal runs the agent-local tracker close.

Extracted as a free function taking the composed persona agent — the
:mod:`.cost_close` / :mod:`.close_notification` extraction idiom — so
the action loop's refusal handler stays a one-liner and
``action_loop.py`` stays under the 500-line review cap. Behaviour is
verbatim from the loop (PR 3 of the close-propagation workstream moved
it); the loop-level pins live in
``test_close_notification_action_loop.py`` and the replay/gate suites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..observability.metrics import gate_attrs, try_get_instruments
from ..persona_types import ActionType, AgentAction
from ..response_gate import POLICY_DEFENSE_IN_DEPTH, GateDecision
from .close_notification import close_interaction_on_notification

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker
    from ..persona_types import AgentEvent, EventType

    class _GateSuppressAgent(Protocol):
        """The composed-agent surface :func:`suppressed_event_actions`
        needs — its own ingest seam plus everything
        :func:`close_interaction_on_notification` requires."""

        _interaction_tracker: InteractionTracker
        _MULTI_TURN_EVENT_TYPES: frozenset[EventType]

        def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None: ...
        async def _persist_closed_interaction(
            self, interaction: Interaction,
        ) -> None: ...
        async def _store_event_episode(
            self, event: AgentEvent, actions: list[AgentAction],
        ) -> None: ...


__all__ = ["suppressed_event_actions"]


async def suppressed_event_actions(
    agent: _GateSuppressAgent, event: AgentEvent, decision: GateDecision,
) -> list[AgentAction]:
    """Handle a gate-refused CHANNEL_MESSAGE; returns the loop's actions.

    Called from the action loop only when ``decision.respond`` is false.
    """
    inst = try_get_instruments()
    if inst is not None:
        # RFC 0011 §D label set: ``{channel_id, policy}``. The
        # legacy SendChatMessage path (deferred for cleanup in
        # ISSUE-0035) builds CHANNEL_MESSAGE events with no
        # channel_id; the gate exits early before this branch
        # runs for those, so an empty channel_id should not
        # reach this site in practice.
        inst.channel_messages_gated.add(1, attributes=gate_attrs(
            channel_id=event.channel_id or "",
            policy=decision.policy or "unknown",
        ))
    # RFC 0011 PR 5: suppressed events still ingest memory so a
    # ``when_mentioned`` listener does not lose context between
    # mentions. The gate decides *whether to respond*, not
    # *whether to remember*.  Exception (PR-263 review M-1):
    # ``policy=defense_in_depth`` fires only when ``sender_id ==
    # agent_id`` — the agent's own outbound message echoed back
    # through the cleartext gRPC port. Ingesting that row would
    # inflate ``turn_count`` and (for DMs) write a turn whose
    # ``payload.sender == agent_id`` for a peer-keyed scope; we
    # do not echo our own outbound message into episodic memory.
    if decision.policy != POLICY_DEFENSE_IN_DEPTH:
        await agent._store_event_episode(event, [])
    # End-vote-close-propagation amendment (CP3): the dedicated
    # refusal doubles as the close signal — the ingest above kept
    # the closing vote as the interaction's final turn; close the
    # scope NOW with the structural ("ended") cause instead of
    # letting it idle out a window later. After the ingest on
    # purpose: closing first would strand the closing message in
    # the successor interaction.
    if decision.reason == "close_notification":
        await close_interaction_on_notification(agent, event)
    return [AgentAction(action_type=ActionType.DO_NOTHING, payload={})]
