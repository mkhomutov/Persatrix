"""The chair-stall-escalation forced turn — agent half (amendment PR 3).

TDD-first: this matrix was written red against the planned API and pins
the §C item 2 contract
(``docs/rfcs/0030-amendment-chair-stall-escalation.md``):

* The ``chair_escalation`` marker is lifted off the wire
  (:func:`agents.channel_wire_metadata.channel_event_payload`) and routes
  the event down the **directed lane**: the response gate admits it with
  a dedicated bounded reason, for any non-``never`` policy (the chair
  may be ``addressed`` — CE2 requires only non-observer membership).
* The admit is **not** an open-floor admit, so the Tier B salience bid
  is skipped by the existing :func:`is_open_floor_admit` routing (TB1's
  directed-address contract) — re-running the bid would re-produce the
  very silence being escalated.
* Defence-in-depth mirrors ``floor_mentions_resolved``: the marker is
  honoured only as the strict boolean ``True`` (a spoofed truthy
  non-bool on the cleartext port must not widen admission), and it never
  overrides the fail-closed branches — a ``never`` policy and the
  self-sender stay suppressed even when marked.
* The escalation framing is rendered into the forced turn's user message
  (the per-event sibling of the ``end-interaction-vote`` system-prompt
  snippet): synthesize + vote, or call on the member best placed. The
  synthesis MUST be steered into the vote action's ``content`` payload
  (PR 610 review, the headline finding):
  :func:`agents.persona_runtime.action_parser.parse_actions` keeps only
  the fenced JSON block — prose beside it never becomes a
  ``COMPLETE_TASK``, so ``synthesize_channel_reply`` has nothing to
  promote and a synthesis written as prose next to the vote block is
  silently dropped. A snippet that lets the chair pick that output shape
  degrades the escalation to a close-vote with no synthesis on the
  record — the pre-amendment outcome with a `dispatched` metric.
"""

from __future__ import annotations

from agents.persona_types import AgentEvent, EventType
from agents.response_gate import (
    POLICY_ALWAYS,
    POLICY_WHEN_MENTIONED,
    evaluate_response_gate,
    is_open_floor_admit,
)


def _escalation_event(
    *,
    respond_policy: str = "always",
    chair_escalation: object = True,
    sender_id: str = "alex",
    channel_id: str = "group:planning",
) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": "thoughts, team?",
            "channel_type": "group",
            "mentions": [],
            "respond_policy": respond_policy,
            "thread_parent_sender_id": "",
            "chair_escalation": chair_escalation,
        },
        channel_id=channel_id,
        sender_id=sender_id,
    )


class TestGateAdmitsForcedTurn:
    def test_marked_event_admits_participant_chair(self) -> None:
        """The directed-lane admit: a marked event reaches the turn with the
        dedicated reason, regardless of the bid that silenced the original
        round."""
        decision = evaluate_response_gate(
            _escalation_event(respond_policy="always"), agent_id="nova-sparrow",
        )
        assert decision.respond is True
        assert decision.policy == POLICY_ALWAYS
        assert decision.reason == "chair_escalation"

    def test_marked_event_admits_addressed_chair(self) -> None:
        """CE2 requires only a non-observer member: an `addressed` chair —
        whose unmarked gate would suppress an unmentioned message — is
        admitted by the marker."""
        decision = evaluate_response_gate(
            _escalation_event(respond_policy="when_mentioned"), agent_id="nova-sparrow",
        )
        assert decision.respond is True
        assert decision.policy == POLICY_WHEN_MENTIONED
        assert decision.reason == "chair_escalation"

    def test_escalation_admit_skips_the_tier_b_bid(self) -> None:
        """TB1: the forced turn is the chair's lane — NOT an open-floor
        admit, so the salience seam's is_open_floor_admit routing leaves the
        bid unrun. Re-running it would re-produce the silence being
        escalated."""
        decision = evaluate_response_gate(
            _escalation_event(respond_policy="always"), agent_id="nova-sparrow",
        )
        assert not is_open_floor_admit(decision)

    def test_marker_is_strictly_boolean(self) -> None:
        """The floor_mentions_resolved posture: a truthy non-bool marker on
        the cleartext port does not widen admission — the event falls through
        to the ordinary policy branches (open floor for `always`)."""
        decision = evaluate_response_gate(
            _escalation_event(respond_policy="always", chair_escalation="true"),
            agent_id="nova-sparrow",
        )
        assert decision.reason != "chair_escalation"

    def test_marker_does_not_override_never(self) -> None:
        """Fail-closed order: a `never` policy wins over a (spoofed) marker —
        the orchestrator validates the chair is not an observer at load, so a
        marked `never` event is a forged or misrouted dispatch. The reason is
        pinned too (PR 610 review): suppressing down a *different* branch —
        say the unknown-policy fall-through — would mean the `never` check
        moved behind the marker, exactly the ordering this test exists to
        hold."""
        decision = evaluate_response_gate(
            _escalation_event(respond_policy="never"), agent_id="nova-sparrow",
        )
        assert decision.respond is False
        assert decision.reason == "policy_never"

    def test_marker_does_not_override_self_sender(self) -> None:
        """The self-sender re-check stays ahead of the marker: an agent never
        replies to its own message, escalated or not. Reason pinned for the
        same ordering argument as the `never` pin above."""
        decision = evaluate_response_gate(
            _escalation_event(respond_policy="always", sender_id="nova-sparrow"),
            agent_id="nova-sparrow",
        )
        assert decision.respond is False
        assert decision.reason == "self_sender"


