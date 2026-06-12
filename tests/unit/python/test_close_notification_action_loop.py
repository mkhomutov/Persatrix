"""Action-loop wiring for the end-vote close notification (CP3).

The committed acceptance (:mod:`test_interaction_close_notification`)
pins each seam in isolation — the wire lift, the gate's refusal, the
close dispatch. This file pins the COMPOSITION through the real agent's
``on_event``: a marked event must ride the gate-suppress path into
:func:`agents.persona_runtime.close_notification.close_interaction_on_notification`
— silence out, no turn/bid LLM call, the open channel scope closed with
the structural ("ended") cause, and the closing message still ingested
as the closed record's final turn (ingest-on-suppress, the window half
of CP3). A regression that unwires the loop hook leaves every isolated
seam green while the product behaviour silently reverts to the "went
idle" burial — exactly the gap this file exists to close.

The persist seam is stubbed with a recording spy throughout: the real
``_persist_closed_interaction`` runs the RFC 0020 close-path summariser
(one legitimate LLM call — the amendment's "summary generated now"
promise), so stubbing it makes "the gate refused pre-LLM" assertable as
a clean ``create_message.assert_not_called()`` without racing or
counting the summary call.
"""

from __future__ import annotations

import time

from agents.memory.boundary_detectors import REASON_IDLE_GAP, REASON_STRUCTURAL
from agents.memory.interactions import Interaction
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

_SCOPE = "group:planning"


async def _make_agent_with_persist_spy() -> tuple[_LLMPersonaAgent, list[Interaction]]:
    cfg = {**_PERSONA_CONFIG}
    agent = create_persona_agent(
        agent_id=cfg["id"],
        config=cfg,
        llm_client=_make_client(),
    )
    await agent.initialize_memory()
    persisted: list[Interaction] = []

    async def _spy(interaction: Interaction) -> None:
        persisted.append(interaction)

    agent._persist_closed_interaction = _spy  # type: ignore[method-assign]
    return agent, persisted


def _notification_event(*, marker: object = True) -> AgentEvent:
    """The closing vote as the runtime sees it post-lift — marked on the
    payload port, ``always`` policy so an unmarked sibling would have
    drawn a turn (the strongest wiring probe)."""
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "Agreed — relay. Nothing further.",
            "channel_type": "group",
            "mentions": [],
            "respond_policy": "always",
            "thread_parent_sender_id": "",
            "interaction_close_notification": marker,
        },
        channel_id=_SCOPE,
        sender_id="iron-fox",
    )


class TestCloseNotificationActionLoopWiring:
    async def test_marked_event_closes_scope_silently_with_no_llm(self):
        """The full CP3 receiver arc in one pass: DO_NOTHING out, zero
        turn/bid LLM calls, the open scope closed through the loop's
        suppress path."""
        agent, persisted = await _make_agent_with_persist_spy()
        agent._interaction_tracker.add_turn(_SCOPE, now=time.time())

        actions = await agent.on_event(_notification_event())

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING
        agent._llm_client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]
        assert agent._interaction_tracker.get(_SCOPE) is None, (
            "the notification closed the scope through the loop's suppress path"
        )
        assert len(persisted) == 1

    async def test_close_reason_is_structural_and_turn_ingested(self):
        """The persisted record carries the established ``end_votes``
        mapping — REASON_STRUCTURAL, the "ended" render, not idle_gap —
        and the closing vote ingested as its FINAL turn (the loop's
        documented ingest-before-close ordering; closing first would
        strand the message in a successor interaction)."""
        agent, persisted = await _make_agent_with_persist_spy()
        agent._interaction_tracker.add_turn(_SCOPE, now=time.time())

        await agent.on_event(_notification_event())

        assert len(persisted) == 1
        assert persisted[0].close_reason == REASON_STRUCTURAL
        assert persisted[0].turn_count == 2, (
            "the closing vote is the closed record's final turn, "
            "after the opening turn it arrived behind"
        )

    async def test_already_idle_scope_invents_no_record(self):
        """PR #614 review finding 3, through the real loop: a
        notification landing AFTER the scope already idled out (restart,
        janitor flush, slow delivery) is a true no-op — the orchestrator's
        "ended" record stands; the agent fabricates nothing. The pre-fix
        composition ingested first, which ``add_turn``-opened a fresh
        interaction holding only the re-delivered closing vote, then
        closed it structurally — a spurious 1-turn "ended" record plus a
        summariser LLM call, duplicating the record that already closed."""
        agent, persisted = await _make_agent_with_persist_spy()
        assert agent._interaction_tracker.get(_SCOPE) is None

        actions = await agent.on_event(_notification_event())

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING
        assert persisted == [], "no record invented for an already-closed scope"
        assert agent._interaction_tracker.get(_SCOPE) is None, (
            "the notification must not re-open the scope"
        )
        agent._llm_client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]

    async def test_expired_scope_flushes_idle_with_no_structural_successor(self):
        """The stale-open half of the same finding: an interaction whose
        idle window expired before the notification arrived closes by the
        agent's own idle rule (the staleness pass every ingest runs), and
        the notification then finds nothing open — one idle record, no
        fabricated structural successor riding behind it."""
        agent, persisted = await _make_agent_with_persist_spy()
        agent._interaction_tracker.add_turn(_SCOPE, now=time.time() - 100_000)

        await agent.on_event(_notification_event())

        assert [i.close_reason for i in persisted] == [REASON_IDLE_GAP], (
            "exactly the idle flush — no structural record fabricated after it"
        )
        assert agent._interaction_tracker.get(_SCOPE) is None

    async def test_impostor_marker_takes_the_ordinary_path(self):
        """A truthy non-bool marker is no notification (strict-bool on
        both seams): the event falls through to the ordinary `always`
        admit — the scope stays open and no close is fabricated."""
        agent, persisted = await _make_agent_with_persist_spy()
        agent._interaction_tracker.add_turn(_SCOPE, now=time.time())

        await agent.on_event(_notification_event(marker="true"))

        assert agent._interaction_tracker.get(_SCOPE) is not None, (
            "an impostor marker must not close the scope"
        )
        assert persisted == []
