"""Shared test doubles for ``test_inject_memory_context*.py``.

Test files are split to stay under the 500-line review-friendly cap; the
doubles live in this underscore-prefixed module (not a test file) so both
test files import one canonical source instead of duplicating them — the
same arrangement ``_scheduled_wakes_wiring_helpers.py`` uses.

Consolidating them is not just tidiness.  These fakes stand in for real
persistence rows, and every field production grows is a field they must
grow too.  While each test file carried its own near-copy, that debt was
owed twice and paid neither time.  One copy means the next production
field is added once, and the two files cannot disagree about the shape of
a row.

This module is a pure extraction: the doubles below are the ones
``test_inject_memory_context.py`` already carried, unchanged.  The gates
file's slightly leaner near-copy is dropped in favour of this superset —
it differed only by lacking ``_make_mixin``'s ``rel`` parameter and
``FakeNote.id``, neither of which changes how its tests behave.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agents.clock import WallClock
from agents.persona_runtime.memory_context import _MemoryContextMixin

__all__ = [
    "ConcreteMemoryMixin",
    "FakeEpisode",
    "FakeNote",
    "FakeRelSummary",
    "make_mixin",
]


@dataclass
class FakeEpisode:
    summary: str
    id: str = "ep-0001"
    # RFC 0021 PR 2: temporal fields accessed by recency rendering.
    # created_at mirrors the DB NOT NULL column — always set in production.
    created_at: float = 0.0
    closed_at: float | None = None
    started_at: float | None = None
    turn_count: int | None = None


@dataclass
class FakeNote:
    topic: str
    content: str
    # RFC 0026 PR 4 wired ``record_admission(item_id=note.id)`` into the
    # notes tier of ``_inject_memory_context``; this fake predates that
    # and must carry an ``id`` or every admitted-note path raises
    # ``AttributeError``.  Default keeps the existing keyword-only
    # constructors (``FakeNote(topic=…, content=…)``) working.
    id: str = "note-fake"


@dataclass
class FakeRelSummary:
    other_participant_id: str
    other_participant_type: str = "agent"
    interaction_count: int = 1
    # Default away from _DEFAULT_TRUST_SCORE (0.5) so the trust-injection
    # branch in _inject_memory_context (which only emits "Trust: ..." when
    # the score deviates from the default by more than
    # _TRUST_DEVIATION_THRESHOLD) is reachable from tests that do not
    # explicitly set trust_score.
    # (PR #146 re-review: trust branch unreachable with the prior 0.5 default.)
    trust_score: float = 0.7
    notes: str = ""
    # RFC 0021 PR 2: temporal fields accessed by recency + cadence rendering.
    last_interaction_at: float | None = None
    first_interaction_at: float | None = None


def make_mixin(
    *,
    episodes: list[FakeEpisode] | None = None,
    notes: list[FakeNote] | None = None,
    rel: FakeRelSummary | None = None,
    sender_id: str | None = None,
    event_type: str = "CHANNEL_MESSAGE",
) -> tuple[ConcreteMemoryMixin, Any]:
    """Return a wired _MemoryContextMixin instance and a matching fake event."""
    from agents.memory.working import WorkingMemory
    from agents.persona_types import EventType

    mixin = ConcreteMemoryMixin()
    mixin.agent_id = "test-agent"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    # RFC 0021 PR 2: temporal seam required by _MemoryContextMixin._inject_memory_context.
    mixin._clock = WallClock()
    mixin._timezone = "UTC"

    # Wire episodic memory mock.
    mixin._episodic_memory = AsyncMock()
    mixin._episodic_memory.recall.return_value = episodes or []
    mixin._episodic_memory.recall_notes.return_value = notes or []

    # Wire relationship memory mock.
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = rel

    et = getattr(EventType, event_type)
    event = MagicMock()
    event.event_type = et
    event.sender_id = sender_id
    event.metadata = {}
    event.payload = {"content": "hello"}

    return mixin, event


class ConcreteMemoryMixin(_MemoryContextMixin):
    """Minimal concrete subclass for testing _MemoryContextMixin."""

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        return str(event.payload.get("content", ""))
