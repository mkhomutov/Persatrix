"""Orchestr8 Agent Memory System (v0.2+)."""

from typing import Protocol, runtime_checkable

from .episodic import Episode, EpisodicMemory, Note
from .relationship import Interaction, RelationshipMemory, RelationshipSummary
from .working import ContextSection, WorkingMemory, estimate_tokens


@runtime_checkable
class MemoryLifecycle(Protocol):
    """Protocol for memory components that manage async resources."""

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...


__all__ = [
    "ContextSection",
    "Episode",
    "EpisodicMemory",
    "Interaction",
    "MemoryLifecycle",
    "Note",
    "RelationshipMemory",
    "RelationshipSummary",
    "WorkingMemory",
    "estimate_tokens",
]
