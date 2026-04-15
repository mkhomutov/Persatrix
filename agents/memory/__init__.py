"""Persatrix Agent Memory System (v0.2+)."""

from typing import Protocol, runtime_checkable

from .episodic import Episode, EpisodicMemory
from .notes import Note, NoteStore
from .relationship import Interaction, RelationshipMemory, RelationshipSummary
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
    "ContextSection",
    "Episode",
    "EpisodicMemory",
    "Interaction",
    "MemoryLifecycle",
    "Note",
    "NoteStore",
    "RelationshipMemory",
    "RelationshipSummary",
    "WorkingMemory",
    "estimate_tokens",
]
