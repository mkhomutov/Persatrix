"""CP acceptance — the end-vote close reaches the agent-local tracker.

RFC 0030 end-vote-close-propagation amendment (§E): these tests pin the
receiver half of the contract at the seams the existing close paths
already use — the servicer wire lift (the OQ 5 / ``chair_escalation``
posture), the response gate (the canonical no-turn enforcement point),
and the agent-side close dispatch (the ``cost_close`` / ``vote_close``
pattern). Nothing may seed the marker for an event that does not carry
the typed field — the typed-field-only posture
(``floor_mentions_resolved``; OQ 5's "anything else seeds nothing").

The contract (amendment CP3): a dispatch marked
``interaction_close_notification`` is control, never stimulus — the
gate refuses it pre-LLM (no turn, no Tier B bid, no LLM call), and the
close dispatch closes the channel scope's open interaction immediately
with the established ``end_votes`` mapping (:data:`REASON_STRUCTURAL`,
the "ended" render) instead of burying it as "went idle" an idle window
later. Honoured strictly: a truthy non-bool impostor takes no close
path on either seam.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import grpc

from agents.memory.boundary_detectors import (
    REASON_IDLE_GAP,
    REASON_STRUCTURAL,
)
from agents.memory.interactions import Interaction, InteractionTracker
from agents.persona_types import AgentAction, AgentEvent, EventType
from agents.response_gate import (
    POLICY_ALWAYS,
    POLICY_NEVER,
    POLICY_UNKNOWN,
    evaluate_response_gate,
)

from ._receive_channel_message_helpers import (
    channel_event,
    enqueued_event,
    make_servicer,
)


def _notification_event(
    *,
    marker: object = True,
    respond_policy: str = "always",
    mentions: list[str] | None = None,
    channel_id: str = "group:planning",
    channel_type: str = "group",
) -> AgentEvent:
    """The closing vote as the agent runtime sees it post-lift — the
    ``_escalation_event`` builder shape, marker on the payload port."""
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "Agreed — relay. Nothing further.",
            "channel_type": channel_type,
            "mentions": mentions or [],
            "respond_policy": respond_policy,
            "thread_parent_sender_id": "",
            "interaction_close_notification": marker,
        },
        channel_id=channel_id,
        sender_id="iron-fox",
    )


class _CloseNotificationAgent:
    """The ``_CostCloseAgent`` harness: a real tracker, a persistence spy,
    the scope surface the close dispatch resolves through, and (since the
    PR #614 review fix) the ingest seam the dispatch drives — modelled as
    the ``add_turn`` it amounts to, so the final-turn append is real."""

    _MULTI_TURN_EVENT_TYPES: frozenset[EventType] = frozenset(
        {EventType.CHANNEL_MESSAGE},
    )

    def __init__(
        self, tracker: InteractionTracker, agent_id: str = "ember-owl",
    ) -> None:
        # agent_id defaults NON-sender so the fixtures (sender "iron-fox")
        # ingest as inbound; set it to the sender id for the self-echo.
        self.agent_id = agent_id
        self._interaction_tracker = tracker
        self.persisted: list[Interaction] = []
        self.ingested: list[AgentEvent] = []

    def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None:
        return event.channel_id

    async def _persist_closed_interaction(self, interaction: Interaction) -> None:
        self.persisted.append(interaction)

    async def _store_event_episode(
        self, event: AgentEvent, actions: list[AgentAction],
    ) -> None:
        self.ingested.append(event)
        if event.channel_id is not None:
            self._interaction_tracker.add_turn(event.channel_id)


class TestCloseNotificationWireLift:
    """The servicer lift: payload carries the marker from the typed proto
    field only (the ``chair_escalation`` lift posture)."""

    async def test_marked_field_lifts_into_event_payload(self):
        """The typed field rides into the enqueued event's payload —
        the port the response gate reads."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(
                interaction_id="int-A",
                interaction_close_notification=True,
            ),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert event.payload.get("interaction_close_notification") is True

    async def test_unmarked_event_does_not_carry_the_key(self):
        """Typed-field-only, the negative half: an ordinary event must
        never grow the marker (e.g. from an over-broad metadata copy).
        Green today by construction; stays as the regression pin once
        PR 3 wires the lift."""
        servicer, dispatcher = make_servicer()
        await servicer.ReceiveChannelMessage(
            channel_event(interaction_id="int-A"),
            MagicMock(spec=grpc.aio.ServicerContext),
        )
        event = enqueued_event(dispatcher)
        assert "interaction_close_notification" not in event.payload
        assert "interaction_close_notification" not in event.metadata


