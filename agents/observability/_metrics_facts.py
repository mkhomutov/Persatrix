"""Declarative-fact tier counter registrations (RFC 0026 PR 1).

Split out of :mod:`agents.observability.metrics` so the parent module
stays under the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  Mirrors the
:mod:`agents.observability._metrics_interactions` split — every
counter registered here is assigned as an attribute on the parent
:class:`_Instruments` instance, so call sites reach them via
``inst.facts_stored`` / ``inst.facts_superseded`` /
``inst.facts_extraction_failed`` without any rename.

All four keep the ``agent.`` prefix because facts are a per-agent
tier (cardinality bounded by ``agent.id``), unlike the cross-binary
``sessions.writes`` counter registered inline in the parent module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.metrics import Meter

    from .metrics import _Instruments


def register(inst: _Instruments, meter: Meter) -> None:
    """Register every ``agent.facts.*`` counter on ``inst``.

    ``stored`` and ``superseded`` are incremented by
    :meth:`agents.memory.facts.FactStore.store` — one tick per row
    written; an additional ``superseded`` tick when the
    latest-asserted-wins write supersedes an older row.
    ``extraction_failed`` is reserved by PR 1 and incremented by
    PR 2's combined summarize + extract prompt when fact-tuple JSON
    parsing fails (summary still commits; RFC 0026 Phase 1 step 4
    atomicity contract).
    """
    inst.facts_stored = meter.create_counter(
        name="agent.facts.stored",
        unit="{fact}",
        description=(
            "Declarative-fact rows persisted to the ``facts`` table "
            "(RFC 0026 PR 1).  Attribute: agent.id."
        ),
    )
    inst.facts_superseded = meter.create_counter(
        name="agent.facts.superseded",
        unit="{fact}",
        description=(
            "Older fact rows replaced by a later ``(subject, predicate)`` "
            "write under RFC 0026 §F latest-asserted-wins retraction.  "
            "Attribute: agent.id."
        ),
    )
    inst.facts_extraction_failed = meter.create_counter(
        name="agent.facts.extraction_failed",
        unit="{failure}",
        description=(
            "Combined summarize + extract calls where the fact-tuple "
            "JSON parse failed; summary commits, facts do not "
            "(RFC 0026 Phase 1 step 4).  Reserved by PR 1; incremented "
            "by PR 2.  Attribute: agent.id."
        ),
    )
    # RFC 0026 PR 3 — facts-tier admission counter.  Increments per
    # fact row admitted into the working-memory ``facts_context``
    # section by :func:`agents.persona_runtime.facts_section.render_facts_section`.
    # ``tier="facts"`` is a low-cardinality dimension that lets a
    # future provenance dashboard join this counter against the
    # per-turn tier-provenance log RFC 0026 PR 4 emits.
    inst.facts_injected = meter.create_counter(
        name="agent.facts.injected",
        unit="{fact}",
        description=(
            "Declarative-fact rows admitted into the persona's "
            "working memory ``facts_context`` section (RFC 0026 PR 3). "
            "Attributes: agent.id, tier."
        ),
    )
