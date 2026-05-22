""":mod:`agents.memory.facade` — ``budget_to_limit`` + store re-exports.

RFC 0029 Phase 1 promoted the RFC 0008 ``MemoryFacade`` to the typed
:class:`~agents.memory.store.MemoryStore`; the facade *home* is now
:mod:`agents.memory.store`.  The one-minor-version ``MemoryFacade``
compatibility alias the v0.3.2 Upgrade Notes committed to removing in
v0.3.3 has been removed — every call site imports ``MemoryStore``
directly (the RFC 0029 Phase 1 PR 3 sweep migrated production code; the
v0.3.3 release-prep migrated the test suite).

This module survives because it owns work the store class does not:

- ``budget_to_limit`` translates the orchestrator's RFC 0008
  ``_context_package.budget_memory_tokens`` into a recall ``limit`` and
  has no dependency on the ``MemoryStore`` class.
- The store surface (``MemoryStore`` / ``StoreConfig`` / the society
  errors) and the facade-level dataclasses (``MemoryEntry`` /
  ``CompressedView`` / ``Candidate``) and ``MemoryDisabledError`` are
  re-exported so existing ``from agents.memory.facade import ...``
  imports keep resolving.
"""

from __future__ import annotations

from .store import (
    MemoryStore,
    SocietyBackendUnavailable,
    SocietyDisabled,
    SocietyTransientError,
    StoreConfig,
)
from .store_types import (
    Candidate,
    CompressedView,
    MemoryDisabledError,
    MemoryEntry,
)

# ─── Budget translation helper ─────────────────────────────────


# RFC 0008 PR plan PR 2 §Key implementation details: agents translate the
# advisory ``budget_memory_tokens`` field from the orchestrator's
# ``_context_package`` into a ``retrieve_relevant(limit=...)`` call by
# dividing by an average per-entry token cost.  The constant is calibrated
# in PR 5; until then the value matches the PR plan integration test
# (``budget=500 → limit=5``).
DEFAULT_AVG_ENTRY_TOKENS = 100


def budget_to_limit(
    budget_memory_tokens: int,
    *,
    avg_entry_tokens: int = DEFAULT_AVG_ENTRY_TOKENS,
    fallback_limit: int = 5,
) -> int:
    """Translate the advisory orchestrator budget into a recall ``limit``.

    Returns ``fallback_limit`` when the orchestrator emits 0 (the PR 1
    placeholder value — RFC 0008 PR plan PR 2 keeps the orchestrator-side
    allocator out of scope under the facade-only split).  Always returns
    ``>= 1`` so a positive budget never collapses to a no-op recall.
    """
    if budget_memory_tokens <= 0:
        return max(1, fallback_limit)
    if avg_entry_tokens <= 0:
        raise ValueError(f"avg_entry_tokens must be positive, got {avg_entry_tokens}")
    return max(1, budget_memory_tokens // avg_entry_tokens)


__all__ = [
    "Candidate",
    "CompressedView",
    "DEFAULT_AVG_ENTRY_TOKENS",
    "MemoryDisabledError",
    "MemoryEntry",
    "MemoryStore",
    "SocietyBackendUnavailable",
    "SocietyDisabled",
    "SocietyTransientError",
    "StoreConfig",
    "budget_to_limit",
]