class TestCloseNotificationProducesNoTurn:
    """CP3, the stimulus half, pinned behaviorally at the canonical
    enforcement point: the response gate refuses a marked event pre-LLM
    (no turn, no Tier B bid, no LLM call), and the action loop's
    ingest-on-suppress gives CP3's window append for free."""

    def test_marked_event_is_refused_for_always_policy(self):
        decision = evaluate_response_gate(
            _notification_event(), agent_id="ember-owl",
        )
        assert decision.respond is False
        assert decision.reason == "close_notification"

    def test_marked_event_is_refused_even_when_mentioned(self):
        """Suppression outranks the directed lane: the closing vote may
        well @-mention the room; a notification is still not an
        invitation to speak."""
        decision = evaluate_response_gate(
            _notification_event(mentions=["ember-owl"]), agent_id="ember-owl",
        )
        assert decision.respond is False
        assert decision.reason == "close_notification"

    def test_marker_is_strictly_boolean(self):
        """The ``floor_mentions_resolved`` posture: a truthy non-bool on
        the cleartext port does not take the notification branch — the
        event falls through to the ordinary policy branches."""
        decision = evaluate_response_gate(
            _notification_event(marker="true"), agent_id="ember-owl",
        )
        assert decision.reason != "close_notification"


class TestCloseNotificationGateOrdering:
    """PR #614 review findings 1+2: the marked refusal must outrank EVERY
    admitting lane — the DM always-override included — and the decision's
    ``policy`` may only ever carry a bounded label onto the
    ``channel.messages.gated`` counter. RFC 0052 bounded-close fix (PR
    4b-i deep review): on a GROUP channel the marker now outranks
    self_sender too — the bounded close fans to the round-triggering
    sender (``excludeSender=false``), whose tracker no vote closed, so its
    self-addressed notification must close the scope, not be dismissed as
    a self-echo. (DM keeps ``dm_self_sender`` first: no close cause fans a
    self-echo to a DM, so that ordering is never exercised.)"""

    def test_marked_dm_event_is_refused(self):
        """The DM override admits every ordinary message ("a DM with no
        reply is broken by definition") — but a close notification is
        control, not a message awaiting reply. DMs are interaction-tracked
        orchestrator-side, so the lane is reachable; labelled POLICY_ALWAYS
        (bounded)."""
        decision = evaluate_response_gate(
            _notification_event(channel_id="dm:ember-owl:iron-fox",
                                channel_type="dm"),
            agent_id="ember-owl",
        )
        assert decision.respond is False
        assert decision.reason == "close_notification"
        assert decision.policy == POLICY_ALWAYS

    def test_dm_impostor_marker_keeps_the_dm_admit(self):
        """Strict ``is True`` on the DM branch too — the group branch's
        ``test_marker_is_strictly_boolean`` twin: a truthy non-bool marker
        is no notification, so the event keeps the ordinary DM always-admit
        (the strict-bool rule is checked inline per consumer)."""
        decision = evaluate_response_gate(
            _notification_event(marker="true",
                                channel_id="dm:ember-owl:iron-fox",
                                channel_type="dm"),
            agent_id="ember-owl",
        )
        assert decision.respond is True
        assert decision.reason == "dm"

    def test_marked_event_for_never_policy_takes_the_dedicated_reason(self):
        """Refused for EVERY policy means `never` too: the orchestrator
        excludes RespondNever members from the fan by contract, but if
        one arrives anyway the dedicated reason (and the close dispatch
        keyed on it) beats the `policy_never` routing-regression warn —
        closing a stale local record truthfully is strictly better than
        warning and letting it idle out."""
        decision = evaluate_response_gate(
            _notification_event(respond_policy="never"), agent_id="ember-owl",
        )
        assert decision.respond is False
        assert decision.reason == "close_notification"
        assert decision.policy == POLICY_NEVER

    def test_marked_event_policy_label_is_bounded(self):
        """A marked event with a garbage wire policy must NOT echo the
        raw (attacker- or bug-supplied) string into the decision — the
        POLICY_UNKNOWN bounded-label discipline, same as the fail-closed
        unknown-policy branch the marked check sits ahead of."""
        decision = evaluate_response_gate(
            _notification_event(respond_policy="zz-spoofed-9f3a"),
            agent_id="ember-owl",
        )
        assert decision.respond is False
        assert decision.reason == "close_notification"
        assert decision.policy == POLICY_UNKNOWN

    def test_marked_event_empty_policy_label_is_bounded(self):
        """The `""` sentinel is documented as never reaching the gated
        counter (it only rides respond=True pass-throughs) — a marked
        event with a missing policy must keep that true."""
        decision = evaluate_response_gate(
            _notification_event(respond_policy=""), agent_id="ember-owl",
        )
        assert decision.respond is False
        assert decision.policy == POLICY_UNKNOWN

    def test_marked_group_self_sender_closes_the_senders_scope(self):
        """RFC 0052 bounded-close regression (PR 4b-i deep review): on a
        GROUP channel a marked SELF-echo now takes the close lane, NOT the
        self_sender refusal — the bounded close fans to the round-triggering
        sender too and nothing else closed its tracker, so its own
        re-dispatched message must close its scope and author the RFC 0020
        summary. Before the fix self_sender swallowed it (no summary)."""
        decision = evaluate_response_gate(
            _notification_event(), agent_id="iron-fox",
        )
        assert decision.respond is False
        assert decision.reason == "close_notification"


