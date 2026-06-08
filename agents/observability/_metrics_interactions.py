"""Interaction-lifecycle counter registrations (RFC 0020 Phase 1).

Split out of :mod:`agents.observability.metrics` so the parent module
stays under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  The split is by topic, not by
visibility — every counter registered here is assigned as an attribute
on the parent :class:`_Instruments` instance, so call sites continue to
reach them via ``inst.interactions_opened`` / ``inst.interactions_closed``
/ ``inst.interactions_closed_by_<reason>`` etc. without any rename.

Per-reason ``by_<reason>`` subtotals are dispatched from the
``_REASON_COUNTER_ATTR`` table in :mod:`agents.memory.interactions`;
adding a new reason is the coordinated edit documented there
(constant + counter registration here + table entry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register every ``agent.interactions.*`` counter on ``inst``.

    Names follow RFC 0020 Phase 1 §5; emitted by
    :class:`agents.memory.interactions.InteractionTracker`.  Per-tracker
    call sites land in PRs 2–4 — counters are registered here so
    dashboards can be wired ahead of the routing rollout.
    """
    inst.interactions_opened = meter.create_counter(
        name="agent.interactions.opened", unit="{interaction}",
        description="Interactions opened by the InteractionTracker (RFC 0020).",
    )
    inst.interactions_closed = meter.create_counter(
        name="agent.interactions.closed", unit="{interaction}",
        description="Interactions closed (any reason) by the InteractionTracker.",
    )
    inst.interactions_closed_by_idle_gap = meter.create_counter(
        name="agent.interactions.closed.by_idle_gap", unit="{interaction}",
        description="Interactions closed by the idle-gap timer (RFC 0020 §B).",
    )
    inst.interactions_closed_by_structural = meter.create_counter(
        name="agent.interactions.closed.by_structural", unit="{interaction}",
        description="Interactions closed by structural triggers (RFC 0020 §B).",
    )
    inst.interactions_closed_by_max_turns = meter.create_counter(
        name="agent.interactions.closed.by_max_turns", unit="{interaction}",
        description="Interactions closed by the max-turns safety net (RFC 0020 §Security).",
    )
    inst.interactions_closed_by_topic_shift = meter.create_counter(
        name="agent.interactions.closed.by_topic_shift", unit="{interaction}",
        description="Interactions closed by topic-shift detection (RFC 0020 §B Phase 4).",
    )
    inst.interactions_closed_by_shutdown = meter.create_counter(
        name="agent.interactions.closed.by_shutdown", unit="{interaction}",
        description="Interactions closed by process-shutdown drain (RFC 0020 §C).",
    )
    inst.interactions_closed_by_cost = meter.create_counter(
        name="agent.interactions.closed.by_cost", unit="{interaction}",
        description=(
            "Interactions closed by the RFC 0030 Layer 1 per-interaction "
            "cost ceiling (interaction_budget_tokens exhausted)."
        ),
    )
    inst.interactions_summary_failed = meter.create_counter(
        name="agent.interactions.summary.failed", unit="{interaction}",
        description="Interactions whose close-time summary call failed (RFC 0020 §C).",
    )
    inst.interactions_janitor_failed = meter.create_counter(
        name="agent.interactions.janitor.failed", unit="{sweep}",
        description="Closing-state janitor sweeps that raised before completing (RFC 0020 §C).",
    )


__all__ = ["register"]
