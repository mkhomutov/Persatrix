"""Tier A directed-elsewhere eligibility for ``agents.response_gate``.

RFC 0030 relevance-amendment PR 2 (v0.3.7). The v0.3.6 manual test
surfaced a directedness defect: a message ``@``-mentioning agent *X*
drew a reply from *every* ``always`` / ``participant`` member, not just
*X*. Tier A adds the **directed-elsewhere** filter — free, deterministic,
no LLM — on top of the existing self-sender / ``never`` / ``not-mentioned``
filters:

    suppress a ``participant`` (``always``) member iff
        ``mentions`` is non-empty
        AND ``agent_id ∉ mentions``
        AND the broadcast sentinel (``@everyone``) ∉ ``mentions``.

An ``addressed`` (``when_mentioned``) member is unchanged — it is already
mention-gated, so a directed-elsewhere message never admitted it. An
``observer`` (``never``) member and a self-sender stay filtered as before.
An open-floor message (empty ``mentions``) or an explicit broadcast admits
all ``participant`` members — forwarded straight to the turn, since Tier B
(the salience bid that decides *who actually has something to add*) is a
v0.3.8 concern and does not exist yet.

These tests are the **red** half of PR 2's TDD pair: they fail against the
pre-PR gate (which admits every ``always`` member) and pass once the
directed-elsewhere branch lands. The gate is **pure** — payloads are built
directly and the :class:`GateDecision` asserted without booting the runtime.
"""

from __future__ import annotations

from agents.persona_types import AgentEvent, EventType
from agents.response_gate import (
    MENTION_EVERYONE,
    POLICY_ALWAYS,
    POLICY_DEFENSE_IN_DEPTH,
    POLICY_NEVER,
    POLICY_WHEN_MENTIONED,
    evaluate_response_gate,
)


def _channel_event(
    *,
    respond_policy: str = "always",
    sender_id: str = "alice",
    mentions: list[str] | None = None,
    channel_id: str = "group:planning",
) -> AgentEvent:
    """Build a group CHANNEL_MESSAGE matching the post-PR-4b wire shape."""
    payload: dict[str, object] = {
        "content": "hi",
        "channel_type": "group",
        "mentions": list(mentions or []),
        "respond_policy": respond_policy,
        "thread_parent_sender_id": "",
    }
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-1",
        thread_id=None,
    )


# ─── directed-elsewhere suppresses other participants ──────────────


class TestDirectedElsewhere:
    def test_participant_not_mentioned_is_suppressed_when_message_directed(self):
        # The defect repro at unit scale: alice asks "@ember-owl ...".
        # iron-fox is a participant (always) but is NOT addressed — Tier A
        # must hold it back rather than pile on.
        evt = _channel_event(
            respond_policy="always", sender_id="alice", mentions=["ember-owl"]
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is False
        assert d.reason == "directed_elsewhere"
        # The suppression stays in the ``always`` policy bucket: a
        # gated-counter fire with ``policy=always`` is, by construction,
        # exactly a directed-elsewhere suppression (self-sender is labelled
        # ``defense_in_depth``), so the §D ``{channel_id, policy}`` label
        # set distinguishes it without a new ``reason`` dimension.
        assert d.policy == POLICY_ALWAYS

    def test_mentioned_participant_responds(self):
        # The addressed participant is admitted — directedness suppresses
        # *others*, never the target.
        evt = _channel_event(
            respond_policy="always", sender_id="alice", mentions=["ember-owl"]
        )
        d = evaluate_response_gate(evt, agent_id="ember-owl")
        assert d.respond is True
        assert d.policy == POLICY_ALWAYS
        assert d.reason == "policy_always"

    def test_participant_among_several_mentions_responds(self):
        # Multiple explicit recipients: each addressed participant is in.
        evt = _channel_event(
            respond_policy="always",
            sender_id="alice",
            mentions=["ember-owl", "iron-fox"],
        )
        assert evaluate_response_gate(evt, agent_id="iron-fox").respond is True
        assert evaluate_response_gate(evt, agent_id="ember-owl").respond is True


# ─── open floor admits all participants (no Tier B yet) ────────────


class TestOpenFloor:
    def test_open_floor_admits_participant(self):
        # No mentions → open floor. Every participant reaches the turn:
        # Tier B (silence-when-nothing-to-add) is v0.3.8, so v0.3.7 keeps
        # today's open-floor admit-all behaviour — no regression.
        evt = _channel_event(respond_policy="always", sender_id="alice", mentions=[])
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is True
        assert d.reason == "policy_always"

    def test_no_mentions_key_admits_participant(self):
        # Defensive: a payload that omits ``mentions`` entirely is an open
        # floor, identical to an empty list.
        evt = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "hi",
                "channel_type": "group",
                "respond_policy": "always",
                "thread_parent_sender_id": "",
            },
            channel_id="group:planning",
            sender_id="alice",
            message_id="msg-1",
        )
        assert evaluate_response_gate(evt, agent_id="iron-fox").respond is True