class TestCloseNotificationClosesTracker:
    """CP3, the control half: the close dispatch (the planned
    ``agents.persona_runtime.close_notification``, the ``cost_close`` /
    ``vote_close`` sibling) closes the channel scope's open interaction
    at notification time and persists the record."""

    async def test_marked_event_closes_scope_with_structural_cause(self):
        """Closed immediately with the established ``end_votes`` mapping
        — :data:`REASON_STRUCTURAL`, rendering "ended" — not an
        idle-window later, not "went idle". The open turn is seeded LIVE
        (PR #614 review finding 3 follow-up): the dispatch now runs the
        same staleness pass every ingest runs, so an epoch-stale seed
        would model an interaction the idle rule already owns, not the
        live one this test always meant."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert tracker.get("group:planning") is None, (
            "the notification closed the scope immediately"
        )
        assert len(agent.persisted) == 1
        assert agent.persisted[0].close_reason == REASON_STRUCTURAL
        assert agent.persisted[0].turn_count == 2, (
            "the closing vote ingested as the closed record's final turn"
        )

    async def test_self_echo_closes_scope_without_ingesting_the_turn(self):
        """RFC 0052 bounded-close regression: the bounded close fans to the
        round-triggering sender too (``excludeSender=false``), so the
        convener/chair gets its OWN message back (fresh wire id → dedup
        misses it). It must still CLOSE the sender's scope but must NOT
        ingest the echo — that would write a ``sender == agent_id`` turn
        and inflate ``turn_count``, the self-echo the gate keeps out of
        memory. Contrast the inbound case above, which DOES ingest."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time())
        agent = _CloseNotificationAgent(tracker, agent_id="iron-fox")  # == sender

        await close_interaction_on_notification(agent, _notification_event())

        assert tracker.get("group:planning") is None, "self-echo still closes scope"
        assert agent.ingested == [], "the sender's own echo is NOT ingested"
        assert len(agent.persisted) == 1
        assert agent.persisted[0].close_reason == REASON_STRUCTURAL
        assert agent.persisted[0].turn_count == 1, "turn_count not inflated by the echo"

    async def test_impostor_marker_closes_nothing(self):
        """Defence-in-depth (CP3): a truthy non-bool marker must not
        fabricate a close — burying an active discussion is exactly the
        failure mode the strict-bool rule exists to block."""
        from agents.persona_runtime.close_notification import (  # type: ignore[import-not-found]
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=100.0)
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(
            agent, _notification_event(marker="true"),
        )

        assert tracker.get("group:planning") is not None, (
            "a non-bool marker is ignored — the scope stays open"
        )
        assert agent.persisted == []

    async def test_no_open_interaction_is_noop(self):
        """A notification for an already-idle scope degrades quietly —
        the ``InteractionTracker.close`` unknown-scope contract, the
        ``cost_close`` no-op posture."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        agent = _CloseNotificationAgent(InteractionTracker())

        await close_interaction_on_notification(agent, _notification_event())

        assert agent.persisted == []

    async def test_already_idle_scope_ingests_nothing(self):
        """PR #614 review finding 3: the no-op above must hold through the
        INGEST half too. Ingesting first would ``add_turn`` into a
        freshly-opened interaction and the close would persist a fabricated
        1-turn "ended" record the no-op contract promises never to invent.
        So the dispatch checks the open scope first, ingests only when
        there is one to land the final turn in."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert agent.ingested == [], (
            "no open interaction — nothing to land the final turn in"
        )
        assert tracker.get("group:planning") is None, (
            "the dispatch must not open a scope just to close it"
        )
        assert agent.persisted == []

    async def test_expired_open_scope_flushes_by_the_idle_rule(self):
        """PR #614 review finding 3, the stale-open half: an interaction
        whose idle window expired before the notification landed belongs
        to the idle rule, not the late signal — the dispatch runs the same
        staleness pass every ingest runs, sees nothing open, and stops.
        Conservative by design; no structural successor is fabricated."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=time.time() - 100_000)
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert [i.close_reason for i in agent.persisted] == [REASON_IDLE_GAP], (
            "the expired window closes by the agent's own idle rule only"
        )
        assert agent.ingested == []
        assert tracker.get("group:planning") is None

    async def test_payloadless_event_is_a_noop_not_a_crash(self):
        """The docstring invites future callers off looser signals — a
        ``None`` payload must read as unmarked (no-op), not raise."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        agent = _CloseNotificationAgent(InteractionTracker())
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload=None,  # type: ignore[arg-type]
            channel_id="group:planning",
            sender_id="iron-fox",
        )

        await close_interaction_on_notification(agent, event)

        assert agent.persisted == []
