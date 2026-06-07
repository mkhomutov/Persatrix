"""Disposition-vocabulary defence-in-depth for ``agents.response_gate``.

RFC 0030 relevance-amendment PR 1 introduces the disposition vocabulary
(``participant`` / ``addressed`` / ``observer``) and normalizes it back
to the legacy ``respond_policy`` (``always`` / ``when_mentioned`` /
``never``) at the Go config-load boundary. The gate therefore normally
sees only the legacy three values.

These tests pin the gate's **defence-in-depth** behaviour: if a
disposition value ever reaches the wire un-normalized (a hand-edited
membership row, a future caller that bypasses the loader), the gate must
recognise it as an alias of its legacy equivalent rather than fall
through to the fail-closed ``unknown_policy`` branch. PR 1 is otherwise
behaviourally inert — the legacy branches (covered by
``test_response_gate.py``) are unchanged.
"""

from __future__ import annotations

from agents.persona_types import AgentEvent, EventType
from agents.response_gate import (
    POLICY_ALWAYS,
    POLICY_NEVER,
    POLICY_WHEN_MENTIONED,
    evaluate_response_gate,
)


def _channel_event(
    *,
    respond_policy: str,
    sender_id: str = "alice",
    mentions: list[str] | None = None,
) -> AgentEvent:
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
        channel_id="group:planning",
        sender_id=sender_id,
        message_id="msg-1",
        thread_id=None,
    )


class TestParticipantAliasesAlways:
    def test_participant_responds_like_always(self):
        evt = _channel_event(respond_policy="participant", sender_id="alice")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.policy == POLICY_ALWAYS
        assert d.reason == "policy_always"

    def test_participant_still_filters_self_sender(self):
        # Self-sender defence-in-depth must hold regardless of vocabulary.
        evt = _channel_event(respond_policy="participant", sender_id="bob")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "self_sender"


class TestChairAliasesAlways:
    """The v0.3.8 Tier B ``chair`` disposition is a low-threshold
    facilitator — a ``participant`` (legacy ``always``) at the gate. The Go
    loader normalizes ``chair`` to ``always`` + a low threshold, so the gate
    normally never sees ``chair`` on the wire; this pins the defence-in-depth
    alias for a value that reaches the gate un-normalized. The chair's
    low-threshold behaviour and its (inert) Layer-5 hooks land downstream in
    later Tier B PRs — PR 1 is behaviourally inert here too.
    """

    def test_chair_responds_like_always(self):
        evt = _channel_event(respond_policy="chair", sender_id="alice")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.policy == POLICY_ALWAYS
        assert d.reason == "policy_always"

    def test_chair_still_filters_self_sender(self):
        evt = _channel_event(respond_policy="chair", sender_id="bob")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.reason == "self_sender"


class TestObserverAliasesNever:
    def test_observer_suppresses_like_never(self):
        evt = _channel_event(respond_policy="observer")
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.policy == POLICY_NEVER
        assert d.reason == "policy_never"


class TestAddressedAliasesWhenMentioned:
    def test_addressed_responds_when_mentioned(self):
        evt = _channel_event(respond_policy="addressed", mentions=["bob"])
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is True
        assert d.policy == POLICY_WHEN_MENTIONED
        assert d.reason == "mentioned"

    def test_addressed_suppressed_when_not_mentioned(self):
        evt = _channel_event(respond_policy="addressed", mentions=["carol"])
        d = evaluate_response_gate(evt, agent_id="bob")
        assert d.respond is False
        assert d.policy == POLICY_WHEN_MENTIONED
        assert d.reason == "not_mentioned"
