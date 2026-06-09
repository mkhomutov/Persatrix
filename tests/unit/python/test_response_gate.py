"""Unit tests for ``agents.response_gate`` (RFC 0011 PR 4b).

The gate is the canonical enforcement point for the per-membership
``respond_policy`` declared in ``schemas/channel.schema.json``. These
tests pin the table-driven contract from RFC 0011 §D:

* ``when_mentioned`` triggers on ``agent_id ∈ event.mentions`` OR
  thread-reply-to-self.
* ``always`` triggers except when the agent is the sender.
* ``never`` always suppresses (and warns; the orchestrator filters
  these upstream of dispatch).
* DM channels (``channel_id`` starting with ``dm:``) override the
  per-membership policy and behave like ``always``.

The gate is **pure** — these tests build :class:`AgentEvent` payloads
directly and assert the returned :class:`GateDecision` without booting
the persona runtime.
"""

from __future__ import annotations

import pytest

from agents.persona_types import AgentEvent, EventType
from agents.response_gate import (
    POLICY_ALWAYS,
    POLICY_DEFENSE_IN_DEPTH,
    POLICY_LOW_SALIENCE,
    POLICY_NEVER,
    POLICY_UNKNOWN,
    POLICY_WHEN_MENTIONED,
    evaluate_response_gate,
)


def _channel_event(
    *,
    channel_id: str = "group:planning",
    sender_id: str = "alice",
    respond_policy: str = "when_mentioned",
    mentions: list[str] | None = None,
    thread_id: str | None = None,
    thread_parent_sender_id: str = "",
) -> AgentEvent:
    """Build a CHANNEL_MESSAGE event matching the wire shape produced by
    ``ReceiveChannelMessage`` after the PR 4b payload propagation."""
    payload: dict[str, object] = {
        "content": "hi",
        "channel_type": "group" if channel_id.startswith("group:") else (
            "dm" if channel_id.startswith("dm:") else "thread"
        ),
        "mentions": list(mentions or []),
        "respond_policy": respond_policy,
        "thread_parent_sender_id": thread_parent_sender_id,
    }
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload=payload,
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-1",
        thread_id=thread_id,
    )


# ─── when_mentioned policy ─────────────────────────────────────────


