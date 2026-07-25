"""RFC 0037 §B (v0.3.12 PR 2) — classification on the wire, receive side.

Pins the two-path ingress contract: the live gRPC lift
(``channel_wire_metadata.seed_wire_metadata``) and the catch-up replay
(``channel_catchup._build_replay_event``) seed the SAME
``channel_classification`` metadata key through the SAME seam
(``agents/channel_event_classification.py``), and the one shared reader
resolves it — with the §A rule-(b) fail-closed tie: an unclassified legacy
event reads as ``None``, which ``acting_rank`` floors to ``public``, never
``internal``. Dark in PR 2: the seeded value is consumed by nothing until
the PR 3 interaction-open capture.
"""

from __future__ import annotations

from agents.channel_catchup import _build_replay_event
from agents.channel_event_classification import (
    CHANNEL_CLASSIFICATION_METADATA_KEY,
    seed_channel_classification,
    wire_channel_classification,
)
from agents.channel_wire_metadata import seed_wire_metadata
from agents.generated import task_pb2
from agents.persona_runtime.classification import (
    CLASSIFICATION_PUBLIC,
    acting_rank,
)
from agents.persona_types import AgentEvent, EventType


def _event(metadata: dict | None = None) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={"content": "x"},
        channel_id="group:leadership",
        sender_id="iron-fox",
        metadata=metadata if metadata is not None else {},
    )


class TestSeedSemantics:
    """The seed is verbatim-or-nothing: no allowlist, no default — the §A
    rule-(b) resolution belongs to ``acting_rank`` alone (the PR 1
    one-resolver-per-rule discipline)."""

    def test_lattice_value_seeds_verbatim(self):
        md: dict[str, object] = {}
        seed_channel_classification(md, "restricted")
        assert md == {CHANNEL_CLASSIFICATION_METADATA_KEY: "restricted"}

    def test_empty_and_non_string_seed_nothing(self):
        md: dict[str, object] = {}
        seed_channel_classification(md, "")
        seed_channel_classification(md, None)
        seed_channel_classification(md, 3)
        assert md == {}

    def test_oversized_value_seeds_nothing(self):
        md: dict[str, object] = {}
        seed_channel_classification(md, "x" * 65)
        assert md == {}

    def test_out_of_lattice_value_seeds_verbatim_and_floors_at_read(self):
        """Deliberately NOT allowlisted at the seed: garbage from a hostile
        or future producer rides verbatim, and the read-side resolver
        floors it — under-injection is the safe failure direction."""
        md: dict[str, object] = {}
        seed_channel_classification(md, "cosmic")
        assert md == {CHANNEL_CLASSIFICATION_METADATA_KEY: "cosmic"}
        assert acting_rank("cosmic") == acting_rank(CLASSIFICATION_PUBLIC)


class TestReader:
    def test_reads_seeded_value(self):
        ev = _event({CHANNEL_CLASSIFICATION_METADATA_KEY: "secret"})
        assert wire_channel_classification(ev) == "secret"

    def test_absent_and_non_string_read_as_none(self):
        assert wire_channel_classification(_event()) is None
        ev = _event({CHANNEL_CLASSIFICATION_METADATA_KEY: 7})
        assert wire_channel_classification(ev) is None


class TestLiveGRPCPath:
    """``seed_wire_metadata`` — the single ``ReceiveChannelMessage``
    reconciliation boundary — threads the typed proto field."""

    def test_classified_event_seeds_metadata(self):
        request = task_pb2.ChannelMessageEvent(
            message_id="m1", channel_id="group:leadership",
            sender_id="iron-fox", classification="restricted",
        )
        ev = _event()
        seed_wire_metadata(ev, request)
        assert ev.metadata[CHANNEL_CLASSIFICATION_METADATA_KEY] == "restricted"
        assert wire_channel_classification(ev) == "restricted"

    def test_unclassified_legacy_event_resolves_fail_closed(self):
        """The PR 2 acceptance: a pre-v0.3.12 producer's event (proto3
        empty field) seeds NO key, the reader returns ``None``, and the §A
        rule-(b) resolver floors it to ``public`` — never ``internal``."""
        request = task_pb2.ChannelMessageEvent(
            message_id="m1", channel_id="group:planning", sender_id="iron-fox",
        )
        ev = _event()
        seed_wire_metadata(ev, request)
        assert CHANNEL_CLASSIFICATION_METADATA_KEY not in ev.metadata
        resolved = wire_channel_classification(ev)
        assert resolved is None
        assert acting_rank(resolved) == acting_rank(CLASSIFICATION_PUBLIC)


class TestCatchupReplayPath:
    """``_build_replay_event`` — the REST leg: the channel-list object's
    ``classification`` (RFC 0037's channelResponse field) stamps replayed
    events with the same key the live path seeds."""

    @staticmethod
    def _msg() -> dict:
        return {
            "id": "m1", "channel_id": "group:leadership",
            "sender_id": "iron-fox", "content": "hello",
            "timestamp": "2026-07-25T10:00:00Z",
        }

    def test_replay_event_carries_channel_classification(self):
        event = _build_replay_event(
            self._msg(), "group:leadership", "when_mentioned",
            {"id": "group:leadership", "channel_type": "group",
             "classification": "restricted"},
        )
        assert event.metadata[CHANNEL_CLASSIFICATION_METADATA_KEY] == "restricted"
        assert wire_channel_classification(event) == "restricted"

    def test_legacy_history_replays_without_the_key(self):
        """A pre-v0.3.12 orchestrator's channel JSON has no
        ``classification`` — the replayed event keeps key-ABSENCE (the
        exact-equality replay pins elsewhere rely on it) and reads floor."""
        event = _build_replay_event(
            self._msg(), "group:planning", "when_mentioned",
            {"id": "group:planning", "channel_type": "group"},
        )
        assert event.metadata == {"replay_mode": True}
        assert wire_channel_classification(event) is None
