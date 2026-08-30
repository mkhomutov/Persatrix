"""ISSUE-0130 (b) — the replay principal seed, and why PRESENCE is the signal.

``channelMessageResponse.principal_id`` (channel-store v12) is the only
place a catch-up replay can learn whose tenant a message belonged to: the
replay is not a live dispatch, so there is no ``persatrix-principal``
gRPC header to lift.  :func:`agents.principal_id.seed_principal_metadata`
puts it on the SAME event-metadata key the live ingress writes, so the one
binder in ``on_event`` attributes a replayed turn exactly as it attributes
a live one.

The contract these pin is narrow and load-bearing:

* the value is seeded verbatim, never coerced or defaulted — a defaulted
  ``"local"`` would be indistinguishable from a real one; and
* the RETURN says whether anything was seeded, because the close path
  branches on that rather than on the value.  A missing key means the
  orchestrator predates the column, which is unattributable; a present
  ``"local"`` is a real answer and is attributable.  The Go DTO is
  deliberately not ``omitempty`` to keep those two apart on the wire,
  and this is the Python end of that agreement.
"""

from __future__ import annotations

from agents.channel_replay_event import build_replay_event
from agents.principal_id import (
    EVENT_PRINCIPAL_METADATA_KEY,
    seed_principal_metadata,
)

CHANNEL = {"channel_type": "group", "id": "group:planning"}


class TestSeedPrincipalMetadata:
    def test_a_real_principal_seeds_and_reports_true(self):
        md: dict[str, object] = {}
        assert seed_principal_metadata(md, "alice-person") is True
        assert md == {EVENT_PRINCIPAL_METADATA_KEY: "alice-person"}

    def test_local_is_a_value_like_any_other(self):
        md: dict[str, object] = {}
        assert seed_principal_metadata(md, "local") is True
        assert md[EVENT_PRINCIPAL_METADATA_KEY] == "local"

    def test_a_missing_field_seeds_nothing(self):
        md: dict[str, object] = {}
        assert seed_principal_metadata(md, None) is False
        assert md == {}

    def test_an_empty_string_seeds_nothing(self):
        # The degraded publish echo the Go handler exists to prevent, and
        # a third value ("") must never reach the tenant vocabulary.
        md: dict[str, object] = {}
        assert seed_principal_metadata(md, "") is False
        assert md == {}

    def test_a_non_string_seeds_nothing(self):
        md: dict[str, object] = {}
        assert seed_principal_metadata(md, 7) is False
        assert md == {}


def _row(**extra: object) -> dict:
    return {
        "id": "m-1", "sender_id": "alice", "content": "hello",
        "mentions": [], **extra,
    }


class TestBuilderSeedsFromTheHistoryRow:
    def test_the_rows_principal_reaches_the_event(self):
        event = build_replay_event(
            _row(principal_id="alice-person"), "group:planning", "all", CHANNEL,
        )
        assert event.metadata[EVENT_PRINCIPAL_METADATA_KEY] == "alice-person"

    def test_a_pre_v12_row_carries_no_principal_key(self):
        # Key ABSENCE, not a blank: the exact-equality replay pins
        # elsewhere depend on the metadata staying minimal, and the close
        # path reads absence as "this span is unattributable".
        event = build_replay_event(_row(), "group:planning", "all", CHANNEL)
        assert EVENT_PRINCIPAL_METADATA_KEY not in event.metadata

    def test_nothing_but_the_principal_field_is_read(self):
        # The seed must never take a caller-supplied tenant by another
        # name: an agent that could name a principal would hold the
        # cross-tenant primitive ISSUE-0130 rules out.  There is no such
        # field on the DTO — this pins that the builder does not invent
        # one by reading the message metadata.
        event = build_replay_event(
            _row(metadata={"persatrix_principal": "bob-person"}),
            "group:planning", "all", CHANNEL,
        )
        assert EVENT_PRINCIPAL_METADATA_KEY not in event.metadata
