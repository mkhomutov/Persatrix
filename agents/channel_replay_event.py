"""The replayed ``CHANNEL_MESSAGE`` event — one history row, one event.

Moved out of :mod:`agents.channel_catchup` (v0.3.15 PR B2): that module
sat at exactly the 500-line cap ``scripts/checks/file_size.py --strict``
enforces, and this builder is where ISSUE-0130 shape (b) lands its
principal seed with the rationale the seed cannot be read without.  The
fetcher keeps the transport — channel list, membership, history paging,
the wall-clock budget; this module owns the translation step between
them: turning one JSON row into the event shape ``ReceiveChannelMessage``
produces on the live path.

That symmetry is the module's whole contract.  A replayed event must be
indistinguishable from a live one except where it is deliberately marked
(``replay_mode``), because every downstream consumer — the tracker key,
the scope router, the close path — is written once for both.
"""

from __future__ import annotations

from typing import Any

from .channel_event_classification import seed_channel_classification
from .channel_validation import parse_channel_timestamp
from .channel_wire_metadata import seed_replay_metadata
from .persona_types import AgentEvent, EventType
from .principal_id import seed_principal_metadata

__all__ = ["build_replay_event"]
def build_replay_event(
    msg: dict,
    channel_id: str,
    respond_policy: str,
    channel: dict,
) -> AgentEvent:
    """Build a CHANNEL_MESSAGE ``AgentEvent`` matching the shape that
    ``ReceiveChannelMessage`` produces on the live path.

    ``metadata["replay_mode"] = True`` is the marker the action-loop
    short-circuit reads; without it the runtime would treat the row as
    live traffic and fire the LLM.

    PR-265 review S2: wire ``msg["timestamp"]`` (RFC 3339, set by the
    orchestrator at publish time — see
    ``internal/server/channel_types.go::channelMessageResponse``) is
    parsed to epoch seconds and forwarded into
    ``AgentEvent.timestamp``. Without this, replayed events default to
    ``time.time()`` at boot, defeating RFC 0021 P1 now-anchor / recency
    rendering, poisoning ``Turn.payload["timestamp"]``, and writing
    wrong ``started_at`` on episodic rows. Shared parser with
    ``validate_channel_message_event``.

    Fallback (post-PR-265 L1 second pass): malformed timestamps cannot
    reach this function — the catch-up loop runs every row through
    ``validate_channel_message_dict`` first. The ``parsed_ts is None``
    branch below is defense-in-depth against an impossible state.

    PR-265 review L2: ``thread_parent_sender_id`` is intentionally
    **not** propagated. The field exists on the live proto but **not**
    on ``channelMessageResponse`` JSON shape — nothing to forward.
    Documented gap, not a defect: the only in-tree consumer (the
    response gate) is bypassed by the replay short-circuit. Future
    threading-aware consumers will need a Go-side schema bump.

    PR 607 second-pass review: the row's wire interaction keys
    (``interaction_id`` + the OQ 5 close-cause pair) ARE propagated,
    re-validated by :func:`agents.channel_wire_metadata
    .seed_replay_metadata` with the live seed point's exact rules.
    Without them a replayed span covering a vote-closed conversation
    and the channel's next topic merges into one local record, and the
    merged record opens with no wire id — the first LIVE id then reads
    as adoption-not-rotation, silently disarming the RFC 0030 close
    propagation after every restart.  Rotation still SEGMENTS the
    replayed spans; what it no longer does is derive from them —
    ISSUE-0130 skips the close-path summariser for every replay-opened
    span, which has no principal to attribute a summary to.

    ISSUE-0130 shape (b) — the principal seed (v0.3.15 PR B2).  The row's
    ``principal_id`` (channel-store v12, stamped server-side at publish)
    is seeded onto the SAME metadata key the live gRPC ingress writes, so
    ``on_event``'s existing binder puts a replayed turn in the tenant that
    caused the message rather than in the persona's default.  This is what
    narrows the shape-(a) derivation skip from *every* replayed span to
    the genuinely unattributable ones — see
    :func:`agents.principal_id.seed_principal_metadata` for why the
    field's PRESENCE, not its value, is the question, and
    :func:`agents.persona_runtime.close_path.persist_closed_interaction`
    for what the answer buys.

    The seed is deliberately the LAST thing this builder does with the
    row, and it takes nothing else from it: no correlation key, no
    caller-supplied tenant.  There is none to take — ``principal_id`` is
    response-only on ``channelMessageResponse`` and the publish request
    struct has no counterpart, so the value can only be the
    orchestrator's own.
    """
    payload: dict[str, Any] = {
        "content": msg.get("content", ""),
        "channel_type": channel.get("channel_type", ""),
        "mentions": list(msg.get("mentions") or []),
        "respond_policy": respond_policy,
    }
    raw_ts = msg.get("timestamp")
    parsed_ts = (
        parse_channel_timestamp(raw_ts) if isinstance(raw_ts, str) else None
    )
    metadata: dict[str, Any] = {"replay_mode": True}
    seed_replay_metadata(metadata, msg.get("metadata"))
    # RFC 0037 §B (v0.3.12 PR 2): stamp the channel's §A classification from
    # the channel-list object already threaded here — the REST leg of the
    # "both delivery paths carry the field" contract, mirroring the live
    # path's typed-field seed. A pre-v0.3.12 orchestrator's JSON has no such
    # key and seeds nothing (the read-side `public` floor, §A rule (b)).
    seed_channel_classification(metadata, channel.get("classification"))
    # ISSUE-0130 (b): the tenant the orchestrator attributed the publish to
    # (channel-store v12).  Absent = a pre-v12 orchestrator, and the span
    # stays unattributable; ``"local"`` present is a real answer and is
    # attributed.  The persona's ``on_event`` binds it for the ingest.
    seed_principal_metadata(metadata, msg.get("principal_id"))
    event_kwargs: dict[str, Any] = {
        "event_type": EventType.CHANNEL_MESSAGE,
        "payload": payload,
        "channel_id": channel_id,
        "sender_id": msg.get("sender_id"),
        "message_id": msg.get("id"),
        "thread_id": msg.get("thread_id") or None,
        "metadata": metadata,
    }
    if parsed_ts is not None:
        event_kwargs["timestamp"] = parsed_ts
    return AgentEvent(**event_kwargs)
