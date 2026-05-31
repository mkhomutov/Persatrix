"""Extractive compression helper for :class:`agents.memory.store.MemoryStore`.

Split out of :mod:`agents.memory.store` so the frozen facade stays under
the project's 500-line review-friendly cap (see
``scripts/checks/file_size.py``).  :meth:`MemoryStore.compress` re-exports
:func:`compress_entries` as a ``staticmethod`` so the RFC 0020 PR 4 call
site (``MemoryStore.compress(...)`` in
:mod:`agents.persona_runtime.summarize_close`) is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable

from .store_types import CompressedView, MemoryEntry
from .working import estimate_tokens

__all__ = ["compress_entries"]


def compress_entries(
    entries: Iterable[MemoryEntry],
    *,
    target_tokens: int,
) -> CompressedView:
    """Extractively compress *entries* into a view of ≤ ``target_tokens``.

    RFC 0020 PR 4 contract.  Phase 2 implementation: highest-importance
    first, in-order until the running token count would exceed
    ``target_tokens``.  Idempotent.  Entries individually larger than
    ``target_tokens`` are silently skipped (knapsack-suboptimal but
    acceptable for Phase 2 — the extractive path is a stop-gap until
    PR 5's abstractive path) and count toward ``entries_dropped``.
    A free function so RFC 0020 PR 4's persona-runtime call site can
    invoke it (via the :meth:`MemoryStore.compress` staticmethod
    re-export) without a facade instance.  Pinned by
    [RFC 0020 PR plan](../../docs/rfcs/0020-pr-plan.md) PR 4.
    """
    if target_tokens < 0:
        raise ValueError(f"target_tokens must be >= 0, got {target_tokens}")
    entry_list = list(entries)
    # Stable-sort by importance descending so equal-importance entries
    # retain their input order (deterministic for tests).
    ordered = sorted(entry_list, key=lambda e: -e.importance)
    admitted_chunks: list[str] = []
    admitted_tokens = 0
    admitted_count = 0
    # tokens_before is summed over the original input set.
    tokens_before = sum(estimate_tokens(e.content) for e in entry_list)
    for entry in ordered:
        entry_tokens = estimate_tokens(entry.content)
        if admitted_tokens + entry_tokens > target_tokens:
            continue
        admitted_chunks.append(entry.content)
        admitted_tokens += entry_tokens
        admitted_count += 1
    return CompressedView(
        summary="\n\n".join(admitted_chunks),
        entries_dropped=len(entry_list) - admitted_count,
        tokens_before=tokens_before,
        tokens_after=admitted_tokens,
    )
