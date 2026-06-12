"""CP acceptance — the end-vote close reaches the agent-local tracker.

RFC 0030 end-vote-close-propagation amendment (§E, TDD): these tests
land with the amendment doc (PR 1 of the workstream) and pin the
receiver half of the contract at the seams the existing close paths
already use — the servicer wire lift (the OQ 5 / ``chair_escalation``
posture), the response gate (the canonical no-turn enforcement point),
and the agent-side close dispatch (the ``cost_close`` / ``vote_close``
pattern).  Written red against the planned API where the seam does not
exist yet (the :mod:`test_end_interaction_vote_action` precedent), and
skip-guarded so ``main`` stays green; PR 3 removes the skips when:

* PR 2's proto regen gives :func:`channel_event` the
  ``interaction_close_notification`` kwarg (until then the marked-lift
  body cannot construct the event), and
* the agent-side consumption exists: the gate's ``close_notification``
  refusal branch and ``agents.persona_runtime.close_notification``
  (amendment §C.2 names both).

PR 3 may extend shared fixtures it needs (scaffolding); the committed
assertions are the acceptance and do not change.

The unmarked-event negative runs UNSKIPPED today: nothing may seed the
marker for an event that does not carry the typed field — the
typed-field-only posture (``floor_mentions_resolved``; OQ 5's
"anything else seeds nothing").

The contract (amendment CP3): a dispatch marked
``interaction_close_notification`` is control, never stimulus — the
gate refuses it pre-LLM (no turn, no Tier B bid, no LLM call; the
action loop's ingest-on-suppress still appends the closing message to
the window), and the close dispatch closes the channel scope's open
interaction immediately with the established ``end_votes`` mapping —
:data:`REASON_STRUCTURAL`, the "ended" render
(:mod:`agents.persona_runtime.interaction_boundary`: the quorum close
IS the explicit end the structural label claims) — instead of burying
it as "went idle" an idle window later.  Honoured strictly: a truthy
non-bool impostor takes no close path on either seam.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import grpc
import pytest

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.interactions import Interaction, InteractionTracker
from agents.persona_types import AgentEvent, EventType
from agents.response_gate import evaluate_response_gate

from ._receive_channel_message_helpers import (
    channel_event,
    enqueued_event,
    make_servicer,
)

SKIP_REASON = (
    "CP acceptance (0030-amendment-end-vote-close-propagation §E) — "
    "unskip in PR 3, the agent-side close-notification consumption"
)


def _notification_event(
    *,
    marker: object = True,
    respond_policy: str = "always",
    mentions: list[str] | None = None,
) -> AgentEvent:
    """The closing vote as the agent runtime sees it post-lift — the
    ``_escalation_event`` builder shape, marker on the payload port."""
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "Agreed — relay. Nothing further.",
            "channel_type": "group",
            "mentions": mentions or [],
            "respond_policy": respond_policy,
            "thread_parent_sender_id": "",
            "interaction_close_notification": marker,
        },
        channel_id="group:planning",
        sender_id="iron-fox",
    )


class _CloseNotificationAgent:
    """The ``_CostCloseAgent`` harness: a real tracker, a persistence spy,
    and the scope surface the close dispatch resolves through."""

    _MULTI_TURN_EVENT_TYPES: frozenset[EventType] = frozenset(
        {EventType.CHANNEL_MESSAGE},
    )

    def __init__(self, tracker: InteractionTracker) -> None:
        self._interaction_tracker = tracker
        self.persisted: list[Interaction] = []

    def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None:
        return event.channel_id

    async def _persist_closed_interaction(self, interaction: Interaction) -> None:
        self.persisted.append(interaction)


class TestCloseNotificationWireLift:
    """The servicer lift: payload carries the marker from the typed proto
    field only (the ``chair_escalation`` lift posture)."""

    @pytest.mark.skip(reason=SKIP_REASON)
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


@pytest.mark.skip(reason=SKIP_REASON)
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


@pytest.mark.skip(reason=SKIP_REASON)
class TestCloseNotificationClosesTracker:
    """CP3, the control half: the close dispatch (the planned
    ``agents.persona_runtime.close_notification``, the ``cost_close`` /
    ``vote_close`` sibling) closes the channel scope's open interaction
    at notification time and persists the record."""

    async def test_marked_event_closes_scope_with_structural_cause(self):
        """Closed immediately with the established ``end_votes`` mapping
        — :data:`REASON_STRUCTURAL`, rendering "ended" — not an
        idle-window later, not "went idle"."""
        from agents.persona_runtime.close_notification import (
            close_interaction_on_notification,
        )

        tracker = InteractionTracker()
        tracker.add_turn("group:planning", now=100.0)
        agent = _CloseNotificationAgent(tracker)

        await close_interaction_on_notification(agent, _notification_event())

        assert tracker.get("group:planning") is None, (
            "the notification closed the scope immediately"
        )
        assert len(agent.persisted) == 1
        assert agent.persisted[0].close_reason == REASON_STRUCTURAL

    async def test_impostor_marker_closes_nothing(self):
        """Defence-in-depth (CP3): a truthy non-bool marker must not
        fabricate a close — burying an active discussion is exactly the
        failure mode the strict-bool rule exists to block."""
        from agents.persona_runtime.close_notification import (
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
