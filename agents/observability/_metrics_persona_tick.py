"""Persona-tick metric registrations (RFC 0023 PR 5).

Split out of :mod:`agents.observability.metrics` so the parent module
stays under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``). Mirrors the
:mod:`agents.observability._metrics_facts` /
:mod:`agents.observability._metrics_interactions` splits — the
registered counter is assigned to the parent :class:`_Instruments`
instance, so call sites reach it via ``inst.persona_tick_idle``
without any rename.

``persona_tick_idle`` is incremented on every autonomous TICK that
returns ``DO_NOTHING`` via a known short-circuit. The
``idle_reason`` attribute is the dashboard discriminator:
``empty_context_tick`` (RFC 0017 §F), ``budget_denied`` (RFC 0023 § F
``BudgetExceededError`` arm), and ``resource_exhausted`` (ISSUE-0066
``AioRpcError(RESOURCE_EXHAUSTED)`` arm — back-pressure from the
per-agent active-lease cap or the gRPC rate-limiter). Low cardinality
by construction — the three values are enumerated at the call sites in
``persona_runtime/action_loop.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register ``agent.persona.tick.idle`` on ``inst``."""
    inst.persona_tick_idle = meter.create_counter(
        name="agent.persona.tick.idle",
        unit="{tick}",
        description=(
            "Autonomous TICKs that short-circuited to DO_NOTHING. "
            "Attributes: agent.id, idle_reason "
            "(empty_context_tick | budget_denied | resource_exhausted)."
        ),
    )


def tick_idle_attrs(*, agent_id: str, idle_reason: str) -> dict[str, str]:
    """Attribute set for ``agent.persona.tick.idle`` (RFC 0023 PR 5)."""
    return {"agent.id": agent_id, "idle_reason": idle_reason}
