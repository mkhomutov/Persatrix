"""RFC 0037 §C (v0.3.12 PR 3) — the wire → capture → stamp seam, end to end.

The PR-3 unit suites pin the three halves in isolation: the tolerant
reader (:mod:`agents.channel_event_classification`), the frozen-at-open
capture on :class:`~agents.memory.interaction_types.Interaction` (driven
with direct keywords), and the two stamp sites' rule-(a) coercion.  What
none of them exercises is the single line of production wiring that joins
them — ``classification=wire_channel_classification(event)`` /
``source_channel_id=event.channel_id`` in
:meth:`agents.persona_runtime.episode_routing._EpisodeRoutingMixin
._handle_multi_turn_event`.

That is exactly the seam ``channel_event_classification``'s own module
docstring argues about ("per-consumer inline reads would sit outside any
drift pin and fail silent on a rename"): a metadata-key rename, a dropped
kwarg, or a capture read at close instead of at open all leave every
existing suite green while every episode and fact in production silently
labels ``internal``.  Under the PR 4 §D gate that is an under-classified
row injected into a lower-classified turn — the exact leak RFC 0037
exists to prevent — so it is pinned here from a real event, through the
real routing/close path, to the stored column.

Both stamped tiers are covered: the Phase-1 closing-row insert
(``close_path.py``) and the Phase-2 facts-extraction dispatch
(``fact_extractor.py``), the latter being the only coverage of
``dispatch_facts_from_response``'s pass-through of the interaction's
capture.

Stamped dark at PR 3; read live since the PR 4 §D gate armed.

The catch-up replay leg (v0.3.12 review item 8, landed at the PR 8
closeout) pins the same seam from the OTHER producer: events built by the
real ``channel_catchup._build_replay_event`` from a ``secret``-classified
channel-list object stamp their episode ``secret`` — the restart path must
label exactly like the live path, or every reboot silently downgrades a
sensitive channel's history to ``internal``.

Shared persona config / mock LLM client / clock-aware agent factory /
episode probe live in :mod:`_interaction_multi_turn_helpers`.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.channel_catchup import _build_replay_event
from agents.clock import FrozenClock
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.memory.interactions import scope_for_group
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.classification import DEFAULT_CLASSIFICATION
from agents.prompt_loader import load_snippet
from agents.tools.registry import clear_registry

from ._interaction_multi_turn_helpers import (
    GROUP_CHANNEL,
    all_episodes,
    channel_event,
    make_agent_with_clock,
    persona_config,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


SCOPE = scope_for_group(GROUP_CHANNEL)


async def _closed_episode(agent: _LLMPersonaAgent) -> dict:
    """The single persisted episode row, asserted to be the only one."""
    episodes = await all_episodes(agent)
    assert len(episodes) == 1, f"expected one closed episode, got {episodes}"
    return episodes[0]


async def _drive_and_rotate(
    agent: _LLMPersonaAgent, *turns: dict,
) -> None:
    """Deliver ``turns`` under ``wire-A``, then rotate the wire id.

    The rotation is the cheapest deterministic close on the channel path
    (the vote close needs a publish-outcome discharge, the idle close a
    clock advance): the ``wire-B`` event closes and persists the
    ``wire-A`` interaction before opening its successor.
    """
    for turn in turns:
        await agent._store_event_episode(
            channel_event(wire_id="wire-A", **turn), [],
        )
    await agent._store_event_episode(
        channel_event("new topic", wire_id="wire-B"), [],
    )


# ─── The episodic leg (Phase-1 closing row) ─────────────────


@pytest.mark.asyncio
class TestEpisodeStampedFromTheWire:
    async def test_classified_event_stamps_the_episode(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _drive_and_rotate(
            agent, {"content": "budget talk", "classification": "restricted"},
        )
        episode = await _closed_episode(agent)
        assert episode["scope"] == SCOPE
        assert episode["protection_level"] == "restricted"
        assert episode["source_channel_id"] == GROUP_CHANNEL

    async def test_capture_is_frozen_at_open(self):
        # §C "classification is read once per interaction": a later turn
        # arriving after an operator reclassifies the channel DOWNWARD
        # cannot relabel the open record — the row keeps the level its
        # first turn was received under.  The tracker-level pin
        # (test_protection_stamping) asserts the same rule on the record;
        # this asserts it survives the routing path into the column.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _drive_and_rotate(
            agent,
            {"content": "opening turn", "classification": "restricted"},
            {"content": "later turn", "classification": "public"},
        )
        episode = await _closed_episode(agent)
        assert episode["turn_count"] == 2
        assert episode["protection_level"] == "restricted"

    async def test_unclassified_producer_stamps_internal(self):
        # The pre-v0.3.12 / resolve-failed producer seeds no key at all.
        # Rule (a) at the stamp site: ``internal``, never ``public`` — the
        # acting-side ``public`` floor is a different rule with a
        # different direction, and picking it here would be a disclosure.
        # ``source_channel_id`` is still captured: it is provenance, not a
        # level, and has no fail-closed direction to get wrong.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _drive_and_rotate(agent, {"content": "legacy turn"})
        episode = await _closed_episode(agent)
        assert episode["protection_level"] == DEFAULT_CLASSIFICATION
        assert episode["source_channel_id"] == GROUP_CHANNEL

    async def test_garbage_on_the_wire_stamps_internal(self):
        # The seed is verbatim by design (no allowlist — §A rule (b)
        # belongs to ``acting_rank`` alone), so a hostile/broken producer's
        # value reaches the capture unchanged and the STAMP SITE owns the
        # unknown → ``internal`` coercion.  Pinned end to end because this
        # is the one path where a missing ``normalize_for_stamp`` call
        # would otherwise persist an off-lattice label that rule (c) then
        # withholds forever.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        await _drive_and_rotate(
            agent,
            {"content": "odd turn", "classification": "ultra-mega-secret"},
        )
        episode = await _closed_episode(agent)
        assert episode["protection_level"] == DEFAULT_CLASSIFICATION
        # The provenance half must still land: without it this case is
        # indistinguishable from "the capture was never wired at all",
        # since both stamp ``internal``.
        assert episode["source_channel_id"] == GROUP_CHANNEL


# ─── The catch-up replay leg (v0.3.12 review item 8) ────────


def _replay_event(
    msg: dict, *, channel: dict, channel_id: str = GROUP_CHANNEL,
) -> object:
    """A catch-up event built by the REAL builder, not a hand-shaped
    fixture: ``_build_replay_event`` is where the REST channel-list
    object's ``classification`` is seeded onto the event, so a fixture
    that wrote the metadata key itself would keep this suite green while
    the boot path stamped nothing."""
    return _build_replay_event(msg, channel_id, "all", channel)


@pytest.mark.asyncio
class TestCatchupReplayPersistsNothing:
    """ISSUE-0130 (v0.3.14): a replayed span persists **no** episode.

    These previously pinned RFC 0037 review item 8 — that a replayed
    rotation close stamped its episode with the channel's classification,
    the restart-path twin of the live-wire capture above.  That path is
    gone: a replayed turn carries no principal (the orchestrator's
    ``messages`` table has no principal column, so ``_build_replay_event``
    has nothing to seed), and deriving from it wrote one authenticated
    person's content into the shared ``local`` tenant — found live at the
    v0.3.14 ``MT-MEMORY-MULTIUSER-001`` run.  The close path now skips
    derivation for replayed spans entirely, so there is no row left to
    stamp.

    The *live* classification capture is unaffected and stays covered by
    the tests above — only the restart path is withdrawn.  v0.3.15
    persists the principal on the message row and seeds it here, at which
    point replayed spans become attributable and stamping returns.
    """

    async def test_replayed_secret_episodes_stamp_secret(self):
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        secret_channel = {"channel_type": "group", "classification": "secret"}
        await agent._store_event_episode(
            _replay_event(
                {
                    "id": "m-1", "sender_id": "alex",
                    "content": "the codename is zephyr",
                    "metadata": {"interaction_id": "wire-A"},
                },
                channel=secret_channel,
            ),
            [],
        )
        # A second replayed row on a rotated wire id closes wire-A — the
        # replayed-rotation close the catch-up docstring promises runs at
        # boot ("those conversations genuinely closed").
        await agent._store_event_episode(
            _replay_event(
                {
                    "id": "m-2", "sender_id": "alex",
                    "content": "new topic",
                    "metadata": {"interaction_id": "wire-B"},
                },
                channel=secret_channel,
            ),
            [],
        )
        assert await all_episodes(agent) == [], (
            "a replayed rotation close must persist nothing — the span has "
            "no principal, so any row it wrote would land in the shared "
            "`local` tenant (ISSUE-0130)"
        )

    async def test_pre_v0312_orchestrator_replays_persist_nothing(self):
        # A pre-v0.3.12 orchestrator's channel-list JSON has no
        # ``classification`` key.  Pre-ISSUE-0130 this exercised the stamp
        # site's rule-(a) default (``internal``); now it pins that the
        # replay skip fires regardless of what the builder seeded.
        agent = await make_agent_with_clock(FrozenClock(at=1_000.0))
        legacy_channel = {"channel_type": "group"}
        for msg_id, wire, content in (
            ("m-1", "wire-A", "legacy history"),
            ("m-2", "wire-B", "new topic"),
        ):
            await agent._store_event_episode(
                _replay_event(
                    {
                        "id": msg_id, "sender_id": "alex",
                        "content": content,
                        "metadata": {"interaction_id": wire},
                    },
                    channel=legacy_channel,
                ),
                [],
            )
        assert await all_episodes(agent) == [], (
            "the legacy-orchestrator replay path persists nothing either — "
            "the skip is on the replay marker, not on classification"
        )


# ─── The facts leg (Phase-2 extraction dispatch) ────────────


def _fact_extracting_client() -> LLMClient:
    """``do_nothing_client``'s twin whose close-path summariser returns a
    well-formed envelope carrying one allowlisted fact tuple, so the
    Phase-2 dispatch actually writes a ``facts`` row to stamp."""
    mock_provider = AsyncMock()
    summarizer_system = load_snippet("episode-summarizer")

    async def _route(*, model, messages, system, tools, max_tokens, temperature):
        if system == summarizer_system:
            return LLMResponse(
                text=json.dumps({
                    "summary": "Budget discussion.",
                    "facts": [{
                        "subject": "alex",
                        "predicate": "works_at",
                        "object": "the-lab",
                    }],
                }),
                stop_reason=StopReason.END_TURN,
                usage=Usage(120, 30),
            )
        return LLMResponse(
            text='```json\n[{"action_type": "do_nothing", "payload": {}}]\n```',
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 5),
        )

    mock_provider.create_message = AsyncMock(side_effect=_route)
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: msgs,
    )
    return LLMClient(mock_provider)


async def _agent_extracting_facts() -> _LLMPersonaAgent:
    cfg = persona_config(agent_id="classification-capture-persona")
    agent = create_persona_agent(
        agent_id=cfg["id"], config=cfg,
        llm_client=_fact_extracting_client(), clock=FrozenClock(at=1_000.0),
    )
    await agent.initialize_memory()
    return agent


async def _stamped_facts(agent: _LLMPersonaAgent) -> list[tuple]:
    assert agent._fact_store is not None
    async with agent._fact_store._ensure_db().execute(
        "SELECT subject, protection_level, source_channel_id FROM facts "
        "WHERE agent_id = ?",
        (agent.agent_id,),
    ) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


@pytest.mark.asyncio
class TestFactsStampedFromTheWire:
    async def test_extracted_fact_inherits_the_capture(self):
        # §C: "a fact is extracted from one interaction; its
        # protection_level is likewise the interaction's classification".
        # The extraction runs in the Phase-2 background task, long after
        # the event's context is gone — so this pins that the level
        # travels on the interaction record rather than being re-derived.
        agent = await _agent_extracting_facts()
        try:
            await _drive_and_rotate(
                agent,
                {"content": "alex works at the-lab", "classification": "restricted"},
            )
            await agent.drain_pending_summaries()
            assert await _stamped_facts(agent) == [
                ("alex", "restricted", GROUP_CHANNEL),
            ]
        finally:
            await agent.close_memory()

    async def test_unclassified_producer_stamps_facts_internal(self):
        # "There is no path that writes a fact without a protection
        # level" — the unconditional-stamp half of §C, through the real
        # dispatch rather than a direct ``store_extracted_facts`` call.
        agent = await _agent_extracting_facts()
        try:
            await _drive_and_rotate(agent, {"content": "alex works at the-lab"})
            await agent.drain_pending_summaries()
            assert await _stamped_facts(agent) == [
                ("alex", DEFAULT_CLASSIFICATION, GROUP_CHANNEL),
            ]
        finally:
            await agent.close_memory()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