class TestWireLift:
    def test_payload_carries_the_marker(self) -> None:
        from agents.channel_wire_metadata import channel_event_payload
        from agents.generated import task_pb2

        request = task_pb2.ChannelMessageEvent(
            message_id="m-1", channel_id="group:planning", channel_type="group",
            sender_id="alex", content="thoughts?", respond_policy="always",
            chair_escalation=True,
        )
        payload = channel_event_payload(request)
        assert payload["chair_escalation"] is True

        request.chair_escalation = False
        assert channel_event_payload(request)["chair_escalation"] is False


class TestEscalationFraming:
    def test_marked_event_renders_the_framing(self) -> None:
        """The forced turn's user message carries the escalation framing —
        synthesize + vote, or call on someone — ahead of the stalled
        stimulus, so the chair knows this turn may not stay silent."""
        from agents.persona_runtime.prompt_assembly import format_chair_escalation

        framed = format_chair_escalation("Message from alex:\n\nthoughts, team?")
        assert "Message from alex:\n\nthoughts, team?" in framed
        assert "chair" in framed.lower()
        assert "vote" in framed.lower()

    def test_format_event_frames_only_the_strictly_marked_turn(self) -> None:
        """Behavioural pins through ``_format_event`` itself (PR 610 review —
        the drift test pins the strict read as source text; this pins what
        the rendered prompt actually does). An ordinary dispatch (proto3
        default ``False``) and a spoofed truthy string both format plain —
        the framing never leaks onto unmarked traffic (which includes every
        replayed conversation-window turn: ``_format_peer_turn``'s synthetic
        payloads carry no marker). Called unbound with ``self=None`` through
        the same cast ``conversation_window._format_peer_message`` uses —
        the branch's self-independence is that seam's pinned contract."""
        from collections.abc import Callable
        from typing import cast

        from agents.persona_runtime.prompt_assembly import _PromptAssemblyMixin

        fmt = cast(
            "Callable[[object, AgentEvent], str]",
            _PromptAssemblyMixin._format_event,
        )
        plain = "Message from alex:\n\nthoughts, team?"
        assert fmt(None, _escalation_event(chair_escalation=False)) == plain
        assert fmt(None, _escalation_event(chair_escalation="true")) == plain
        framed = fmt(None, _escalation_event(chair_escalation=True))
        assert framed.startswith("[Chair escalation")
        assert framed.endswith(plain)

    def test_snippet_exists_and_names_the_two_outcomes(self) -> None:
        from agents.prompt_loader import load_snippet

        snippet = load_snippet("chair-escalation")
        assert "synthes" in snippet.lower(), "outcome (a): state the synthesis"
        assert "vote" in snippet.lower(), "outcome (a): cast the end-of-discussion vote"
        assert "call on" in snippet.lower() or "name the member" in snippet.lower(), (
            "outcome (b): hand the floor to the member best placed"
        )

    def test_snippet_routes_the_synthesis_through_the_vote_content(self) -> None:
        """PR 610 review, the headline finding. Outcome (a) MUST tell the
        chair to carry the synthesis *inside* the vote's ``content`` payload,
        and MUST warn against writing it as prose beside the action block:
        ``parse_actions`` keeps only the fenced JSON block (prose around it
        never becomes a ``COMPLETE_TASK``), so ``synthesize_channel_reply``
        finds no reply text to promote and the prose is silently dropped.
        The room would then see a close-vote carrying only a brief sign-off
        — or the bare ``end_vote_action`` default — with no synthesis on the
        record: the exact stall outcome the escalation exists to prevent,
        now hidden behind a ``dispatched`` metric and a green suite. The
        vote publish *is* a real channel message whose body is the payload's
        ``content``, so synthesis-in-``content`` is the one output shape
        where the synthesis and the vote genuinely travel together."""
        from agents.prompt_loader import load_snippet

        snippet = load_snippet("chair-escalation")
        assert "`content`" in snippet, (
            "outcome (a) must name the vote's `content` field as where the "
            "synthesis goes"
        )
        assert "action block" in snippet, (
            "the snippet must warn that prose beside the action block is "
            "not published"
        )
