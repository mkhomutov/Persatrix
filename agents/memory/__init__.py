"""Persatrix Agent Memory System (v0.2+)."""

from typing import Protocol, runtime_checkable

from .episodic import Episode, EpisodicMemory
from .eviction import EvictionPass, EvictionStats
from .facade import (
    Candidate,
    CompressedView,
    MemoryDisabledError,
    MemoryEntry,
    MemoryFacade,
    budget_to_limit,
)
from .notes import Note, NoteStore
from .relationship import RelationshipMemory
from .relationship_types import Interaction, RelationshipSummary
from .shared_pool import SharedPoolRegistry
from .working import ContextSection, WorkingMemory, estimate_tokens


@runtime_checkable
class MemoryLifecycle(Protocol):
    """Protocol for memory components that manage async resources.

    ``EpisodicMemory`` and ``WorkingMemory`` satisfy this protocol
    structurally.  ``RelationshipMemory`` does **not** — its
    ``initialize()`` accepts an optional ``config_relationships``
    parameter for trust seeding, so the signature differs.

    Note: ``@runtime_checkable`` only checks method *existence*, not
    signatures, so ``isinstance(rm, MemoryLifecycle)`` will return
    ``True`` for ``RelationshipMemory`` even though argument lists
    differ.  Callers needing strict type-safety should use static
    type checkers (mypy / pyright) rather than runtime ``isinstance``.
    """

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "Candidate",
    "CompressedView",
    "ContextSection",
    "Episode",
    "EpisodicMemory",
    "EvictionPass",
    "EvictionStats",
    "Interaction",
    "MemoryDisabledError",
    "MemoryEntry",
    "MemoryFacade",
    "MemoryLifecycle",
    "Note",
    "NoteStore",
    "RelationshipMemory",
    "RelationshipSummary",
    "SharedPoolRegistry",
    "WorkingMemory",
    "budget_to_limit",
    "estimate_tokens",
]
