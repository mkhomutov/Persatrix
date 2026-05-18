"""Shared facade-level types for :mod:`agents.memory.store` (RFC 0029 Phase 1).

Leaf module — the tier-agnostic dataclasses returned by the
:class:`~agents.memory.store.MemoryStore` facade and the memory-disabled
error live here, separate from ``MemoryStore`` itself.  Two reasons:

- both ``store.py`` and the back-compat ``facade.py`` alias shim import
  these names, and a leaf module breaks the import cycle that a direct
  ``store`` ↔ ``facade`` dependency would create;
- it keeps ``store.py`` under the repo's 500-line file cap, mirroring the
  ``relationship_types`` extraction precedent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryEntry:
    """A facade-level memory entry returned by ``retrieve_relevant``.

    Tier-agnostic projection of an underlying episode (or, in future PRs,
    a note / shared-pool entry).  Callers must not depend on the underlying
    storage tier — the facade is the boundary.
    """

    id: str
    content: str
    importance: float
    tags: tuple[str, ...]
    created_at: float
    score: float
    """Relevance score in ``[0, 1]`` from the underlying tier (0 when unranked)."""
    scope: str | None = None


@dataclass(frozen=True)
class CompressedView:
    """Result of ``MemoryStore.compress`` — the API hook required by RFC 0020 PR 4.

    Fields match RFC 0008 §B compress contract.  ``summary`` is the
    extractive concatenation of admitted entries (Phase 2); the abstractive
    path is wired in PR 5.
    """

    summary: str
    entries_dropped: int
    tokens_before: int
    tokens_after: int


@dataclass(frozen=True)
class Candidate:
    """Phase 2 stub: ``list_candidates`` returns ``[]``.  Populated in PR 5."""

    id: str
    content: str
    tokens: int
    importance: float


class MemoryDisabledError(RuntimeError):
    """Raised when memory operations are attempted on an uninitialised store.

    Per RFC 0008 PR plan PR 2 integration test: tool calls that would write
    memory must raise instead of silently no-op'ing, so the misconfiguration
    surfaces during agent startup integration testing.  Subclasses
    :class:`RuntimeError` for backward compatibility with the pre-PR-220
    facade error type.  Raised from both the write-side
    ``MemoryStore._require_initialised`` and the read-side ``episodic``
    property.
    """


__all__ = [
    "Candidate",
    "CompressedView",
    "MemoryDisabledError",
    "MemoryEntry",
]
