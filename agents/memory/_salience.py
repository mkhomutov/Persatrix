"""RFC 0024 Phase 3 — conservative write-side salience scoring (PR 3a).

Single place to flip for the calibration follow-up named in
[v0.3.x sequencing §Open questions §3](../../docs/v0.3.x-sequencing.md#open-questions).
Per-tier constants live here so :mod:`agents.memory.episodic` /
:mod:`agents.memory.notes` / :mod:`agents.memory.facts` /
:mod:`agents.memory.relationship` can all consult one source of truth.

The default-off invariant PR 3b ships (threshold default ``0.95`` strictly
above PR 3a's maximum scoring of ``0.6``) is pinned by
:mod:`agents.tests.test_event_loop_salience_default_off` once PR 3b lands.
Editing :data:`REFLECTION_CONTRADICTION_SALIENCE` upward without
re-evaluating the PR 3b threshold breaks that invariant.
"""

from __future__ import annotations

import contextlib

from ..observability.spans import current_llm_span_id
from ._events import MemoryTier, emit_memory_write

# ─── Per-tier conservative defaults ─────────────────────────────────────────


EPISODIC_APPEND_SALIENCE: float = 0.0
NOTES_APPEND_SALIENCE: float = 0.0
RELATIONSHIP_APPEND_SALIENCE: float = 0.0
FACTS_APPEND_SALIENCE: float = 0.0

# Reflection-contradiction salience has no production write site at v0.3.3 —
# it lives here for PR 3b's default-off invariant and for the future RFC 0027
# reflection-consolidation work to consume.  The value is named so the
# calibration follow-up is a one-line change.
REFLECTION_CONTRADICTION_SALIENCE: float = 0.6


# ─── Write-site emit shim ───────────────────────────────────────────────────


def emit_for_tier(
    *,
    agent_id: str,
    tier: MemoryTier,
    salience: float,
) -> None:
    """Publish a :class:`MemoryWriteEvent` for ``tier`` after a successful write.

    Captures the active OTEL span id at call time as ``source_span_id`` (PR 3b's
    loop-back guard input).  Wraps the publish in ``contextlib.suppress`` so a
    bus-subscriber bug cannot surface as a write failure — the row is already
    persisted by the time the caller invokes this shim.
    """
    with contextlib.suppress(Exception):
        emit_memory_write(
            agent_id=agent_id,
            tier=tier,
            salience=salience,
            source_span_id=current_llm_span_id(),
        )


__all__ = [
    "EPISODIC_APPEND_SALIENCE",
    "FACTS_APPEND_SALIENCE",
    "NOTES_APPEND_SALIENCE",
    "REFLECTION_CONTRADICTION_SALIENCE",
    "RELATIONSHIP_APPEND_SALIENCE",
    "emit_for_tier",
]