class TestWhenMentioned:
    def test_mentioned_responds(self):
        evt = _channel_event(respond_policy="when_mentioned", mentions=["bob"])
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.policy == POLICY_WHEN_MENTIONED
        assert d.reason == "mentioned"

    def test_not_mentioned_suppressed(self):
        evt = _channel_event(respond_policy="when_mentioned", mentions=["carol"])
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.policy == POLICY_WHEN_MENTIONED
        assert d.reason == "not_mentioned"

    def test_thread_reply_to_self_responds_even_without_mention(self):
        # The agent (bob) authored the parent of this thread. A reply
        # in that thread fires the gate even when the reply does not
        # explicitly mention bob.
        evt = _channel_event(
            respond_policy="when_mentioned",
            mentions=[],
            thread_id="parent-msg",
            thread_parent_sender_id="bob",
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.reason == "thread_reply_to_self"

    def test_thread_reply_to_other_does_not_respond(self):
        # Thread reply to carol's parent — bob is not mentioned and is
        # not the parent author. Suppress.
        evt = _channel_event(
            respond_policy="when_mentioned",
            mentions=[],
            thread_id="parent-msg",
            thread_parent_sender_id="carol",
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "not_mentioned"

    def test_mentioned_and_thread_to_other_responds(self):
        # If both mention and thread are present but only the mention
        # matches, mention wins. The reason field reflects whichever
        # check fires first; the contract is just "respond".
        evt = _channel_event(
            respond_policy="when_mentioned",
            mentions=["bob"],
            thread_id="parent",
            thread_parent_sender_id="carol",
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True


# ─── always policy ─────────────────────────────────────────────────


class TestAlways:
    def test_always_responds_when_not_self(self):
        evt = _channel_event(respond_policy="always", sender_id="alice")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.policy == POLICY_ALWAYS
        assert d.reason == "policy_always"

    def test_always_does_not_respond_to_own_message(self):
        # Defense in depth: the orchestrator already filters the sender
        # in fanout, but the gate re-checks because the cleartext gRPC
        # transport cannot be trusted to carry a non-spoofed sender_id.
        # The decision carries ``policy=defense_in_depth`` (not the
        # configured ``always``) so the ``channel.messages.gated``
        # counter cleanly separates user-policy suppressions from
        # router-malfunction suppressions — see PR #252 review N-2.
        evt = _channel_event(respond_policy="always", sender_id="bob")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "self_sender"
        assert d.policy == POLICY_DEFENSE_IN_DEPTH

    def test_when_mentioned_does_not_respond_to_own_message(self):
        # Same defense-in-depth path but starting from a different
        # configured policy; the decision still carries
        # ``policy=defense_in_depth`` regardless of the wire policy
        # because the suppression is a routing artifact, not a
        # user-policy outcome.
        evt = _channel_event(
            respond_policy="when_mentioned",
            sender_id="bob",
            mentions=["bob"],
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "self_sender"
        assert d.policy == POLICY_DEFENSE_IN_DEPTH


# ─── never policy ──────────────────────────────────────────────────


class TestNever:
    def test_never_suppresses(self, caplog: pytest.LogCaptureFixture):
        # ``never`` is filtered by the orchestrator before dispatch, so
        # reaching the gate signals routing drift. The gate suppresses
        # and warns so operators can see the surface.
        evt = _channel_event(respond_policy="never", mentions=["bob"])
        with caplog.at_level("WARNING"):
            d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.policy == POLICY_NEVER
        assert d.reason == "policy_never"
        assert any("respond_policy=never" in r.message for r in caplog.records)


# ─── DM override ───────────────────────────────────────────────────


class TestDMOverride:
    def test_dm_responds_regardless_of_membership_policy(self):
        # DM with a hypothetically-mistaken ``when_mentioned`` policy
        # still fires — DM channels are documented as ``always`` by
        # the §D table.
        evt = _channel_event(
            channel_id="dm:alice:bob",
            respond_policy="when_mentioned",
            mentions=[],
            sender_id="alice",
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.policy == POLICY_ALWAYS
        assert d.reason == "dm"

    def test_dm_does_not_respond_to_own_message(self):
        # A self-message in a DM should still suppress (defense in
        # depth — the router already filters the sender). The decision
        # carries ``policy=defense_in_depth`` so this fire is not
        # mis-attributed to the DM's natural ``always`` policy on the
        # ``channel.messages.gated`` counter (PR #252 review N-2).
        evt = _channel_event(
            channel_id="dm:alice:bob",
            respond_policy="always",
            sender_id="bob",
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "dm_self_sender"
        assert d.policy == POLICY_DEFENSE_IN_DEPTH


# ─── Non-CHANNEL_MESSAGE event types ───────────────────────────────


class TestNonChannelEvents:
    def test_tick_passes_through(self):
        # The gate has no opinion on TICK / TASK_ASSIGNED events.
        evt = AgentEvent(event_type=EventType.TICK)
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.reason == "not_channel_message"
        # Non-enforcing pass-through: the gate carries the empty-string
        # sentinel here (not a named policy constant). It never reaches the
        # ``channel.messages.gated`` metric because that counter only fires on
        # suppression — see TestPolicyContract for the field-vs-label split.
        assert d.policy == ""

    def test_legacy_chat_path_with_no_channel_id_passes_through(self):
        # The legacy ``SendChatMessage`` RPC builds CHANNEL_MESSAGE
        # events without a channel_id. ISSUE-0035 tracks the cleanup;
        # until then the gate must let those through unchanged.
        evt = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            sender_id="user-1",
            payload={"content": "hi"},
        )
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.reason == "no_channel_id"
        # Same empty-string sentinel as the non-CHANNEL_MESSAGE pass-through.
        assert d.policy == ""


# ─── GateDecision.policy contract ──────────────────────────────────


class TestPolicyContract:
    """Pin the ``GateDecision.policy`` field contract its docstring documents.

    The docstring must keep two sets distinct (an earlier revision conflated
    them):

    * **Field values** the gate assigns to ``GateDecision.policy`` — the
      legacy triple, the synthetic routing-artifact labels
      (``defense_in_depth`` / ``unknown``), and the empty-string sentinel
      ``""`` for the non-enforcing pass-through branches.
    * **Metric labels** that can land on ``channel.messages.gated``. That set
      is *not* the same: ``""`` never appears (the counter fires only on
      suppression, and the ``""`` branches return ``respond=True``), while
      ``low_salience`` *does* — but it is applied by the downstream salience
      stage (``agents/observability/_metrics_salience.py``), never by this
      pure gate.
    """

    # Every value evaluate_response_gate can put on GateDecision.policy.
    _FIELD_VALUES = frozenset(
        {
            "",
            POLICY_ALWAYS,
            POLICY_NEVER,
            POLICY_WHEN_MENTIONED,
            POLICY_DEFENSE_IN_DEPTH,
            POLICY_UNKNOWN,
        }
    )

    def _all_branch_decisions(self):
        """One representative event per gate branch."""
        yield evaluate_response_gate(
            AgentEvent(event_type=EventType.TICK), agent_id="bob"
        )
        yield evaluate_response_gate(
            AgentEvent(
                event_type=EventType.CHANNEL_MESSAGE,
                sender_id="user-1",
                payload={"content": "hi"},
            ),
            agent_id="bob",
        )
        # DM self-sender (defense_in_depth) and DM peer (always).
        yield evaluate_response_gate(
            _channel_event(channel_id="dm:bob:carol", sender_id="bob"),
            agent_id="bob",
        )
        yield evaluate_response_gate(
            _channel_event(channel_id="dm:bob:carol", sender_id="carol"),
            agent_id="bob",
        )
        # Group self-sender (defense_in_depth).
        yield evaluate_response_gate(
            _channel_event(respond_policy="always", sender_id="bob"),
            agent_id="bob",
        )
        # never.
        yield evaluate_response_gate(
            _channel_event(respond_policy="never"), agent_id="bob"
        )
        # always: open-floor, directed-elsewhere, mentioned, broadcast.
        yield evaluate_response_gate(
            _channel_event(respond_policy="always", mentions=[]), agent_id="bob"
        )
        yield evaluate_response_gate(
            _channel_event(respond_policy="always", mentions=["carol"]),
            agent_id="bob",
        )
        yield evaluate_response_gate(
            _channel_event(respond_policy="always", mentions=["bob"]),
            agent_id="bob",
        )
        yield evaluate_response_gate(
            _channel_event(respond_policy="always", mentions=["@everyone"]),
            agent_id="bob",
        )
        # when_mentioned: mentioned and not.
        yield evaluate_response_gate(
            _channel_event(respond_policy="when_mentioned", mentions=["bob"]),
            agent_id="bob",
        )
        yield evaluate_response_gate(
            _channel_event(respond_policy="when_mentioned", mentions=["carol"]),
            agent_id="bob",
        )
        # unknown / empty policy.
        yield evaluate_response_gate(
            _channel_event(respond_policy="weekly"), agent_id="bob"
        )

    def test_emitted_policies_are_a_subset_of_the_documented_field_values(self):
        emitted = {d.policy for d in self._all_branch_decisions()}
        assert emitted <= self._FIELD_VALUES, emitted - self._FIELD_VALUES

    def test_gate_never_emits_low_salience(self):
        # low_salience is a metric-only label applied outside the gate; no
        # GateDecision the gate returns may carry it.
        emitted = {d.policy for d in self._all_branch_decisions()}
        assert POLICY_LOW_SALIENCE not in emitted

    def test_empty_sentinel_only_on_non_suppressing_decisions(self):
        # ``""`` is a field value but never a gated-metric label: every
        # decision that carries it must be a respond=True pass-through.
        for d in self._all_branch_decisions():
            if d.policy == "":
                assert d.respond is True


# ─── Unknown / empty policy fail-closed ────────────────────────────


class TestUnknownPolicy:
    def test_unknown_policy_suppresses(self, caplog: pytest.LogCaptureFixture):
        # Belt-and-braces — the wire validator already rejects unknown
        # values, but if one slips through, the gate fails-closed and
        # warns.
        evt = _channel_event(respond_policy="weekly")
        with caplog.at_level("WARNING"):
            d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "unknown_policy"
        # The metric ``policy`` label must be the bounded sentinel, not the
        # raw unknown wire value — an arbitrary string here is an unbounded-
        # cardinality vector on ``channel.messages.gated`` (same precedent as
        # POLICY_DEFENSE_IN_DEPTH / POLICY_LOW_SALIENCE). The raw value is
        # still preserved in the warning log for diagnosis.
        assert d.policy == POLICY_UNKNOWN
        assert any("unknown respond_policy" in r.message for r in caplog.records)
        assert any("weekly" in r.message for r in caplog.records)
