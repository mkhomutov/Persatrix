"""v0.3.8 SS2 — the cost-close dispatch wrapper must not mask the denial.

``handle_llm_call_exception_with_cost_close`` runs the RFC 0030 Layer 1
cost-close (terminate + summarise the interaction) *before* deciding
whether to re-raise the wallet denial (chat/channel) or short-circuit
(TICK). The close path persists best-effort and guards its own
``store_episode``, but the scope-resolution / tracker call ahead of it is
not otherwise guarded — if it ever raised, that error would replace the
``BudgetExceededError`` the caller expects, turning a clean wallet denial
into an opaque internal failure (PR-583 review). This pins that the
wrapper swallows a close-path error and still dispatches the original
exception.
"""

from __future__ import annotations

from _otel_test_helpers import build_meter, counter_total

from agents.memory.boundary_detectors import REASON_COST
from agents.memory.interactions import SCOPE_TICK, Interaction, InteractionTracker
from agents.persona_runtime import llm_call_errors
from agents.persona_runtime.cost_close import close_interaction_on_cost
from agents.persona_types import AgentEvent, EventType
from agents.wallet_client import BudgetExceededError


class _FakeAgent:
    agent_id = "agent-x"


def _interaction_budget_denial() -> BudgetExceededError:
    return BudgetExceededError(
        "interaction budget exceeded", reason="interaction_budget_exhausted",
    )


async def test_close_path_error_does_not_mask_wallet_denial(monkeypatch):
    """A raising cost-close must not swallow/replace the wallet denial."""
    async def _boom(agent, event):
        raise RuntimeError("scope resolution blew up")

    monkeypatch.setattr(llm_call_errors, "close_interaction_on_cost", _boom)

    # A CHANNEL_MESSAGE re-raises to the caller → the wrapper must return
    # None (the re-raise signal), NOT propagate the RuntimeError.
    result = await llm_call_errors.handle_llm_call_exception_with_cost_close(
        _FakeAgent(),
        _interaction_budget_denial(),
        AgentEvent(event_type=EventType.CHANNEL_MESSAGE, sender_id="peer"),
    )
    assert result is None  # re-raise the BudgetExceededError, not the RuntimeError


async def test_cost_close_invoked_only_for_interaction_budget(monkeypatch):
    """The close fires for an interaction-budget denial, not a per-agent one."""
    calls: list[str] = []

    async def _record(agent, event):
        calls.append(event.event_type.value)

    monkeypatch.setattr(llm_call_errors, "close_interaction_on_cost", _record)

    ev = AgentEvent(event_type=EventType.CHANNEL_MESSAGE, sender_id="peer")
    # Interaction-budget denial → close runs.
    await llm_call_errors.handle_llm_call_exception_with_cost_close(
        _FakeAgent(), _interaction_budget_denial(), ev,
    )
    # Per-agent denial → close must NOT run (it's the agent's own wallet).
    await llm_call_errors.handle_llm_call_exception_with_cost_close(
        _FakeAgent(),
        BudgetExceededError("per-agent", reason="budget_exceeded"),
        ev,
    )
    assert calls == ["channel_message"]


class _CostCloseAgent:
    """Minimal surface ``close_interaction_on_cost`` needs (the Protocol).

    A TICK event takes the single-turn ``SCOPE_TICK`` branch of
    ``_scope_for_cost_close``, so ``_MULTI_TURN_EVENT_TYPES`` is empty and
    ``_scope_for_multi_turn_event`` is never reached here.
    """

    _MULTI_TURN_EVENT_TYPES: frozenset[EventType] = frozenset()

    def __init__(self, tracker: InteractionTracker) -> None:
        self._interaction_tracker = tracker
        self.persisted: list[Interaction] = []

    async def _persist_closed_interaction(self, interaction: Interaction) -> None:
        self.persisted.append(interaction)


async def test_cost_close_dispatch_emits_by_cost_counter():
    """The real dispatch path closes with REASON_COST → ``by_cost`` counter.

    ``test_interaction_tracker`` pins ``close(reason=REASON_COST)`` → the
    ``by_cost`` subtotal, and the cost-close *dispatch* is otherwise mocked
    out in the tests above. This drives the production
    ``close_interaction_on_cost`` against a real tracker with metrics live,
    closing the gap between "dispatch is invoked" and "dispatch actually
    closes the scope's interaction with REASON_COST and persists it".
    """
    reader, metrics_mod = build_meter()
    try:
        tracker = InteractionTracker()
        tracker.add_turn(SCOPE_TICK)  # open an interaction in the tick scope
        agent = _CostCloseAgent(tracker)

        await close_interaction_on_cost(
            agent, AgentEvent(event_type=EventType.TICK),
        )

        # The scope's interaction is closed, persisted once, and tagged cost.
        assert tracker.get(SCOPE_TICK) is None
        assert len(agent.persisted) == 1
        assert agent.persisted[0].close_reason == REASON_COST
        assert counter_total(reader, "agent.interactions.closed.by_cost") == 1
    finally:
        await metrics_mod.shutdown()


async def test_cost_close_dispatch_no_open_interaction_is_noop():
    """The ceiling tripping before any interaction opened → no persist.

    Mirrors ``InteractionTracker.close``'s unknown-scope contract: a cost
    denial on the very first turn (no open interaction in the scope yet)
    must not fabricate or persist an empty interaction.
    """
    tracker = InteractionTracker()  # nothing open
    agent = _CostCloseAgent(tracker)

    await close_interaction_on_cost(agent, AgentEvent(event_type=EventType.TICK))

    assert agent.persisted == []


async def test_cost_fan_leaves_replayed_records_to_the_catchup_sweep():
    """PR #846 re-review: the cost fan gained the ``replayed`` guard the
    close-notification fan already carried.

    This close fires from the LLM-error path, before the stale fan has
    reconciled the scope, so a denial landing mid catch-up closed the
    replay-opened record under ``REASON_COST`` — mislabelling the
    per-reason counter AND popping the record before
    ``close_replayed_scopes`` could close it as
    ``REASON_CATCHUP_COMPLETE``.  ``persist_closed_interaction`` returns
    early on the flag, so the span derived nothing on the way out either.
    """
    tracker = InteractionTracker()
    replayed = tracker.add_turn(SCOPE_TICK, replayed=True)
    agent = _CostCloseAgent(tracker)

    await close_interaction_on_cost(agent, AgentEvent(event_type=EventType.TICK))

    assert replayed.is_open, "the replay-opened record is the sweep's to close"
    assert agent.persisted == [], "and nothing is derived from a flagged span"
