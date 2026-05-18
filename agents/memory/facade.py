"""``MemoryFacade`` — backward-compat alias for ``MemoryStore`` (RFC 0029 Phase 1).

RFC 0029 Phase 1 promotes the RFC 0008 ``MemoryFacade`` to the typed
:class:`~agents.memory.store.MemoryStore` facade.  The facade *home* is
now :mod:`agents.memory.store`; this module survives as a thin
compatibility shim for one minor version (removed in v0.3.3):

- ``MemoryFacade`` is re-bound to ``MemoryStore`` — the **same class
  object**, so every existing ``MemoryFacade()`` construction,
  ``isinstance(x, MemoryFacade)`` check, and ``MemoryFacade.compress``
  call keeps working unchanged until the RFC 0029 Phase 1 PR 3 call-site
  sweep migrates them.
- The facade-level dataclasses (``MemoryEntry`` / ``CompressedView`` /
  ``Candidate``) and ``MemoryDisabledError`` are re-exported so existing
  ``from agents.memory.facade import ...`` imports keep resolving.

``budget_to_limit`` lives here: it translates the orchestrator's RFC 0008
``_context_package.budget_memory_tokens`` into a recall ``limit`` and has
no dependency on the ``MemoryStore`` class.
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

#: Backward-compat alias.  ``MemoryFacade`` *is* ``MemoryStore`` — the same
#: class object, not a subclass — so identity and ``isinstance`` checks
#: hold across the rename.
MemoryFacade = MemoryStore


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
    "MemoryFacade",
    "MemoryStore",
    "SocietyBackendUnavailable",
    "SocietyDisabled",
    "SocietyTransientError",
    "StoreConfig",
    "budget_to_limit",
]
