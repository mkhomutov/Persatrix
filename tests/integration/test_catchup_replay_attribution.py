"""ISSUE-0130 shape (b) — the replayed write, attributed, and derived once.

The v0.3.14 leak-stopper (shape (a)) bought the tenant boundary by
deriving nothing at all from an on-startup catch-up replay: a replayed
turn carried no principal, so any episode or RFC 0026 fact it produced
landed in the shared ``local`` tenant.  v0.3.15 PR B1 persisted the
tenant on the message row (channel-store ``v11 → v12``); this suite pins
what PR B2 does with it, from the REAL history JSON through the REAL
replay builder, the REAL ``on_event`` scope binding and the REAL close
path, to the stored ``episodes`` columns.

Four properties, and the last two are what make the first two safe:

1. **A row that names its tenant derives under it.**  Not ``local`` —
   the principal the orchestrator stamped at publish.
2. **A row that does not still derives nothing.**  A pre-v12
   orchestrator's history has no ``principal_id`` key at all, which is
   the one case the narrowed skip must keep refusing.  A present
   ``"local"`` is NOT that case: it is a real answer, and it derives.
3. **The same window replayed twice derives once.**  Catch-up has no
   watermark (RFC 0011 OQ #8) and re-reads the last-N page on every
   boot, so narrowing the skip without this would hand back the growth
   curve shape (a) bounded — ``local`` episodes ``0 → 2 → 5 → 13 → 18``
   across four restarts in the v0.3.14 MT — merely relocated to the
   correct tenant.  This is the release's stated acceptance bar.
4. **…and a window that GREW still derives.**  The guard bounds
   duplication; it must not cost a span the memory of messages no boot
   has seen yet.  Without this control, property 3 would also pass for
   a close path that had simply stopped deriving replays again.

The unit-level halves live in
:mod:`tests.unit.python.test_catchup_replay_principal_leak` (the skip and
its frozen-at-open marker) and
:mod:`tests.unit.python.test_replay_span_identity` (the digest).
"""

from __future__ import annotations

import pytest

from agents.channel_replay_event import build_replay_event
from agents.clock import FrozenClock
from agents.memory.interactions import scope_for_group
from agents.persona_runtime import _LLMPersonaAgent
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    GROUP_CHANNEL,
    make_agent_with_clock,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


SCOPE = scope_for_group(GROUP_CHANNEL)
CHANNEL = {"channel_type": "group", "id": GROUP_CHANNEL}


def _history_row(
    msg_id: str, sender: str, content: str, principal: str | None,
) -> dict:
    """One ``channelMessageResponse`` as catch-up receives it.

    ``principal=None`` omits the key entirely — a pre-v12 orchestrator.
    The Go DTO is deliberately not ``omitempty``, so on a v12
    orchestrator the key is always present, ``"local"`` included.
    """
    row: dict = {
        "id": msg_id,
        "channel_id": GROUP_CHANNEL,
        "sender_id": sender,
        "content": content,
        "mentions": [],
        "metadata": {"interaction_id": "wire-A"},
    }
    if principal is not None:
        row["principal_id"] = principal
    return row


async def _replay(agent: _LLMPersonaAgent, *rows: dict) -> None:
    """One catch-up pass: replay ``rows``, then sweep the scopes it opened.

    Through ``on_event`` rather than ``_store_event_episode`` on purpose
    — binding the seeded principal for the ingest is
    ``request_scope_from_metadata``'s job there, and a test that skipped
    it would pass while production attributed nothing.
    ``close_replayed_interactions`` is the pass-end sweep
    ``replay_for_persona_agents`` runs in its ``finally``.
    """
    for row in rows:
        await agent.on_event(build_replay_event(row, GROUP_CHANNEL, "all", CHANNEL))
    await agent.close_replayed_interactions()
    await agent.drain_pending_summaries()