# ─── broadcast (@everyone) disables the directed-elsewhere filter ──


class TestBroadcast:
    def test_broadcast_admits_unmentioned_participant(self):
        # D3 (amendment OQ #5, adopted): an explicit broadcast addresses the
        # room, so the directed-elsewhere filter is disabled even though
        # mentions is non-empty and this member is not individually named.
        evt = _channel_event(
            respond_policy="always",
            sender_id="alice",
            mentions=[MENTION_EVERYONE],
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is True
        assert d.reason == "policy_always"

    def test_broadcast_alongside_a_named_mention_still_admits_others(self):
        # "@everyone — and especially @ember-owl": the broadcast sentinel
        # wins, so iron-fox (a participant, unnamed) is still admitted.
        evt = _channel_event(
            respond_policy="always",
            sender_id="alice",
            mentions=[MENTION_EVERYONE, "ember-owl"],
        )
        assert evaluate_response_gate(evt, agent_id="iron-fox").respond is True


# ─── cross-language wire-contract guard ───────────────────────────


class TestBroadcastSentinelWireContract:
    def test_sentinel_value_is_the_pinned_wire_literal(self):
        # The broadcast sentinel is an *in-band wire value*: it travels in a
        # message's ``mentions`` list across the gRPC/REST boundary and is
        # read by both this gate and the Go transport (the candidate set in
        # ``internal/channels/floor_control.go`` and the persist exemption in
        # ``internal/channels/sqlite_messages.go``, whose ``MentionEveryone``
        # const must stay byte-identical). The two constants are coupled only
        # by this literal — nothing else fails if one side is renamed — so
        # pin the wire value here (and in Go's
        # ``TestMentionEveryone_WireContract``). A change to either constant
        # alone now breaks its own suite loudly instead of silently splitting
        # the broadcast path (one side admits, the other suppresses).
        assert MENTION_EVERYONE == "@everyone"


# ─── unchanged classes: addressed / observer / self ───────────────


class TestUnchangedDispositions:
    def test_addressed_member_still_mention_gated_on_directed_message(self):
        # An ``addressed`` (when_mentioned) member is already mention-gated,
        # so a message directed at someone else never admitted it — Tier A
        # changes nothing here.
        evt = _channel_event(
            respond_policy="when_mentioned",
            sender_id="alice",
            mentions=["ember-owl"],
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is False
        assert d.policy == POLICY_WHEN_MENTIONED
        assert d.reason == "not_mentioned"

    def test_addressed_member_admitted_when_named(self):
        evt = _channel_event(
            respond_policy="when_mentioned",
            sender_id="alice",
            mentions=["iron-fox"],
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is True
        assert d.reason == "mentioned"

    def test_observer_always_filtered_even_on_broadcast(self):
        # ``observer`` (never) never speaks, broadcast or not.
        evt = _channel_event(
            respond_policy="never",
            sender_id="alice",
            mentions=[MENTION_EVERYONE],
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is False
        assert d.policy == POLICY_NEVER
        assert d.reason == "policy_never"

    def test_self_sender_filtered_before_directedness(self):
        # The sender never replies to its own broadcast — the self-sender
        # defence-in-depth filter runs ahead of the directedness branch.
        evt = _channel_event(
            respond_policy="always",
            sender_id="iron-fox",
            mentions=[MENTION_EVERYONE],
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is False
        assert d.reason == "self_sender"
        assert d.policy == POLICY_DEFENSE_IN_DEPTH

    def test_participant_disposition_value_directed_elsewhere(self):
        # Defence in depth: an un-normalized ``participant`` disposition
        # value reaching the gate is aliased to ``always`` and still obeys
        # the directed-elsewhere filter.
        evt = _channel_event(
            respond_policy="participant",
            sender_id="alice",
            mentions=["ember-owl"],
        )
        d = evaluate_response_gate(evt, agent_id="iron-fox")
        assert d.respond is False
        assert d.reason == "directed_elsewhere"
        assert d.policy == POLICY_ALWAYS
