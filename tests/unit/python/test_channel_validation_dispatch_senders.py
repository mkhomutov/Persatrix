"""RFC 0052 §B/§D — the agent receiver accepts the reserved forced-turn
dispatch senders (the convene / synthesis-close *delivery* fix).

The orchestrator OPENS (§B convene) and CLOSES (§D synthesis) an autonomous
discussion by dispatching a FORCED TURN to the convener / chair under a
SYNTHETIC sender id that deliberately contains a reserved ``:`` so it can never
collide with a real participant id (Go ``ConveneDispatchSenderID`` /
``SynthesisDispatchSenderID``; the ``:`` is forbidden by the participant-id
pattern, and ``convene_test.go`` pins that on purpose — a valid-pattern sender
like ``orchestrator`` could be a real agent id and would hit the receiver's
self-sender defence).

But the agent's inbound ``validate_channel_message_event`` applied that SAME
pattern to ``sender_id`` — so, before this fix, it REFUSED the directive at the
transport gate (``sender_id is not a valid participant id``) BEFORE the
forced-turn admit ever ran: ``ConveneChannel`` 202'd, the receiver ``503``'d the
delivery, and a convened channel produced no opener and no synthesis. The
in-process Go acceptance tests mock the dispatcher, so they never exercised the
real Python receiver gate; this surfaced only on a booted ``make demo-autonomous``.

These tests pin the carve-out: the two reserved sentinels are accepted by both
inbound validators, and nothing else about the sender-id trust boundary
(PR #248 — cleartext-port spoofing) is loosened. The Go/Python drift pin lives
with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.channel_validation import (
    _RESERVED_DISPATCH_SENDER_IDS,
    validate_channel_message_dict,
    validate_channel_message_event,
)

from .test_channel_validation import _event, _msg_dict
from .test_cross_language_interaction_wire_drift import _go_const

# The two reserved forced-turn dispatch senders (Go source of truth:
# ``ConveneDispatchSenderID`` / ``SynthesisDispatchSenderID``). Pinned against
# the Go constants by ``test_reserved_dispatch_senders_agree_with_go`` below.
_RESERVED = ["orchestrator:convene", "orchestrator:synthesis"]


class TestReservedDispatchSendersAccepted:
    """The convene (§B) / synthesis (§D) forced-turn directives must clear the
    inbound validator — otherwise the receiver refuses delivery before the
    forced-turn admit runs and a convened channel does nothing."""

    @pytest.mark.parametrize("sender", _RESERVED)
    def test_event_accepts_reserved_sender(self, sender: str) -> None:
        err, ts = validate_channel_message_event(_event(sender_id=sender))
        assert err is None, f"reserved dispatch sender {sender!r} must be accepted"
        assert isinstance(ts, float)

    @pytest.mark.parametrize("sender", _RESERVED)
    def test_dict_accepts_reserved_sender(self, sender: str) -> None:
        err, ts = validate_channel_message_dict(
            _msg_dict(sender_id=sender),
            channel_type="group",
        )
        assert err is None, f"reserved dispatch sender {sender!r} must be accepted"
        assert isinstance(ts, float)


class TestSenderTrustBoundaryPreserved:
    """The carve-out is EXACTLY the two enumerated sentinels — every other
    ``:``-bearing or malformed sender still rejects, so PR #248's transport
    trust boundary (a spoofed author id on the cleartext gRPC port) is not
    loosened by the fix."""

    @pytest.mark.parametrize(
        "sender",
        [
            "orchestrator:evil",
            "ember-owl:convene",
            "orchestrator:",
            ":convene",
            "bad:id",
            "@everyone",
        ],
    )
    def test_event_still_rejects_non_reserved_senders(self, sender: str) -> None:
        err, _ = validate_channel_message_event(_event(sender_id=sender))
        assert err is not None, f"{sender!r} must still be rejected"
        assert "sender_id" in err

    def test_reserved_sender_is_not_treated_as_a_mention(self) -> None:
        # The carve-out is scoped to ``sender_id`` only — a reserved sentinel in
        # the mentions list is NOT a valid participant id and must still reject
        # (mirrors the ``@everyone`` sentinel being mention-only, not a sender).
        err, _ = validate_channel_message_event(
            _event(mentions=["orchestrator:convene"]),
        )
        assert err is not None
        assert "mentions" in err


def test_reserved_dispatch_senders_agree_with_go() -> None:
    """The agent receiver's carve-out set MUST exactly equal the Go dispatch
    constants. The sentinels are AUTHORED Go-side, so a one-sided rename — Go
    changes the dispatched sender, or Python changes the carve-out — silently
    reverts the delivery fix: the receiver refuses the forced-turn directive at
    the transport gate and a convened channel does nothing. Source pinned as
    text so the test runs anywhere the Python unit suite runs (no Go toolchain),
    the ``test_cross_language_*_drift.py`` posture."""
    convene = _go_const(Path("internal/channels/convene.go"), "ConveneDispatchSenderID")
    synthesis = _go_const(Path("internal/channels/synthesis_close.go"), "SynthesisDispatchSenderID")
    assert _RESERVED_DISPATCH_SENDER_IDS == {convene, synthesis}, (
        "the agent receiver's reserved-dispatch-sender carve-out must exactly "
        "match Go's ConveneDispatchSenderID / SynthesisDispatchSenderID"
    )
