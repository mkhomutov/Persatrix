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

from agents.persona_runtime import llm_call_errors
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
