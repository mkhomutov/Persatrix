"""Channel-history tier in ``_inject_memory_context`` (RFC 0011 PR 5 follow-up).

Pins the contract from [RFC 0011 §E](docs/rfcs/0011-channels-bridges.md#e-memory-integration)
and [RFC 0021 §J](docs/rfcs/0021-persona-temporal-awareness.md#j-token-budget-integration):

- A new ``"channel_history"`` tier admits same-scope episodes between the
  relationship and episodic tiers, gated on ``EventType.CHANNEL_MESSAGE``.
- The tier delegates to the shared
  :func:`agents.memory.scope_recall.recall_with_scope_filter` helper so the
  filter contract does not fork between the facade and the persona-runtime.
- Non-channel events (TICK, etc.) skip the tier entirely.
- Recall failure is logged and non-fatal; downstream tiers still run.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.working import WorkingMemory
from agents.persona_runtime.memory_context import _MemoryContextMixin
from agents.persona_types import EventType

# ─── Fixture ──────────────────────────────────────────────────


@pytest.fixture
async def seeded_episodic() -> AsyncGenerator[EpisodicMemory, None]:
    """In-memory ``EpisodicMemory`` with three channel-scoped episodes.

    Two episodes scoped ``group:planning`` (the channel under test) plus
    one in ``group:other`` so the scope filter has something to exclude.
    """
    mem = EpisodicMemory(agent_id="channel-history-test", db_path=":memory:")
    await mem.initialize()
    try:
        await mem.store_episode(
            summary="Planning room: agreed on the launch checklist",
            context={},
            importance=0.9,
            scope="group:planning",
        )
        await mem.store_episode(
            summary="Planning room: REDFROG-7841 milestone confirmed for Q3",
            context={},
            importance=0.9,
            scope="group:planning",
        )
        await mem.store_episode(
            summary="Other room: unrelated discussion about lunch",
            context={},
            importance=0.9,
            scope="group:other",
        )
        yield mem
    finally:
        await mem.close()


@dataclass
class _FakeRelSummary:
    other_participant_id: str = "agent-a"
    other_participant_type: str = "agent"
    interaction_count: int = 0
    trust_score: float = 0.5
    notes: str | None = None
    last_interaction_at: float | None = None
    first_interaction_at: float | None = None


class _ConcreteMemoryMixin(_MemoryContextMixin):
    def __init__(self) -> None:
        from agents.clock import WallClock  # noqa: PLC0415 — local import

        super().__init__()
        self._clock = WallClock()
        self._timezone = "UTC"

    def _format_event(self, event: Any) -> str:  # type: ignore[override]
        payload = getattr(event, "payload", {}) or {}
        return str(payload.get("content", ""))


def _make_mixin(
    episodic: EpisodicMemory,
    *,
    event_type: EventType,
    channel_id: str | None = "group:planning",
    sender_id: str | None = "agent-a",
    thread_id: str | None = None,
    channel_type: str | None = "group",
    content: str = "planning room",
    rel: _FakeRelSummary | None = None,
) -> tuple[_ConcreteMemoryMixin, Any]:
    mixin = _ConcreteMemoryMixin()
    mixin.agent_id = "agent-b"
    mixin._working_memory = WorkingMemory(max_tokens=8192)
    mixin._episodic_memory = episodic
    mixin._relationship_memory = AsyncMock()
    mixin._relationship_memory.get_relationship_summary.return_value = rel

    event = MagicMock()
    event.event_type = event_type
    event.channel_id = channel_id
    event.sender_id = sender_id
    event.thread_id = thread_id
    event.metadata = {}
    event.payload = {"content": content, "channel_type": channel_type}
    event.timestamp = 0.0
    return mixin, event


# ─── Tests ────────────────────────────────────────────────────


async def test_channel_message_event_admits_channel_history_section(
    seeded_episodic: EpisodicMemory,
) -> None:
    """``CHANNEL_MESSAGE`` produces a ``channel_history`` working-memory section.

    Same-scope episodes are admitted; off-scope episodes are excluded.
    """
    mixin, event = _make_mixin(
        seeded_episodic,
        event_type=EventType.CHANNEL_MESSAGE,
        content="planning room",
    )
    await mixin._inject_memory_context(event)

    sections = {s.name: s for s in mixin._working_memory._sections}
    assert "channel_history" in sections, (
        "expected a channel_history section after CHANNEL_MESSAGE; "
        f"saw {list(sections)}"
    )
    body = sections["channel_history"].content
    assert "REDFROG-7841" in body or "launch checklist" in body
    # Off-scope content must not leak into the section.
    assert "lunch" not in body


async def test_tick_event_skips_channel_history_recall(
    seeded_episodic: EpisodicMemory,
) -> None:
    """A TICK event must not invoke the channel-history scope-filter helper."""
    mixin, event = _make_mixin(
        seeded_episodic,
        event_type=EventType.TICK,
        channel_id=None,
        sender_id=None,
        channel_type=None,
        content="Autonomous tick: review your goals.",
    )
    with patch(
        "agents.persona_runtime.channel_history.recall_with_scope_filter",
        new=AsyncMock(return_value=[]),
    ) as helper:
        await mixin._inject_memory_context(event)
    helper.assert_not_called()
    section_names = {s.name for s in mixin._working_memory._sections}
    assert "channel_history" not in section_names


async def test_under_populated_channel_event_skips_tier(
    seeded_episodic: EpisodicMemory,
) -> None:
    """``CHANNEL_MESSAGE`` with no channel_id / sender / thread → no recall.

    ``scope_for_channel_event`` returns ``None`` for under-populated events;
    the tier short-circuits without calling the helper or adding a section.
    """
    mixin, event = _make_mixin(
        seeded_episodic,
        event_type=EventType.CHANNEL_MESSAGE,
        channel_id=None,
        sender_id=None,
        thread_id=None,
        channel_type=None,
        content="orphaned event",
    )
    with patch(
        "agents.persona_runtime.channel_history.recall_with_scope_filter",
        new=AsyncMock(return_value=[]),
    ) as helper:
        await mixin._inject_memory_context(event)
    helper.assert_not_called()
    section_names = {s.name for s in mixin._working_memory._sections}
    assert "channel_history" not in section_names


async def test_channel_history_recall_failure_is_non_fatal(
    seeded_episodic: EpisodicMemory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A helper raise on the channel-history call must not break the rest of the budget."""
    mixin, event = _make_mixin(
        seeded_episodic,
        event_type=EventType.CHANNEL_MESSAGE,
        content="planning room",
    )

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("scope_recall exploded")

    # PR #264 review L2: the warning is emitted from
    # ``agents.persona_runtime.channel_history`` (where ``recall_channel_episodes``
    # lives), not ``...memory_context``.  Pre-fix the test passed only because
    # caplog's root-logger default captured the propagated record; a future
    # tightening of caplog scoping or a logger config change would silently
    # break the "must log" intent.
    caplog.set_level(logging.WARNING, logger="agents.persona_runtime.channel_history")
    with patch(
        "agents.persona_runtime.channel_history.recall_with_scope_filter",
        new=_boom,
    ):
        # Must not raise — log-and-continue idiom.
        await mixin._inject_memory_context(event)

    assert any(
        "channel-history" in record.message.lower()
        or "scope_recall" in record.message.lower()
        or "channel" in record.message.lower()
        for record in caplog.records
    ), f"expected a channel-history warning; saw {[r.message for r in caplog.records]}"
    section_names = {s.name for s in mixin._working_memory._sections}
    assert "channel_history" not in section_names


