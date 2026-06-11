"""Lift typed ``ChannelMessageEvent`` wire fields onto the ``AgentEvent``.

Extracted from ``server_servicers.py`` so the servicer stays under the
500-line review cap (``scripts/checks/file_size.py``). The lifts are a
cohesive concern: ``ChannelMessageEvent`` carries the channel context as
first-class proto fields (because it has no metadata map), and the
agent-side read paths consume those values off the ``AgentEvent`` payload
(:func:`channel_event_payload` — the response gate / salience seam inputs)
and metadata keys (:func:`seed_wire_metadata` — cross-cutting context).
This module reconciles the wire and event shapes at the single
``ReceiveChannelMessage`` boundary. The matching precedent is
``session_metadata`` / the Tier B ``salience_gate`` carve-outs.
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


def channel_event_payload(request: task_pb2.ChannelMessageEvent) -> dict[str, object]:
    """Build the CHANNEL_MESSAGE ``AgentEvent.payload`` from the wire event.

    The payload carries the receiver-side decision inputs in the shapes
    their consumers read:

    * ``mentions`` / ``respond_policy`` / ``thread_parent_sender_id`` — the
      RFC 0011 PR 4b response-gate inputs (``agents/response_gate.py``).
    * ``salience_gated`` / ``threshold`` / ``channel_size`` /
      ``salience_max_channel_members`` — the RFC 0030 Tier B salience-bid
      inputs (``agents/persona_runtime/salience_gate.py``); ``threshold``
      is ``None`` when absent, a tri-state distinct from an explicit 0.0.
    * ``floor_mentions`` / ``floor_mentions_resolved`` — the
      floor-capable-directedness amendment (v0.3.8): the
      orchestrator-resolved Tier A suppression basis plus its
      producer-presence flag. The gate keys the basis switch on the flag,
      never on the list's emptiness (proto3 repeated fields have no
      presence; a resolved *empty* subset is the motivating human-mention
      case). An old orchestrator leaves the flag at the proto3-default
      ``False`` and the gate falls back to the raw ``mentions`` basis.
    """
    return {
        "content": request.content,
        "channel_type": request.channel_type,
        "mentions": list(request.mentions),
        "respond_policy": request.respond_policy,
        "thread_parent_sender_id": request.thread_parent_sender_id,
        "salience_gated": request.salience_gated,
        "threshold": request.threshold if request.HasField("threshold") else None,
        "channel_size": request.channel_size,
        "salience_max_channel_members": request.salience_max_channel_members,
        "floor_mentions": list(request.floor_mentions),
        "floor_mentions_resolved": request.floor_mentions_resolved,
    }


def seed_wire_metadata(
    event: AgentEvent, request: task_pb2.ChannelMessageEvent
) -> None:
    """Lift the typed ``ChannelMessageEvent`` wire fields onto the metadata
    keys the downstream read paths consume. ``cascade_depth`` is seeded at
    ``AgentEvent`` construction (it is always present as a proto3 scalar); the
    fields here are conditional — only a non-empty value is seeded, and
    ``interaction_id`` additionally only within ``_INTERACTION_ID_MAX_BYTES``
    (an over-length claim degrades to untracked). Whether a field actually
    carries a value depends on its producer: ``participant_type``'s is the
    REST chat handler; ``interaction_id``'s is the orchestrator's interaction
    resolver, live since the interaction-id producer plan PR 1 (see the
    per-field note below), so both are seeded on routed traffic today.
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
    # proto field onto the event-metadata key the governance layers read.
    # Both ends are live now: the orchestrator's resolver stamps the id onto
    # every routed publish and the dispatcher lifts it onto the wire field
    # (interaction-id producer plan PR 1), and the loop-side read —
    # ``wallet_cause.lease_interaction_id_for_event`` (producer plan PR 2) —
    # threads the seeded key into the channel-path leases, while Layers 2/4
    # key reply budgets and end-votes on the same id orchestrator-side. So
    # this branch fires on every routed publish. Only seed a non-empty value — an
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


__all__ = ["channel_event_payload", "seed_wire_metadata"]
