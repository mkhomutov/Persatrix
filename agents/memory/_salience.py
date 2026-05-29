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

from ..observability.metrics import try_get_instruments
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
    loop-back guard input).

    The ``contextlib.suppress`` is defence-in-depth against *non-subscriber*
    failure modes — subscriber exceptions are already swallowed inside
    :meth:`MemoryWriteBus.publish`.  The suppress here catches anything
    upstream of the fan-out: a future :meth:`MemoryWriteEvent.__post_init__`
    validation that raises, an OTEL API change in :func:`current_llm_span_id`,
    or a type-system gap that lets a bad ``tier`` reach this shim.  The row
    is already persisted by the time the caller invokes this shim, so a
    raise here would surface as a write failure for a row that did, in fact,
    write — the suppress preserves the failure-isolation contract.
    """
    with contextlib.suppress(Exception):
        emit_memory_write(
            agent_id=agent_id,
            tier=tier,
            salience=salience,
            source_span_id=current_llm_span_id(),
        )


def emit_session_write(
    *,
    agent_id: str,
    session_id: str,
    surface: str,
) -> None:
    """Increment the per-session ``sessions.writes`` counter after a write.

    The RFC 0031 Phase 1 ``sessions.writes`` instrument (PR #337 M1) is
    emitted from every persona-memory write boundary (episodes / notes /
    relationships) with the same shape; extracted here so the three sites
    share one shim instead of re-implementing the ``try_get_instruments``
    + ``contextlib.suppress`` block (and so ``episodic.py`` stays under the
    file-size cap once ISSUE-0081 PR 3 added the tenant dimension).

    Like :func:`emit_for_tier`, the ``contextlib.suppress`` preserves the
    failure-isolation contract: the row is already persisted, so a
    metrics-backend failure must not surface as a write failure.
    """
    with contextlib.suppress(Exception):
        inst = try_get_instruments()
        if inst is not None:
            inst.sessions_writes.add(
                1,
                attributes={
                    "session_id": session_id,
                    "agent.id": agent_id,
                    "surface": surface,
                },
            )


__all__ = [
    "EPISODIC_APPEND_SALIENCE",
    "FACTS_APPEND_SALIENCE",
    "NOTES_APPEND_SALIENCE",
    "REFLECTION_CONTRADICTION_SALIENCE",
    "RELATIONSHIP_APPEND_SALIENCE",
    "emit_for_tier",
    "emit_session_write",
]
