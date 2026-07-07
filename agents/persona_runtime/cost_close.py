"""RFC 0030 Layer 1 cost-ceiling → RFC 0020 close path (v0.3.8 PR 1, SS2).

The per-interaction cost ceiling (``interaction_budget_tokens``)
exhausting mid-conversation is an explicit close trigger: the wallet
denies the lease with ``interaction_budget_exhausted`` and the
conversation must *terminate and summarise*, not merely stop fanout — a
bounded brainstorm has to hand back a readable result.

Extracted as a free function (taking the composed persona agent) so the
action loop's wallet-denial handler stays a one-liner and
``action_loop.py`` / ``episode_routing.py`` stay under the 500-line
review cap — the same extraction idiom as :mod:`agents.persona_runtime.close_path`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..memory.boundary_detectors import REASON_COST
from ..memory.interactions import SCOPE_TICK
from ..persona_types import EventType

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker
    from ..persona_types import AgentEvent

    class _CostCloseAgent(Protocol):
        """The composed-agent surface :func:`close_interaction_on_cost` needs."""

        _interaction_tracker: InteractionTracker
        _MULTI_TURN_EVENT_TYPES: frozenset[EventType]

        def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None: ...
        async def _persist_closed_interaction(
            self, interaction: Interaction,
        ) -> None: ...


__all__ = ["close_interaction_on_cost"]


def _scope_for_cost_close(agent: _CostCloseAgent, event: AgentEvent) -> str | None:
    """RFC 0020 §G scope of the interaction the cost ceiling is closing.

    Mirrors ``route_event_to_episode``'s scope routing so the cost-close
    targets the *same* open interaction the normal per-event path would
    have appended to: channel/mention traffic resolves via
    ``_scope_for_multi_turn_event``; everything else takes the
    single-turn scope (``SCOPE_TICK`` for ticks, the event-type value
    otherwise).
    """
    if event.event_type in agent._MULTI_TURN_EVENT_TYPES:
        return agent._scope_for_multi_turn_event(event)
    if event.event_type is EventType.TICK:
        return SCOPE_TICK
    return str(event.event_type.value)


async def close_interaction_on_cost(
    agent: _CostCloseAgent, event: AgentEvent,
) -> None:
    """Close + summarise the open interaction in the event's scope.

    Called from the action loop only for the
    ``interaction_budget_exhausted`` wallet denial — a per-agent
    (``budget_exceeded``) denial is the agent's own RFC 0023 wallet and
    does *not* terminate the shared interaction. No-op when the scope has
    no open interaction (e.g. the ceiling tripped on the first turn
    before any interaction opened), mirroring ``InteractionTracker.close``'s
    unknown-scope contract. Runs under the caller's ``_lock`` (Phase 1
    INSERT is synchronous; Phase 2 summary is a background task), same as
    ``route_event_to_episode``.
    """
    scope = _scope_for_cost_close(agent, event)
    if scope is None:
        return
    closed = agent._interaction_tracker.close(scope, reason=REASON_COST)
    if closed is not None:
        # OQ #6 metering interaction (deep-review follow-up): this LOCAL Layer-1
        # cost close does NOT set ``meter_close_summary``, so the summary it
        # triggers runs UNLEASED — unlike the orchestrator's bounded cost close,
        # whose close notification marks the record (close_notification.py). The
        # two race: a member whose own compose lease is denied
        # (``interaction_budget_exhausted``) self-closes here before the
        # orchestrator notification lands, and its summary escapes the cap, so the
        # RFC 0052 ``1 + N`` accounting can undercount by one on the very
        # close-by-budget path OQ #6 targets. This is DELIBERATELY not metered
        # here yet: the wallet reserve is still dark (AcquireLease enforces only
        # the hard cap — synthesis_reserve.go), so metering this summary against
        # the already-exhausted cap would DENY it and degrade a real artifact to
        # the ``[interaction summary unavailable]`` placeholder — strictly worse
        # than an unleased-but-real summary while spend-counting gates nothing.
        # The under-count is only load-bearing once reserve enforcement lands; the
        # RFC 0052 PR-plan "Deep-review follow-ups" tracks metering this path
        # together with that enforcement.
        await agent._persist_closed_interaction(closed)
