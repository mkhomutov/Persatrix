"""RFC 0030 producer plan OQ 5 — the wire-carried close-cause labels.

The orchestrator stamps the retired interaction's id + close trigger
(``previous_interaction_id`` / ``previous_interaction_close_trigger``)
onto every publish of the successor, and the rotation-close seam
(``agents/persona_runtime/interaction_boundary.wire_rotation_close_reason``)
labels the local boundary by it — applied only when the retired id
matches the wire id the open local record was opened under.  An idle
rotation finally stops rendering as "ended" (PR 607 review finding 3);
absent or mismatched cause keeps the legacy structural label — the
mixed-version contract for an old orchestrator and the post-restart
re-mint.

Split out of :mod:`test_interaction_channel_close_propagation` (the
vote / rotation propagation seams) for the 500-line cap; shared persona
config / event builders live in :mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import pytest

from agents.clock import FrozenClock
from agents.memory.interactions import REASON_IDLE_GAP, REASON_STRUCTURAL
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    all_episodes,
    channel_event,
    close_reasons,
    make_agent_with_clock,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
class TestWireRotationCloseCause:
    async def test_idle_cause_closes_as_idle_gap(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("old topic", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event(
                "new topic", wire_id="wire-B",
                prev_id="wire-A", prev_trigger="idle",
            ),
            [],
        )
        episodes = await all_episodes(agent)
        assert close_reasons(episodes) == [REASON_IDLE_GAP]

    async def test_end_votes_cause_closes_as_structural(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("old topic", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event(
                "new topic", wire_id="wire-B",
                prev_id="wire-A", prev_trigger="end_votes",
            ),
            [],
        )
        episodes = await all_episodes(agent)
        assert close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_mismatched_predecessor_keeps_structural(self):
        # The agent missed a generation (opened under wire-A, the channel
        # is now two rotations on): the stamped cause attributes wire-B's
        # close, not wire-A's, so it must be discarded — the legacy
        # structural label is the honest fallback.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("old topic", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event(
                "newer topic", wire_id="wire-C",
                prev_id="wire-B", prev_trigger="idle",
            ),
            [],
        )
        episodes = await all_episodes(agent)
        assert close_reasons(episodes) == [REASON_STRUCTURAL]

    async def test_absent_cause_keeps_structural(self):
        # Old orchestrator / post-restart re-mint: no cause fields at
        # all — byte-identical to the pre-OQ5 behaviour.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await agent._store_event_episode(
            channel_event("old topic", wire_id="wire-A"), [],
        )
        await agent._store_event_episode(
            channel_event("new topic", wire_id="wire-B"), [],
        )
        episodes = await all_episodes(agent)
        assert close_reasons(episodes) == [REASON_STRUCTURAL]
