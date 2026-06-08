"""Wallet- and provider-error handling for the persona action loop.

Extracted from :mod:`agents.persona_runtime.action_loop` so that module
stays under the 500-line review-friendly cap enforced by
``scripts/checks/file_size.py``. Same precedent as
:mod:`agents.chat_reply` (extracted from
:mod:`agents.server_servicers`, RFC 0011 PR 4a-i; extended for
ISSUE-0065 / ISSUE-0066's dispatcher arm).

The action loop's `LLMClient.create_message` call can raise four
classes of exception, with different downstream contracts:

* :class:`BudgetExceededError` — wallet denial. Chat / channel /
  workflow callers render it (``reply_status="error"`` / task
  ``FAILED``); an autonomous TICK has no caller so the action loop
  short-circuits to ``DO_NOTHING`` with
  ``idle_reason="budget_denied"`` on ``persona_tick_idle``.
* :class:`grpc.aio.AioRpcError` with
  ``code == grpc.StatusCode.RESOURCE_EXHAUSTED`` — wallet
  back-pressure (per-agent active-lease cap from
  :file:`internal/wallet/wallet.go` or the orchestrator's gRPC
  rate-limit interceptor from :file:`internal/security/middleware.go`).
  Operator-visible same shape as a budget denial; same
  TICK-vs-other-event split. The chat / channel path's published
  error reply is built by
  :func:`agents.chat_reply.dispatch_channel_event_with_chat_error_recovery`
  with ``error_reason="resource_exhausted"`` (ISSUE-0066).
* :class:`grpc.aio.AioRpcError` with any other code (``INTERNAL`` /
  ``UNAVAILABLE`` / ``INVALID_ARGUMENT`` / etc.) — real provider-side
  or wallet-side problems. Degrade to a generic
  ``COMPLETE_TASK("LLM provider error")`` so they are not masked as
  back-pressure (dashboards split on ``error_reason``).
* Any other :class:`Exception` — bare provider outage (network, 5xx
  through the HTTP client, etc.). Same generic ``COMPLETE_TASK``
  degradation as v0.2.3.

:func:`handle_llm_call_exception` consolidates the dispatch. It
returns the list of actions the action loop should return on
graceful-degradation paths, or :data:`None` to signal the action
loop should re-raise (chat / channel / workflow caller renders
the error).
"""

from __future__ import annotations

import logging
from typing import Any

import grpc
import grpc.aio

from ..observability._metrics_persona_tick import tick_idle_attrs
from ..observability.metrics import try_get_instruments
from ..persona_types import ActionType, AgentAction, AgentEvent, EventType
from ..wallet_client import BudgetExceededError
from .cost_close import close_interaction_on_cost

__all__ = [
    "handle_llm_call_exception",
    "handle_llm_call_exception_with_cost_close",
]

# Deliberately pinned to the caller's logger name (``action_loop``)
# rather than ``__name__`` — operator dashboards and the
# ``caplog.at_level(logger="agents.persona_runtime.action_loop")``
# filter in ``test_action_loop_resource_exhausted.py`` /
# ``test_action_loop_tick_lease.py`` key on the action-loop logger.
# The extraction to this helper is a file-size convenience (RFC 0011
# 500-line cap); the log records must continue to surface as the
# action loop's own.
logger = logging.getLogger("agents.persona_runtime.action_loop")


def _generic_provider_error(agent_id: str, exc: BaseException) -> list[AgentAction]:
    """Render the generic provider-error degradation (v0.2.3 surface).

    Logged at ERROR so a real provider problem stays visible; the
    ``COMPLETE_TASK("LLM provider error")`` action is rendered by the
    dispatcher / chat handler as a reply text without the structured
    error envelope. Used for non-``RESOURCE_EXHAUSTED`` gRPC codes and
    for any non-gRPC :class:`Exception`.
    """
    logger.error("LLM provider error in agent %s: %s", agent_id, exc)
    return [AgentAction(
        action_type=ActionType.COMPLETE_TASK,
        payload={"result": "LLM provider error"},
    )]


def _tick_idle(agent_id: str, *, idle_reason: str) -> list[AgentAction]:
    """Increment ``persona_tick_idle`` and return ``[DO_NOTHING]``.

    Shared between the :class:`BudgetExceededError` arm
    (``idle_reason="budget_denied"``) and the
    :class:`grpc.aio.AioRpcError(RESOURCE_EXHAUSTED)` arm
    (``idle_reason="resource_exhausted"``). The
    :func:`agents.observability._metrics_persona_tick.tick_idle_attrs`
    helper enumerates the valid values.
    """
    inst = try_get_instruments()
    if inst is not None:
        inst.persona_tick_idle.add(
            1,
            attributes=tick_idle_attrs(
                agent_id=agent_id, idle_reason=idle_reason,
            ),
        )
    return [AgentAction(action_type=ActionType.DO_NOTHING, payload={})]


