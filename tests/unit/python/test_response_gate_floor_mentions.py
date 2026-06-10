"""Floor-capable-directedness gate matrix (RFC 0030 amendment, v0.3.8).

Pins the §C item 3 contract for the ``POLICY_ALWAYS`` suppression branch
of :func:`agents.response_gate.evaluate_response_gate` — the Python half
of the paired basis flip
(``docs/rfcs/0030-amendment-floor-capable-directedness.md``):

* The directed-elsewhere suppression basis is the orchestrator-resolved
  ``floor_mentions`` subset **iff** ``floor_mentions_resolved`` is
  ``True`` — keyed on the flag, never on the list's own presence or
  emptiness, which the wire cannot express (proto3 repeated fields have
  no presence; §C item 2).
* Flag false or missing (an old orchestrator, the legacy in-process
  path) — and a malformed non-list under a true flag — fall back to the
  raw-mentions basis: the pre-amendment behaviour, degrading toward
  *over*-suppression, never under-suppression.
* Flag true with an empty list: open floor — the motivating case (a sole
  mention of the human operator), carrying the ``policy_always`` reason
  so :func:`is_open_floor_admit` routes it into the Tier B bid exactly
  like an unmentioned message (no third lane).
* The ``mentioned``/``broadcast`` admit paths read raw ``mentions``
  throughout (amendment OQ 3).

The matrix deliberately has no present-vs-absent axis for the list
itself — the flag is that distinction's wire-expressible replacement.
"""

from __future__ import annotations

from agents.persona_types import AgentEvent, EventType
from agents.response_gate import (
    POLICY_ALWAYS,
    evaluate_response_gate,
    is_open_floor_admit,
)


def _always_event(
    *,
    mentions: list[str] | None = None,
    floor_mentions: object = None,
    floor_mentions_resolved: object = None,
) -> AgentEvent:
    """Build an ``always``-policy CHANNEL_MESSAGE event with the wire shape
    ``ReceiveChannelMessage`` produces after the v0.3.8 payload lift. The
    two floor keys are omitted entirely when ``None`` so the legacy-payload
    case (an old orchestrator: no keys at all) is expressible alongside the
    explicit ``floor_mentions_resolved=False`` case.
    """
    payload: dict[str, object] = {
        "content": "hi",
        "channel_type": "group",
        "mentions": list(mentions or []),
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    if floor_mentions is not None:
        payload["floor_mentions"] = floor_mentions
    if floor_mentions_resolved is not None:
        payload["floor_mentions_resolved"] = floor_mentions_resolved
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id="group:planning",
        sender_id="nova-sparrow",
    )


class TestResolvedBasis:
    """Flag true: the resolved subset is the suppression basis."""

    def test_resolved_empty_subset_is_open_floor(self) -> None:
        """The motivating case: "@alex, here's our recommendation…" — raw
        mentions name only the floor-incapable human, the orchestrator
        resolved the subset to empty, and the participant is admitted."""
        event = _always_event(
            mentions=["alex"], floor_mentions=[], floor_mentions_resolved=True,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is True
        assert decision.policy == POLICY_ALWAYS
        assert decision.reason == "policy_always"

    def test_resolved_open_floor_admit_reaches_tier_b(self) -> None:
        """The reclassified admit is an *open-floor* admit — same lane as an
        unmentioned message, so the Tier B salience bid still governs it
        (the amendment moves the message between two existing lanes)."""
        event = _always_event(
            mentions=["alex"], floor_mentions=[], floor_mentions_resolved=True,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert is_open_floor_admit(decision)

    def test_resolved_nonempty_subset_still_suppresses_unnamed(self) -> None:
        """Pile-on protection intact: a floor-capable addressee exists, so
        the unnamed participant is still directed-elsewhere."""
        event = _always_event(
            mentions=["alex", "ember-owl"],
            floor_mentions=["ember-owl"],
            floor_mentions_resolved=True,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is False
        assert decision.policy == POLICY_ALWAYS
        assert decision.reason == "directed_elsewhere"

    def test_named_agent_admits_on_raw_mentions(self) -> None:
        """The admit path stays on raw ``mentions`` (OQ 3): the named member
        admits with the directed ``mentioned`` reason regardless of the
        resolved subset's content."""
        event = _always_event(
            mentions=["iron-fox"],
            floor_mentions=["iron-fox"],
            floor_mentions_resolved=True,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is True
        assert decision.reason == "mentioned"
        assert not is_open_floor_admit(decision)

    def test_broadcast_admits_on_raw_mentions(self) -> None:
        """``@everyone`` rides raw ``mentions`` and admits with the directed
        ``broadcast`` reason; the sentinel never appears in the resolved
        subset (it is not a member id), and that must not demote the
        broadcast to a suppression."""
        event = _always_event(
            mentions=["@everyone", "ember-owl"],
            floor_mentions=["ember-owl"],
            floor_mentions_resolved=True,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is True
        assert decision.reason == "broadcast"

    def test_malformed_subset_falls_back_to_raw_basis(self) -> None:
        """A non-list ``floor_mentions`` under a true flag is a malformed or
        spoofed producer: fall back to the raw-mentions basis (the
        over-suppression direction), exactly like a false flag."""
        event = _always_event(
            mentions=["alex"],
            floor_mentions="alex",  # str, not list
            floor_mentions_resolved=True,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is False
        assert decision.reason == "directed_elsewhere"


class TestLegacyFallback:
    """Flag false/absent/non-bool: the raw-mentions basis (pre-amendment)."""

    def test_no_floor_keys_keeps_raw_basis(self) -> None:
        """An old orchestrator (or the legacy in-process path) sends no
        floor keys at all: a mention of anyone still suppresses the
        unnamed participant — today's behaviour, unchanged."""
        event = _always_event(mentions=["alex"])
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is False
        assert decision.reason == "directed_elsewhere"

    def test_flag_false_ignores_present_list(self) -> None:
        """The basis switch keys on the flag, never on the list's presence:
        a present-but-unresolved list does not flip the basis."""
        event = _always_event(
            mentions=["alex"], floor_mentions=[], floor_mentions_resolved=False,
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is False
        assert decision.reason == "directed_elsewhere"

    def test_truthy_non_bool_flag_does_not_flip_basis(self) -> None:
        """``is True`` is deliberate: a spoofed truthy non-bool on the
        cleartext port (e.g. the string "true") must not widen admission."""
        event = _always_event(
            mentions=["alex"], floor_mentions=[], floor_mentions_resolved="true",
        )
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is False
        assert decision.reason == "directed_elsewhere"

    def test_empty_mentions_stays_open_floor(self) -> None:
        """The pre-amendment open-floor admit is untouched: no mentions, no
        floor keys, participant admitted."""
        event = _always_event()
        decision = evaluate_response_gate(event, agent_id="iron-fox")
        assert decision.respond is True
        assert decision.reason == "policy_always"
        assert is_open_floor_admit(decision)
