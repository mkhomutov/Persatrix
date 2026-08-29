"""Shared test doubles for ``test_inject_memory_context*.py``.

Test files are split to stay under the 500-line review-friendly cap; the
doubles live in this underscore-prefixed module (not a test file) so both
test files import one canonical source instead of duplicating them — the
same arrangement ``_scheduled_wakes_wiring_helpers.py`` uses.

Consolidating them is not just tidiness.  These fakes stand in for real
persistence rows, and every field production grows is a field they must
grow too.  While each test file carried its own copy, that debt was owed
twice and paid neither time: both copies drifted behind ``Episode`` and
``Note`` in exactly the same three ways (RFC 0021 temporal fields, the
scope/tags projection, the RFC 0037 §C protection columns).  One copy
means the next production field is added once, and the tests that would
catch its absence cannot disagree about the shape of a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.channel_event_classification import CHANNEL_CLASSIFICATION_METADATA_KEY
from agents.clock import WallClock
from agents.memory._migration_protection import PROTECTION_LEVEL_DEFAULT
from agents.persona_runtime.classification import CLASSIFICATION_INTERNAL
from agents.persona_runtime.memory_context import _MemoryContextMixin

__all__ = [
    "ConcreteMemoryMixin",
    "FakeEpisode",
    "FakeNote",
    "FakeRelSummary",
    "episodic_tier",
    "make_mixin",
]


@dataclass
class FakeEpisode:
    """Stand-in for ``memory.episode_types.Episode``.

    Carries every attribute the injection path reads.  A missing one does
    not fail loudly: each tier wraps its recall in a broad ``except`` that
    logs a WARNING and continues with an empty result, so an out-of-date
    fake silently zeroes the admitted-token assertions instead.
    """

    summary: str
    id: str = "ep-0001"
    # RFC 0021 PR 2: temporal fields accessed by recency rendering.
    # created_at mirrors the DB NOT NULL column — always set in production.
    created_at: float = 0.0
    closed_at: float | None = None
    started_at: float | None = None
    turn_count: int | None = None
    # The channel-history tier reaches ``recall_with_scope_filter``, which
    # reads ``tags``/``scope``/``context`` off every row it ranks; ``tags``
    # and ``context`` are declared without defaults on the real ``Episode``.
    tags: list[str] = field(default_factory=list)
    scope: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    # RFC 0037 §C/§D (migration v16): the §D injection gate ranks every
    # candidate by its protection label and fails closed on one it cannot
    # parse (rule (c)), so a fake reading back ``level=None`` has every row
    # withheld as an unknown-label casualty.  The real ``Episode`` defaults
    # to the v16 column DEFAULT precisely so a hand-built fixture
    # round-trips; match it.
    protection_level: str = PROTECTION_LEVEL_DEFAULT
    source_channel_id: str | None = None


@dataclass
class FakeNote:
    """Stand-in for ``memory.note_types.Note`` (same gating as above)."""

    topic: str
    content: str
    # RFC 0026 PR 4 wired ``record_admission(item_id=note.id)`` into the
    # notes tier of ``_inject_memory_context``; this fake predates that
    # and must carry an ``id`` or every admitted-note path raises
    # ``AttributeError``.  Default keeps the existing keyword-only
    # constructors (``FakeNote(topic=…, content=…)``) working.
    id: str = "note-fake"
    # RFC 0037 §C/§D (v16): the notes tier is gated too, and the real
    # ``Note`` carries the same v16 default as ``Episode``.
    protection_level: str = PROTECTION_LEVEL_DEFAULT
    source_channel_id: str | None = None


@dataclass
class FakeRelSummary:
    """Stand-in for the relationship tier's summary row."""

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


class ConcreteMemoryMixin(_MemoryContextMixin):
    """Minimal concrete subclass for testing _MemoryContextMixin."""

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        return str(event.payload.get("content", ""))


async def _forward_to_recall(
    memory: Any,
    query: str = "",
    *,
    limit: int = 10,
    min_importance: float = 0.0,
    min_score: float | None = None,
    reinforce: bool = False,
) -> Any:
    """Mirror ``recall_room_ranked``'s call onto the ``recall`` test double."""
    return await memory.recall(
        query, limit=limit, min_score=min_score, sessions=None,
    )


@pytest.fixture(autouse=True)
def episodic_tier(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stand in for the room-ranked episodic entry point (RFC 0049 PR 4).

    ``cross_room: live`` is the promoted default, so the episodic tier of
    ``_inject_memory_context`` runs through ``recall_room_ranked`` and no
    longer calls ``EpisodicMemory.recall`` itself.  ``recall_room_ranked``
    drives the real SQLite query layer (``recall_fts5`` does
    ``async with db.execute(...)``), which an ``AsyncMock`` store cannot
    satisfy — so without this seam the tier raises ``AttributeError:
    'coroutine' object has no attribute 'execute'``, is swallowed to a
    WARNING, and every case runs against a silently empty episodic tier.

    Patching here keeps the production branch under test while letting
    ``make_mixin`` keep expressing fixtures through ``recall``.  Returned
    so a test can assert on the tier call itself.

    Autouse, and active in any module that imports it.
    """
    mock = AsyncMock(side_effect=_forward_to_recall)
    monkeypatch.setattr(
        "agents.persona_runtime.memory_context.recall_room_ranked", mock,
    )
    return mock


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
    # RFC 0021 PR 2: temporal seam required by _inject_memory_context.
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
    # RFC 0037 §B/§D: the ingress seed stamps the acting channel's
    # classification onto every channel-anchored event, and the §D gate
    # withholds any entry ranked ABOVE it.  An unstamped event resolves to
    # the rule-(b) ``public`` floor, under which the ``internal``-by-default
    # rows above are all withheld — which zeroes every admission assertion.
    # Stamp the event the way a real producer does.  Floor-class types
    # (TICK) ignore metadata outright, so this is inert for them.
    event.metadata = {CHANNEL_CLASSIFICATION_METADATA_KEY: CLASSIFICATION_INTERNAL}
    event.payload = {"content": "hello"}

    return mixin, event