async def test_channel_history_uses_shared_scope_filter_helper(
    seeded_episodic: EpisodicMemory,
) -> None:
    """The tier must call ``recall_with_scope_filter`` (no parallel implementation)."""
    mixin, event = _make_mixin(
        seeded_episodic,
        event_type=EventType.CHANNEL_MESSAGE,
        content="planning room",
    )
    with patch(
        "agents.persona_runtime.channel_history.recall_with_scope_filter",
        new=AsyncMock(return_value=[]),
    ) as helper:
        await mixin._inject_memory_context(event)
    assert helper.await_count == 1
    call = helper.await_args
    # The first positional arg is the EpisodicMemory under test.
    assert call.args[0] is seeded_episodic
    # ``scope`` must be the per-channel key produced by scope_for_channel_event.
    assert call.kwargs["scope"] == "group:planning"


async def test_channel_history_off_scope_seeds_admit_no_section(
    seeded_episodic: EpisodicMemory,
) -> None:
    """A CHANNEL_MESSAGE for a channel with no matching episodes adds no section."""
    mixin, event = _make_mixin(
        seeded_episodic,
        event_type=EventType.CHANNEL_MESSAGE,
        channel_id="dm:agent-a:agent-z",
        sender_id="agent-z",
        channel_type="dm",
        content="hello there",
    )
    await mixin._inject_memory_context(event)
    section_names = {s.name for s in mixin._working_memory._sections}
    assert "channel_history" not in section_names
