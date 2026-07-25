"""RFC 0037 §B (v0.3.12 PR 2) — the channel-classification event seam.

The orchestrator stamps the acting channel's §A confidentiality level onto
both delivery paths: ``ChannelMessageEvent.classification`` on live gRPC
dispatch, and the channel object / history envelope ``classification`` on the
REST surface that feeds on-startup catch-up replay. This module is the ONE
place both ingress paths seed the value onto ``AgentEvent.metadata`` and the
ONE tolerant reader downstream consumers use — the PR 3 interaction-open
capture and the PR 4 §D gate read through :func:`wire_channel_classification`
exactly like the RFC 0052 origin threading reads through
``channel_wire_metadata.wire_interaction_id``.

Own module rather than a ``channel_wire_metadata`` section: both that module
and ``channel_catchup`` sit near the 500-line review cap, and the import
direction must stay ``persona_runtime → here`` (the executor entry points
must not grow a hard dep on the persona subpackage, whose
``classification.py`` twin owns the rank resolvers).

Seed posture — VERBATIM, no lattice allowlist (deliberately unlike this
seam's ``WIRE_CLOSE_TRIGGERS`` precedent): §A rule (b) says an unknown or
absent ACTING level resolves to the ``public`` floor, and RFC 0037 PR 1
pinned one-resolver-per-rule — ``persona_runtime/classification.py``'s
``acting_rank`` owns that rule. An allowlist here would be a second resolver
deciding what counts as unknown; seeding garbage verbatim is safe because
every read site resolves it to the floor, which can only UNDER-inject. The
only bound applied is byte length (the ``_INTERACTION_ID_MAX_BYTES``
rationale: metadata seeded at a trust boundary must not be an unbounded
memory vector), and empty seeds nothing — key-ABSENCE is the pre-v0.3.12
producer shape the exact-equality replay pins rely on.
"""

from __future__ import annotations

from typing import Final

from .persona_types import AgentEvent

#: The ``AgentEvent.metadata`` key both ingress paths seed. Named
#: ``channel_classification`` (not bare ``classification``) so a grep for the
#: §D gate's acting-level input never collides with the memory-side
#: ``protection_level`` vocabulary arriving in PR 3.
CHANNEL_CLASSIFICATION_METADATA_KEY: Final[str] = "channel_classification"

#: Byte bound on the seeded value, measured in UTF-8 bytes to mirror the Go
#: boundary's ``len()``. The four lattice levels are ≤ 10 bytes; 64 is
#: generous headroom for a future level while keeping a hostile producer's
#: payload out of every event's metadata bag.
_CLASSIFICATION_MAX_BYTES: Final[int] = 64


def seed_channel_classification(
    metadata: dict[str, object], classification: object,
) -> None:
    """Seed the wire classification onto event ``metadata`` — verbatim.

    Only a non-empty ``str`` within :data:`_CLASSIFICATION_MAX_BYTES` seeds;
    everything else (the proto3 ``""`` of a pre-v0.3.12 or resolve-failed
    producer, a non-string from untyped history JSON, an oversized hostile
    value) seeds nothing, and the absent key resolves to the ``public``
    acting floor at the read site (§A rule (b)). No default is written here.
    """
    if (
        isinstance(classification, str)
        and classification
        and len(classification.encode("utf-8")) <= _CLASSIFICATION_MAX_BYTES
    ):
        metadata[CHANNEL_CLASSIFICATION_METADATA_KEY] = classification


def wire_channel_classification(event: AgentEvent) -> str | None:
    """The acting channel's wire classification, read tolerantly off the
    metadata key both ingress seeds write — the ONE shared reader behind the
    PR 3 interaction-open capture and the PR 4 §D gate (the
    ``wire_interaction_id`` discipline: per-consumer inline reads would sit
    outside any drift pin and fail silent on a rename).

    ``None`` — not ``""`` — for an absent or non-string value: the return
    feeds ``persona_runtime/classification.py``'s ``acting_rank`` directly,
    whose ``None`` arm IS the §A rule-(b) ``public`` floor. Dark in PR 2:
    exported and pinned, consumed by nothing until PR 3.
    """
    value = event.metadata.get(CHANNEL_CLASSIFICATION_METADATA_KEY)
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "CHANNEL_CLASSIFICATION_METADATA_KEY",
    "seed_channel_classification",
    "wire_channel_classification",
]
