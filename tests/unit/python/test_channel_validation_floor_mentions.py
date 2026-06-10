"""Validation pins for the ``floor_mentions`` wire field (v0.3.8).

Sibling to ``test_channel_validation.py`` — that file sits at 485 of the
500-line review cap (``scripts/checks/file_size.py``), so these pins
take the established per-topic split (the same reason the amendment's
drift pins live in ``test_cross_language_floor_mentions_drift.py``).

``floor_mentions`` (RFC 0030 floor-capable-directedness amendment) is
the orchestrator-resolved Tier A suppression basis and rides the same
cleartext gRPC port as ``mentions``. ``validate_channel_message_event``
must reject a malformed or spoofed list *before* the response gate
consumes it:

* the ``mentions`` bounds apply symmetrically (entry cap, participant-id
  pattern) — the subset is attacker-writable on the port, so it gets the
  same hygiene as the field it derives from;
* **no** ``@everyone`` carve-out — the sentinel is never a member id, so
  the resolver never emits it; an inbound one is a malformed producer;
* every entry must appear in raw ``mentions`` — the resolver computes an
  intersection, so a conforming producer satisfies this by construction.
  Enforcing it restores the amendment's skew invariant against *spoofed*
  producers too: a subset of ``mentions`` can only ever *narrow*
  suppression relative to the raw basis, so a spoofed ``floor_mentions``
  cannot silence a participant the raw mentions would not already have
  silenced. (The one spoof shape that remains — flag true with an empty
  list, widening admission to pre-v0.3.7 behaviour — is unverifiable by
  design: a resolved-empty subset is the motivating human-mention case,
  and the amendment's trust-extension paragraph accepts it.)

The legacy producer (an old orchestrator: no floor keys at all → proto3
defaults) must keep validating clean — the gate's flag-keyed fallback,
not validation, owns that path.
"""

from __future__ import annotations

from typing import Any

from agents.channel_validation import validate_channel_message_event
from agents.generated import task_pb2


def _event(**overrides: object) -> task_pb2.ChannelMessageEvent:
    fields: dict[str, Any] = {
        "message_id": "msg-001",
        "channel_id": "group:general",
        "channel_type": "group",
        "sender_id": "iron-fox",
        "content": "hello",
        "timestamp": "2026-05-04T00:00:00Z",
        "thread_id": "",
        "mentions": [],
        "respond_policy": "always",
        "thread_parent_sender_id": "",
    }
    fields.update(overrides)
    return task_pb2.ChannelMessageEvent(**fields)


class TestFloorMentionsBounds:
    """The ``mentions`` hygiene, applied symmetrically to the subset."""

    def test_oversize_list_rejected(self):
        # The subset rule alone cannot bound the list: a spoofed producer
        # may repeat one legitimate mention id without limit, so the
        # entry cap must fire first (and does — it precedes the
        # per-entry checks).
        err, ts = validate_channel_message_event(
            _event(
                mentions=["ember-owl"],
                floor_mentions=["ember-owl"] * 11,
            ),
        )
        assert err is not None
        assert "floor_mentions list exceeds 10 entries" in err
        assert ts is None

    def test_invalid_participant_id_rejected(self):
        err, ts = validate_channel_message_event(
            _event(floor_mentions=["Not_A_Valid_Id"]),
        )
        assert err is not None
        assert "floor_mentions[0] is not a valid participant id" in err
        assert ts is None

    def test_everyone_sentinel_rejected_no_carve_out(self):
        # ``mentions`` carves the broadcast sentinel out of the pattern
        # check (D3); ``floor_mentions`` must NOT — the resolver never
        # emits it (it is not a member id), so an inbound sentinel is a
        # malformed producer, not a broadcast.
        err, ts = validate_channel_message_event(
            _event(mentions=["@everyone"], floor_mentions=["@everyone"]),
        )
        assert err is not None
        assert "floor_mentions[0]" in err
        assert ts is None


class TestFloorMentionsSubsetOfMentions:
    """Every entry must appear in raw ``mentions`` (resolver = ∩)."""

    def test_entry_not_in_mentions_rejected(self):
        # The spoof shape this closes: raw mentions name only the
        # floor-incapable human (would resolve open floor), a spoofed
        # subset names a member the message never mentioned — without
        # the check, every unnamed participant suppresses on a fabricated
        # basis wider than the raw mentions could ever justify.
        err, ts = validate_channel_message_event(
            _event(mentions=["alex"], floor_mentions=["iron-fox"]),
        )
        assert err is not None
        assert "floor_mentions[0]" in err
        assert "not in mentions" in err
        assert ts is None

    def test_resolved_subset_accepted(self):
        # The conforming producer: the orchestrator's intersection.
        err, ts = validate_channel_message_event(
            _event(
                mentions=["alex", "ember-owl"],
                floor_mentions=["ember-owl"],
            ),
        )
        assert err is None
        assert ts is not None

    def test_legacy_producer_with_no_floor_keys_accepted(self):
        # An old orchestrator sends no floor keys at all (proto3
        # defaults: empty list, false flag) — must validate clean; the
        # gate's flag-keyed fallback owns this path, not validation.
        err, ts = validate_channel_message_event(_event(mentions=["alex"]))
        assert err is None
        assert ts is not None
