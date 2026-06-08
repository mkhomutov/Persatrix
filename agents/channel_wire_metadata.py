"""Lift typed ``ChannelMessageEvent`` wire fields onto ``AgentEvent`` metadata.

Extracted from ``server_servicers.py`` so the servicer stays under the
500-line review cap (``scripts/checks/file_size.py``). The lifts are a
cohesive concern: ``ChannelMessageEvent`` carries some cross-cutting context
as first-class proto fields (because it has no metadata map), and the
agent-side read paths consume those values off ``AgentEvent.metadata`` keys.
This module reconciles the two at the single ``ReceiveChannelMessage``
boundary. The matching precedent is ``session_metadata`` / the Tier B
``salience_gate`` carve-outs.
"""

from __future__ import annotations

from .generated import task_pb2
from .persona_types import AgentEvent

# Byte bound on the lifted ``interaction_id``, the receive-side counterpart to
# the Go publish boundary's ``interactionIDMaxBytes`` (internal/channels/
# interaction_id.go). The value is seeded onto the metadata the per-interaction
# maps the layer PRs build (Layer 2 reply budget, Layer 4 end-of-interaction
# votes) key on, so an unbounded id is an unbounded map-key growth vector — the
# bound must hold at *this* seed point, not only at publish, because a non-Go
# (or compromised) producer can deliver an oversized wire field straight here.
# Measured in UTF-8 bytes to mirror Go's ``len()`` exactly (a value accepted at
# one boundary is accepted at the other); 128 reuses the *value* of the agent
# receive path's ``_CHANNEL_THREAD_ID_MAX_CHARS`` cap (that cap counts code
# points, this bound counts bytes — equal for the ASCII id) and is generous
# over the 36-byte uuid4.
_INTERACTION_ID_MAX_BYTES = 128


def seed_wire_metadata(
    event: AgentEvent, request: task_pb2.ChannelMessageEvent
) -> None:
    """Lift the typed ``ChannelMessageEvent`` wire fields onto the metadata
    keys the downstream read paths consume. ``cascade_depth`` is seeded at
    ``AgentEvent`` construction (it is always present as a proto3 scalar); the
    fields here are conditional — only a non-empty value is seeded, and
    ``interaction_id`` additionally only within ``_INTERACTION_ID_MAX_BYTES``
    (an over-length claim degrades to untracked). Whether a field actually
    carries a value depends on its producer: ``participant_type`` has one (the
    REST chat handler), ``interaction_id`` does not yet (see the per-field note
    below), so today only the former is ever seeded.
    """
    # ISSUE-0068: lift the sender's peer type off the typed proto field onto
    # the metadata key the episode-routing close path reads
    # (``sender_participant_type``) so a channel-delivered (REST) chat peer is
    # recorded as ``other_participant_type=user`` rather than the ``agent``
    # default. Only seed a non-empty value: an empty field is genuine
    # agent-to-agent traffic, which the read path resolves to ``agent``.
    # Reconciles the publish-side ``participant_type`` key with the read-side
    # ``sender_participant_type`` key at this single boundary.
    if request.sender_participant_type:
        event.metadata["sender_participant_type"] = request.sender_participant_type

    # RFC 0030 deterministic governance layers (v0.3.8), PR 1: lift the RFC
    # 0020 ``interaction_id`` (an opaque uuid4 token, not a ULID despite RFC
    # 0020 §D's wording — see ``agents/memory/interactions.py``) off the typed
    # proto field onto the event-metadata key the governance layers will read:
    # the Layer 1 PR will thread it toward the ``AcquireLease`` cost ceiling,
    # Layers 2/4 will key reply budgets and end-votes on it. Inert this PR:
    # nothing reads the key, and nothing populates the wire field either (no
    # orchestrator-side producer exists yet — interaction tracking is
    # agent-side), so ``request.interaction_id`` is empty on every publish
    # today and the branch below never fires. Only seed a non-empty value — an
    # empty field is the untracked / pre-v0.3.8 publish, which leaves every
    # layer at its uncapped default (the additive opt-in contract). Drop an
    # over-length claim to untracked (mirrors the Go publish boundary's
    # ``readInteractionID``): a clipped opaque token would key a *different*
    # interaction, so fall back to absent rather than truncate. See
    # ``_INTERACTION_ID_MAX_BYTES`` for why the bound must hold at this seed
    # point and not only at publish.
    interaction_id = request.interaction_id
    if interaction_id and len(interaction_id.encode("utf-8")) <= _INTERACTION_ID_MAX_BYTES:
        event.metadata["interaction_id"] = interaction_id


__all__ = ["seed_wire_metadata"]
