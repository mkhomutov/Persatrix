"""Temporal-awareness metric registrations (RFC 0021 Phase 1 — PR 2).

Split out of :mod:`agents.observability.metrics` so the parent module
stays under the project's 500-line review-friendly cap (mirrors the
:mod:`._metrics_facts` / :mod:`._metrics_interactions` /
:mod:`._metrics_persona_tick` / :mod:`._metrics_wakes` splits).

Two counters: now-anchor emissions (one per system-prompt build,
bounded by the per-event prompt-assembly call rate) and recency tag
renders (one per episode/relationship line; ``source`` attribute
distinguishes the two surfaces so dashboards can show drift between
recall volume and prompt-line volume).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register the two ``agent.temporal.*`` counters on ``inst``."""
    inst.temporal_now_anchor_emitted = meter.create_counter(
        name="agent.temporal.now_anchor.emitted",
        unit="{prompt}",
        description=(
            "Persona system prompts that included the RFC 0021 §C "
            "now-anchor block."
        ),
    )
    inst.temporal_recency_rendered = meter.create_counter(
        name="agent.temporal.recency.rendered",
        unit="{render}",
        description=(
            "Recency tags rendered onto recalled episodes, "
            "relationship summaries, or channel-history turns "
            "(RFC 0021 §D / §E + RFC 0011 §E).  Attributes: "
            "agent.id, source (episode|relationship|channel_history)."
        ),
    )


__all__ = ["register"]
