"""RFC 0052 PR 5 — Phase-1 acceptance, the agent (Python) half.

The Go integration suite (``internal/channels/autonomous_acceptance_test.go``)
proves the ORCHESTRATOR arc against a real wallet: convene → bounded discussion
→ soft-budget/structural close → chair synthesis turn → close-notification fan,
with every ``1 + N`` close-path lease honoured and spend ≤ the mandatory cap.
This file proves the matching PER-PERSONA close-artifact contract on the
receiving side — the piece MT-AUTONOMOUS-001 reads out of each persona's memory
(``docs/manual-tests/MT-AUTONOMOUS-001.md``) — chained end to end across a
≥2-persona roster on BOTH bounded-close causes:

  close notification → truthful close reason (``cost`` / ``structural``) →
  the synthesis ingested as the record's final turn (§D artifact #1) →
  the OQ #6 metering mark → a leased summariser call billing the SHARED
  governance interaction id (the ``N`` of ``1 + N``) → a REAL RFC 0020
  summary, never the ``[interaction summary unavailable]`` placeholder
  (§D artifact #2).

The unit matrices pin each seam alone (``test_interaction_close_notification``,
``test_summarize_close_metering``); what this suite adds is the CHAIN — that
the record the notification closes is the same record the metering mark rides
and the same record the summariser leases and summarises — for every persona
shape the close fans to: the ordinary member, the floor-path member whose
redelivered stimulus must not double-ingest, and the round-triggering
sender's self-echo.
"""

from __future__ import annotations

from agents.channel_wire_metadata import (
    WIRE_CLOSE_TRIGGER_COST,
    WIRE_CLOSE_TRIGGER_STRUCTURAL,
)
from agents.generated import wallet_pb2 as walletpb
from agents.memory.boundary_detectors import REASON_COST, REASON_STRUCTURAL
from agents.memory.interactions import SUMMARY_UNAVAILABLE_TEXT, InteractionTracker
from agents.persona_runtime.close_notification import (
    close_interaction_on_notification,
)
from agents.persona_runtime.summarize_close import summarize_closed_interaction
from agents.persona_types import AgentEvent

from .test_close_notification_redelivery import _notification_event
from .test_interaction_close_notification import _CloseNotificationAgent
from .test_summarize_close_metering import _SpyClient

_SCOPE = "group:brainstorm"
_WIRE_ID = "wire-ix-accept"
_SYNTHESIS = "Synthesis: adopt the monorepo; revisit tooling next quarter."


