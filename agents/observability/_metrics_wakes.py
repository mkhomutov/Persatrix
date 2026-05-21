"""Wake-counter metric registrations (RFC 0024 PR 3b).

Split out of :mod:`agents.observability.metrics` so the parent module
stays under the project's 500-line review-friendly cap (mirrors the
:mod:`agents.observability._metrics_facts` /
:mod:`._metrics_interactions` / :mod:`._metrics_persona_tick` splits).

PR 3b is the formal home for all four ``agent.wake.*`` counters named in
[RFC 0024 PR plan PR 3b](../../docs/rfcs/0024-pr-plan.md):

* ``agent.wake.salience`` — produced by the :class:`MemoryWriteBus`
  subscriber the ``EventLoop`` installs at :meth:`EventLoop.start`. Every
  ``MemoryWriteEvent`` for this agent increments exactly one data point
  on this counter; the ``suppressed_reason`` attribute discriminates the
  four branches of the enqueue decision tree
  (``below_threshold`` | ``loopback`` | ``rate_limit`` | ``none``).
  Without this attribute "no salience wakes" is indistinguishable from
  "wakes are working and the agent is quiet" — the dashboard cannot
  attribute the silence.
* ``agent.wake.inbound`` / ``agent.wake.scheduled`` — recorded by the
  ``EventLoop`` supervisor when it dispatches the matching wake variant.
  PR 1 declared the ``wake.kind`` taxonomy (`_wake_kind`) in
  :mod:`agents.event_loop` but did not register the counters; PR 3b
  lands them as the formal home so PR 4's bored-persona cost-regression
  gate can assert *all four* read zero over a 60-second window.
* ``agent.wake.dropped`` — incremented when ``EventLoop.enqueue`` returns
  ``False`` because the queue is full. PR 1's discard policy already
  bumps :attr:`EventLoop.dropped_count`; PR 3b wires the counter
  alongside that internal counter so dashboards see the same number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register the four ``agent.wake.*`` counters on ``inst``."""
    inst.wake_inbound = meter.create_counter(
        name="agent.wake.inbound",
        unit="{wake}",
        description=(
            "InboundEventWake dispatched by the EventLoop supervisor. "
            "Attributes: agent.id, wake.kind=inbound."
        ),
    )
    inst.wake_scheduled = meter.create_counter(
        name="agent.wake.scheduled",
        unit="{wake}",
        description=(
            "ScheduledWake dispatched by the EventLoop supervisor. "
            "Attributes: agent.id, wake.kind=scheduled, timer_id."
        ),
    )
    inst.wake_salience = meter.create_counter(
        name="agent.wake.salience",
        unit="{wake}",
        description=(
            "MemoryWriteEvent observed by this agent's EventLoop subscriber. "
            "Recorded exactly once per same-agent write — the "
            "suppressed_reason attribute discriminates the four enqueue "
            "branches (below_threshold | loopback | rate_limit | none). "
            "Attributes: agent.id, wake.kind=salience, tier, "
            "suppressed_reason."
        ),
    )
    inst.wake_dropped = meter.create_counter(
        name="agent.wake.dropped",
        unit="{wake}",
        description=(
            "Wake enqueue rejected because the EventLoop queue was full. "
            "Discard-not-block policy per RFC 0024 Decided §1. "
            "Attributes: agent.id, wake.kind=dropped."
        ),
    )


def wake_attrs(
    *,
    agent_id: str,
    wake_kind: str,
    timer_id: str | None = None,
    tier: str | None = None,
    suppressed_reason: str | None = None,
) -> dict[str, str]:
    """Attribute set for the ``agent.wake.*`` counter family.

    ``wake_kind`` is required (``inbound`` / ``scheduled`` / ``salience`` /
    ``dropped``); the per-variant fields (``timer_id`` for scheduled,
    ``tier`` + ``suppressed_reason`` for salience) are optional and
    omitted when not applicable so dashboards do not have to filter on
    ``<missing>`` sentinels.
    """
    attrs: dict[str, str] = {"agent.id": agent_id, "wake.kind": wake_kind}
    if timer_id is not None:
        attrs["timer_id"] = timer_id
    if tier is not None:
        attrs["tier"] = tier
    if suppressed_reason is not None:
        attrs["suppressed_reason"] = suppressed_reason
    return attrs


__all__ = ["register", "wake_attrs"]
