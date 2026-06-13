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

# The close triggers the orchestrator's resolver actually stamps onto
# ``previous_interaction_close_trigger`` (producer plan OQ 5) — the §L
# instrument vocabulary, mirroring Go's ``idleTrigger`` /
# ``endVotesTrigger`` (``internal/channels/interaction_resolver.go`` /
# ``end_vote.go``; pinned by the cross-language drift test). Allowlisted at
# this seed point because the value drives the close *reason* persisted on
# the local interaction record: an unrecognised string from a non-Go (or
# compromised) producer must degrade to the legacy label, never ride into
# ``close_reason`` verbatim.
#
# Public (PR 607 second-pass review): this module is the single Python
# source of the trigger vocabulary — the rotation-close seam
# (``persona_runtime/interaction_boundary.py``) imports the idle value
# instead of re-declaring it, so growing the vocabulary (the reserved
# ``cost`` trigger) is one edit per language, held to Go by one drift pin.
WIRE_CLOSE_TRIGGER_IDLE = "idle"
WIRE_CLOSE_TRIGGER_END_VOTES = "end_votes"
WIRE_CLOSE_TRIGGERS = frozenset({
    WIRE_CLOSE_TRIGGER_IDLE,
    WIRE_CLOSE_TRIGGER_END_VOTES,
})


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
    payload: dict[str, object] = {
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
        # Chair-stall-escalation amendment (§C item 2): the forced-turn
        # marker. The gate admits a marked event down the directed lane
        # (strict ``is True``) and the escalation framing renders into the
        # turn's user message; false is every ordinary dispatch.
        "chair_escalation": request.chair_escalation,
    }
    # End-vote-close-propagation amendment (CP3): the close-notification
    # marker — seeded ONLY when the typed field is set, unlike
    # ``chair_escalation``'s unconditional lift, because the committed
    # acceptance pins key-ABSENCE on ordinary traffic (the typed-field-only
    # negative: an over-broad copy that grew the key on every event would
    # erase the marked/unmarked distinction the strict consumers key on).
    # The gate refuses a marked event pre-LLM (reason
    # ``close_notification``) and the suppress path's close dispatch
    # closes the local tracker with the structural ("ended") cause.
    if request.interaction_close_notification:
        payload["interaction_close_notification"] = True
    # Chair-stall-escalation resynthesize variant (ISSUE-0099): a REFINEMENT
    # of ``chair_escalation`` — the orchestrator sets both on the second
    # forced turn, after the chair's first hand-off provably reached nobody.
    # Seeded typed-field-only (like ``interaction_close_notification`` above,
    # not ``chair_escalation``'s unconditional copy) so ordinary traffic keeps
    # key-ABSENCE: the framing selector reads strict ``is True``, and the lift
    # itself still rides ``chair_escalation``, so this key only ever flips the
    # persona to the synthesize-only snippet.
    if request.chair_escalation_resynthesize:
        payload["chair_escalation_resynthesize"] = True
    return payload


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
    # The OQ 5 pair is seeded by the shared core below; see its notes.
    _seed_validated_interaction_keys(
        event.metadata,
        interaction_id=request.interaction_id,
        prev_id=request.previous_interaction_id,
        prev_trigger=request.previous_interaction_close_trigger,
    )


def _seed_validated_interaction_keys(
    metadata: dict[str, object],
    *,
    interaction_id: str,
    prev_id: str,
    prev_trigger: str,
) -> None:
    """The shared validation core behind both wire-ingress paths (the live
    gRPC lift above and the catch-up replay's :func:`seed_replay_metadata`).

    ``interaction_id``: only a non-empty value within
    ``_INTERACTION_ID_MAX_BYTES`` — an over-length claim degrades to
    untracked rather than truncating (a clipped opaque token would key a
    *different* interaction).

    Producer plan OQ 5: the retired predecessor's id + close trigger, the
    close-cause attribution the rotation-close seam
    (``persona_runtime/interaction_boundary.py``) uses to label the local
    boundary truthfully (``idle_gap`` vs ``structural``). Seeded only as a
    validated PAIR — the trigger is meaningless without the id it
    attributes (the seam applies it only when the id matches the wire id
    its open record was opened under), and a lone trigger could mislabel a
    mismatched generation. The id gets the same byte bound as
    ``interaction_id``; the trigger must be in the resolver's §L
    vocabulary. Anything else — absent (old orchestrator, fresh channel,
    post-restart re-mint), oversized, or unrecognised — seeds nothing and
    the rotation close keeps its pre-OQ5 structural label (the
    mixed-version contract).
    """
    if interaction_id and len(interaction_id.encode("utf-8")) <= _INTERACTION_ID_MAX_BYTES:
        metadata["interaction_id"] = interaction_id
    if (
        prev_id
        and len(prev_id.encode("utf-8")) <= _INTERACTION_ID_MAX_BYTES
        and prev_trigger in WIRE_CLOSE_TRIGGERS
    ):
        metadata["previous_interaction_id"] = prev_id
        metadata["previous_interaction_close_trigger"] = prev_trigger


def seed_replay_metadata(
    metadata: dict[str, object], row_metadata: object,
) -> None:
    """Lift the validated wire interaction keys off a persisted REST
    history row onto a catch-up replay event's metadata (PR 607
    second-pass review).

    The history response (``messageToResponse``) returns the
    router-stamped metadata bag verbatim, so the same three keys the live
    path receives as typed proto fields arrive here as untyped JSON —
    re-validated with exactly the live seed point's rules (byte bound,
    pair-or-nothing, trigger allowlist), with non-string values reading
    as absent.  Without this lift a replayed span covering a vote-closed
    conversation and the channel's next topic merges into one local
    record, and the merged record opens with no wire id so the first
    LIVE id reads as adoption-not-rotation — the close propagation
    silently disarmed after every restart.  Rows persisted before
    v0.3.8 carry none of the keys and replay exactly as before.
    """
    if not isinstance(row_metadata, dict):
        return

    def _str(key: str) -> str:
        value = row_metadata.get(key, "")
        return value if isinstance(value, str) else ""

    _seed_validated_interaction_keys(
        metadata,
        interaction_id=_str("interaction_id"),
        prev_id=_str("previous_interaction_id"),
        prev_trigger=_str("previous_interaction_close_trigger"),
    )


__all__ = [
    "WIRE_CLOSE_TRIGGER_END_VOTES",
    "WIRE_CLOSE_TRIGGER_IDLE",
    "WIRE_CLOSE_TRIGGERS",
    "channel_event_payload",
    "seed_replay_metadata",
    "seed_wire_metadata",
]