class _PersonaAgent(_CloseNotificationAgent):
    """The ``_CloseNotificationAgent`` harness on a 600 s idle window.

    PR #846 review: this used to override ``_store_event_episode`` to
    append a production-shaped turn, but nothing drove it — the fixtures
    seed turns with ``add_turn`` directly, and the close dispatch stopped
    calling that seam when it moved to the direct per-record append.  The
    override was dead scaffolding and is gone; §D artifact #1 (the
    synthesis landing in the persona's record) is asserted on the record
    itself, which is where these tests already looked."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(InteractionTracker(idle_timeout_sec=600.0), agent_id)


def _persona_mid_discussion(agent_id: str) -> _PersonaAgent:
    """A persona two turns into the discussion, its open record stamped
    with the governance wire id (the RFC 0030 producer's id every reply
    echoed)."""
    agent = _PersonaAgent(agent_id)
    tracker = agent._interaction_tracker
    tracker.add_turn(_SCOPE, {"sender": "nova-sparrow", "summary": "opening"})
    record = tracker.add_turn(
        _SCOPE, {"sender": "ember-owl", "summary": "a contribution"},
    )
    record.wire_interaction_id = _WIRE_ID
    return agent


def _bounded_close_notification(
    *, trigger: str, sender: str = "iron-fox", redelivery: bool = False,
) -> AgentEvent:
    """The bounded close's notification as the runtime sees it post-lift:
    the redelivery suite's builder shape, carrying the synthesis as the
    content and the closed interaction's wire id on the metadata port
    (CP2)."""
    event = _notification_event(
        redelivery=redelivery, close_trigger=trigger,
        sender_id=sender, channel_id=_SCOPE,
    )
    event.payload["content"] = _SYNTHESIS
    event.metadata["interaction_id"] = _WIRE_ID
    return event


async def _summarize(agent: _PersonaAgent) -> tuple[str, dict[str, object]]:
    """Run the RFC 0020 summariser over the persona's closed record and
    return (summary, the create_message kwargs)."""
    assert len(agent.persisted) == 1, "exactly one record persisted on close"
    client = _SpyClient()
    summary, failed, _facts, _projections = await summarize_closed_interaction(
        client,  # type: ignore[arg-type]  # a duck-typed LLM double
        agent.agent_id, agent.persisted[0],
    )
    assert failed is False
    assert len(client.calls) == 1
    return summary, client.calls[0]


class TestPhase1CloseArtifactContract:
    """MT-AUTONOMOUS-001's per-persona readout: both §D artifacts, for
    every persona, on both bounded-close causes."""

    async def test_cost_close_yields_both_artifacts_for_every_persona(self):
        """The close-by-budget leg's agent half, on a ≥2-persona roster:
        each persona ingests the synthesis as its final turn, closes with
        the truthful cost reason, and leases ONE metered summary billing
        the SHARED governance id — the ``N`` of the PR 4a ``1 + N`` reserve
        the Go leg proves is funded."""
        roster = ["ember-owl", "nova-sparrow"]
        billed_ids: list[object] = []
        for agent_id in roster:
            agent = _persona_mid_discussion(agent_id)
            await close_interaction_on_notification(
                agent, _bounded_close_notification(trigger=WIRE_CLOSE_TRIGGER_COST),
            )

            closed = agent.persisted[0]
            assert closed.close_reason == REASON_COST, (
                "the wallet soft-budget close IS a cost close"
            )
            assert closed.turns[-1].payload.get("text") == _SYNTHESIS, (
                "§D artifact #1: the synthesis lands as the record's final "
                "turn under the production body key (ISSUE-0054 ``text``)"
            )
            assert closed.meter_close_summary is True

            summary, call = await _summarize(agent)
            assert summary != SUMMARY_UNAVAILABLE_TEXT, (
                "§D artifact #2: a real summary, not the placeholder"
            )
            assert call.get("cause") == walletpb.CAUSE_CHANNEL_MESSAGE
            assert call.get("agent_id") == agent_id
            billed_ids.append(call.get("interaction_id"))

        assert billed_ids == [_WIRE_ID] * len(roster), (
            "every persona's summary lease bills the ONE shared governance id "
            "— the attribution the roster-scaled reserve was carved against"
        )

    async def test_structural_close_carries_the_structural_reason(self):
        """The max_rounds cause: same chain, REASON_STRUCTURAL, still
        metered (a bounded close is autonomous by construction)."""
        agent = _persona_mid_discussion("ember-owl")
        await close_interaction_on_notification(
            agent,
            _bounded_close_notification(trigger=WIRE_CLOSE_TRIGGER_STRUCTURAL),
        )

        closed = agent.persisted[0]
        assert closed.close_reason == REASON_STRUCTURAL
        assert closed.meter_close_summary is True
        summary, call = await _summarize(agent)
        assert summary != SUMMARY_UNAVAILABLE_TEXT
        assert call.get("interaction_id") == _WIRE_ID

    async def test_floor_path_redelivery_closes_without_double_ingest(self):
        """The PR 4b-ii redelivery acceptance: a floor-path bounded close
        redelivers a stimulus the member already ingested live, so the
        notification closes the record WITHOUT appending a duplicate final
        turn — and the metering + real-summary contract still holds."""
        agent = _persona_mid_discussion("ember-owl")
        before = agent._interaction_tracker.get(_SCOPE).turn_count

        await close_interaction_on_notification(
            agent,
            _bounded_close_notification(
                trigger=WIRE_CLOSE_TRIGGER_STRUCTURAL, redelivery=True,
            ),
        )

        closed = agent.persisted[0]
        assert closed.turn_count == before, (
            "a redelivered bounding stimulus must not duplicate the final turn"
        )
        assert closed.close_reason == REASON_STRUCTURAL
        assert closed.meter_close_summary is True
        summary, _call = await _summarize(agent)
        assert summary != SUMMARY_UNAVAILABLE_TEXT

    async def test_self_echo_closes_the_sender_without_an_echo_turn(self):
        """The round-triggering sender (convener/chair) receives its own
        closing message back: the record closes — so the sender authors its
        summary like every other persona — but the self-echo never lands as
        a turn."""
        agent = _persona_mid_discussion("iron-fox")  # == the notification sender
        before = agent._interaction_tracker.get(_SCOPE).turn_count

        await close_interaction_on_notification(
            agent,
            _bounded_close_notification(
                trigger=WIRE_CLOSE_TRIGGER_COST, sender="iron-fox",
            ),
        )

        closed = agent.persisted[0]
        assert closed.turn_count == before, "no self-echo turn"
        assert closed.close_reason == REASON_COST
        assert closed.meter_close_summary is True
        summary, call = await _summarize(agent)
        assert summary != SUMMARY_UNAVAILABLE_TEXT
        assert call.get("interaction_id") == _WIRE_ID

    async def test_human_close_stays_unmetered_and_unleased(self):
        """The OQ #2/OQ #6 scope regression at the acceptance level: an
        UNMARKED close (the human-channel end-vote shape — no bounded
        trigger) closes with the established structural mapping, meters
        nothing, and the summariser call carries no lease kwargs at all."""
        agent = _persona_mid_discussion("ember-owl")
        event = _bounded_close_notification(trigger=WIRE_CLOSE_TRIGGER_COST)
        del event.payload["close_notification_close_trigger"]
        del event.payload["close_notification_redelivery"]

        await close_interaction_on_notification(agent, event)

        closed = agent.persisted[0]
        assert closed.close_reason == REASON_STRUCTURAL
        assert closed.meter_close_summary is False
        summary, call = await _summarize(agent)
        assert summary != SUMMARY_UNAVAILABLE_TEXT
        assert "cause" not in call
        assert "interaction_id" not in call
        assert "agent_id" not in call