def handle_llm_call_exception(
    exc: BaseException,
    *,
    event: AgentEvent,
    agent_id: str,
) -> list[AgentAction] | None:
    """Dispatch an exception raised by :meth:`LLMClient.create_message`.

    Returns a list of actions on graceful-degradation paths (TICK
    short-circuits, generic provider-error fallback), or :data:`None`
    when the action loop should re-raise so the chat / channel /
    workflow caller can render the error.

    See the module docstring for the four exception classes handled.
    """
    if isinstance(exc, BudgetExceededError):
        # RFC 0023 § F — most calling surfaces render the denial to
        # the caller (chat → ``reply_status="error"``; workflow task
        # → ``TaskStatus.FAILED``), so the action loop re-raises.
        # An autonomous TICK has no caller to notify: re-raising
        # would surface as ``Tick error`` in
        # :meth:`TickScheduler._run` and lose the tick instead of
        # reflecting it as idle, blinding dashboards to the budget
        # pressure the wallet is actually suppressing. PR 5
        # short-circuits TICK to ``DO_NOTHING`` — same shape as the
        # RFC 0017 §F empty-context branch — with a WARN log and the
        # ``idle_reason=budget_denied`` counter so the throttling is
        # visible.
        if event.event_type == EventType.TICK:
            logger.warning(
                "Agent %s: autonomous tick denied by wallet (%s) — "
                "treating as idle",
                agent_id, exc,
            )
            return _tick_idle(agent_id, idle_reason="budget_denied")
        return None  # re-raise to caller

    if isinstance(exc, grpc.aio.AioRpcError):
        # ISSUE-0066 — :meth:`WalletClient._acquire` retries
        # ``AcquireLease`` on ``codes.ResourceExhausted`` and, after
        # exhausting the retry budget, re-raises the raw
        # ``AioRpcError``. That surface is wallet back-pressure —
        # operator-visible same shape as a budget denial. Mirror the
        # :class:`BudgetExceededError` arm: TICK short-circuits with
        # ``idle_reason="resource_exhausted"`` (matching the
        # dispatcher's ``error_reason``); chat / channel re-raise so
        # the dispatcher's
        # :func:`agents.chat_reply.dispatch_channel_event_with_chat_error_recovery`
        # wrapper publishes the structured error reply (the unit-
        # level contract is pinned by
        # ``agents/tests/test_chat_path_resource_exhausted.py``).
        # Other gRPC codes indicate real provider-side problems and
        # must degrade to the generic surface — not masked as
        # back-pressure.
        if exc.code() != grpc.StatusCode.RESOURCE_EXHAUSTED:
            return _generic_provider_error(agent_id, exc)
        if event.event_type == EventType.TICK:
            logger.warning(
                "Agent %s: autonomous tick denied by wallet "
                "back-pressure (%s) — treating as idle",
                agent_id, exc.details() or exc.code(),
            )
            return _tick_idle(agent_id, idle_reason="resource_exhausted")
        return None  # re-raise to caller

    # Bare provider outage — same v0.2.3 degradation shape.
    return _generic_provider_error(agent_id, exc)


async def handle_llm_call_exception_with_cost_close(
    agent: Any,
    exc: BaseException,
    event: AgentEvent,
) -> list[AgentAction] | None:
    """Cost-close (RFC 0030 Layer 1) then dispatch the wallet/provider error.

    The RFC 0030 Layer 1 cost ceiling exhausting
    (``BudgetExceededError(reason="interaction_budget_exhausted")``) is an
    explicit close trigger: terminate + summarise the interaction
    (v0.3.8 PR 1, SS2) *before* the usual
    :func:`handle_llm_call_exception` dispatch decides whether to
    re-raise (chat/channel) or short-circuit (TICK). A per-agent
    ``budget_exceeded`` denial is the agent's own RFC 0023 wallet and is
    left to the normal dispatch untouched. Returns whatever
    :func:`handle_llm_call_exception` returns (``None`` → caller re-raises).
    """
    if (
        isinstance(exc, BudgetExceededError)
        and exc.reason == "interaction_budget_exhausted"
    ):
        await close_interaction_on_cost(agent, event)
    return handle_llm_call_exception(
        exc, event=event, agent_id=agent.agent_id,  # type: ignore[attr-defined]
    )