async def _derived(agent: _LLMPersonaAgent) -> list[tuple]:
    """``(principal_id, speaker_id, summary)`` per episode — the triple the
    release's live MT reads, in the order the rows were written."""
    db = agent._episodic_memory._ensure_db()
    async with db.execute(
        "SELECT principal_id, speaker_id, summary FROM episodes "
        "WHERE agent_id = ? ORDER BY created_at",
        (agent.agent_id,),
    ) as cursor:
        return [tuple(r) for r in await cursor.fetchall()]


@pytest.mark.asyncio
class TestReplayDerivesUnderTheRowsTenant:
    async def test_attributed_replay_derives_in_its_own_tenant(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent,
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
        )
        assert await _derived(agent) == [
            ("alice-person", "alice", "Multi-turn session summary."),
        ], (
            "the replayed span must derive under the tenant the message row "
            "names — deriving it under `local` is the ISSUE-0130 leak, and "
            "not deriving it at all is the v0.3.14 cost shape (b) removes"
        )

    async def test_two_tenants_in_one_room_derive_separately(self):
        # The ISSUE-0123 re-key applied to the replayed write: one room,
        # one pass, two principals — two records, neither readable by the
        # other's tenant.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent,
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "bob", "noted", "bob-person"),
        )
        assert sorted(row[:2] for row in await _derived(agent)) == [
            ("alice-person", "alice"), ("bob-person", "bob"),
        ]

    async def test_unattributed_replay_still_derives_nothing(self):
        # The leak-stopper, on the only case that still reaches it: a
        # pre-v12 orchestrator, whose history JSON has no `principal_id`.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent, _history_row("m-1", "alice", "Mira turns seven", None),
        )
        assert await _derived(agent) == [], (
            "a row that cannot name its tenant must derive nothing — this "
            "is the v0.3.14 leak-stopper, narrowed but not withdrawn"
        )

    async def test_a_present_local_is_an_answer_and_derives(self):
        # The distinction the record key cannot make and the whole reason
        # `replay_attributed` exists: `"local"` PRESENT means "this
        # publish had no verified tenant" (an agent publish, or the whole
        # deployment under `auth.mode: disabled`), where `local` is the
        # correct attribution — the v0.3.14 cost this PR removes.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _replay(
            agent, _history_row("m-1", "ember-owl-peer", "status", "local"),
        )
        assert await _derived(agent) == [
            ("local", "ember-owl-peer", "Multi-turn session summary."),
        ]


@pytest.mark.asyncio
class TestReplayIsIdempotentAcrossPasses:
    async def test_the_same_window_replayed_twice_derives_once(self):
        """The release acceptance bar (v0.3.15 plan, scope lock 4)."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        window = (
            _history_row("m-1", "alice", "Mira turns seven", "alice-person"),
            _history_row("m-2", "alice", "she likes dinosaurs", "alice-person"),
        )
        await _replay(agent, *window)
        after_first = await _derived(agent)
        assert len(after_first) == 1, after_first

        # The second boot: catch-up has no watermark, so it re-reads the
        # same page. Same rows, same order, a fresh tracker.
        await _replay(agent, *window)
        assert await _derived(agent) == after_first, (
            "a span replayed twice must derive once — without this the "
            "narrowed skip hands back the unbounded re-derivation shape "
            "(a) bounded, relocated from `local` to the right tenant"
        )

    async def test_a_grown_window_derives_again(self):
        """The control: the guard bounds duplication, it does not stop
        replay from ever deriving again.  A window that gained a message
        while the agent was down is a different span, and its content has
        never been summarised."""
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        first = _history_row("m-1", "alice", "Mira turns seven", "alice-person")
        await _replay(agent, first)
        assert len(await _derived(agent)) == 1

        await _replay(
            agent, first,
            _history_row("m-2", "alice", "she likes dinosaurs", "alice-person"),
        )
        assert len(await _derived(agent)) == 2, (
            "a replayed window that GREW must derive again — skipping it "
            "would lose the memory of every message that arrived while "
            "the agent was down, which is the cost shape (b) buys back"
        )
