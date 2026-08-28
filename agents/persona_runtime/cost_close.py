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

from ..channel_wire_metadata import wire_interaction_id
from ..memory.boundary_detectors import REASON_COST
from ..memory.interactions import SCOPE_TICK, is_thread_scope
from ..persona_types import EventType
from .close_path import persist_fanned_closes

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
    # ISSUE-0123 part 3: the interaction budget is the shared
    # conversation's, so its exhaustion is a ROOM event — fan the close
    # over every ``(principal, speaker)`` record open in the scope, or
    # the siblings leak open until idle buries the terminated
    # conversation without a summary trigger of their own.  Persistence
    # is guarded per record (``persist_fanned_closes``, v0.3.15 PR 3
    # review fix): the fan pops ALL its records before the first persist
    # runs, so one failure must not discard the siblings.
    #
    # Per-record wire-id admission (PR #846 review, the close_notification
    # conjunct): the exhausted budget is the EVENT's wire interaction's —
    # this close fires from the LLM-error path, before the stale fan has
    # reconciled the scope against this event — so a record positively
    # stamped with a DIFFERENT id (a successor conversation with a fresh
    # budget) is skipped, not buried under ``REASON_COST``.  A blank on
    # either side keeps the scope-keyed behaviour (thread scopes are
    # wire-untracked; ticks and legacy traffic carry no id).  One room
    # event, one instant: a single clock read stamps every close.
    anchor = "" if is_thread_scope(scope) else wire_interaction_id(event)
    now = agent._interaction_tracker.now()
    closed_records: list[Interaction] = []
    for record in agent._interaction_tracker.records_for_scope(scope):
        if record.replayed:
            # PR #846 review: a replay-opened record belongs to the
            # catch-up pass and its ``REASON_CATCHUP_COMPLETE`` sweep —
            # never retire it under a live cause.  ``close_notification``
            # carried this guard; this fan and the vote fan did not, so a
            # denial landing before ``close_replayed_scopes`` buried the
            # replayed span as ``cost`` and the sweep then found nothing.
            continue
        if (
            anchor
            and record.wire_interaction_id
            and record.wire_interaction_id != anchor
        ):
            continue
        closed = agent._interaction_tracker.close_record(
            record, reason=REASON_COST, now=now,
        )
        if closed is not None:
            closed_records.append(closed)
    # OQ #6 metering interaction (deep-review follow-up): this LOCAL Layer-1
    # cost close does NOT set ``meter_close_summary``, so the summaries it
    # triggers run UNLEASED — unlike the orchestrator's bounded cost close,
    # whose close notification marks the records (close_notification.py). The
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
    await persist_fanned_closes(
        closed_records, agent._persist_closed_interaction,
    )
