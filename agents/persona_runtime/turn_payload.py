"""The multi-turn ingest shapes — what a turn carries, and what it opens.

Two builders, both translating an :class:`~agents.persona_types.AgentEvent`
into what the tracker is handed: :func:`build_turn_payload` for the turn
itself, :func:`frozen_open_capture` for the fields a turn freezes onto the
record when it is the one that OPENS it.

Extracted from ``_EpisodeRoutingMixin._handle_multi_turn_event``
(v0.3.15 residuals PR 3) so the close-notification room fan
(:mod:`.close_notification`) can land the closing message as the final
turn of EVERY open record in the scope without routing through the
per-event path — which would deliver it to the sender's
``(principal, speaker)`` key alone, or fabricate a fresh record where
the sender has none.  Keeping the construction in one place pins the
shape both consumers feed the close-path summariser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from ..channel_event_classification import wire_channel_classification
from ..memory.interaction_types import ROOM_CLOSE_TURN_KEY
from ..principal_id import principal_id_from_metadata

if TYPE_CHECKING:
    from ..persona_types import AgentEvent

__all__ = [
    "FrozenOpenCapture",
    "build_turn_payload",
    "frozen_open_capture",
    "replay_markers",
]


def replay_markers(event: AgentEvent) -> tuple[bool, bool]:
    """``(replayed, replay_attributed)`` for ``event`` — the ISSUE-0130 pair.

    ONE spelling, because two consumers have to agree on it exactly:
    :func:`frozen_open_capture` freezes the pair onto the record a turn
    OPENS, and
    :func:`~agents.persona_runtime.interaction_boundary.stale_close_reason`
    compares the arriving event's pair against the open record's to decide
    whether they may share a span.  If those two drifted, the split would
    either fire on every turn (fragmenting every span into one-turn
    records) or never fire at all (which is the merge both guards exist to
    prevent) — and neither shows up as a type error.

    ``replay_attributed`` is conjoined with ``replayed`` here rather than
    left to each reader.  The live gRPC ingress seeds the same principal
    key, so an unconditional expression marks essentially every
    authenticated live record ``replay_attributed=True`` — a state the
    field's own contract calls meaningless, and one that reads as "was
    authenticated" instead.  Making the pair unrepresentable costs one
    ``and`` (v0.3.15 PR B2 review).
    """
    replayed = event.metadata.get("replay_mode") is True
    return (
        replayed,
        replayed and principal_id_from_metadata(event.metadata) is not None,
    )


class FrozenOpenCapture(TypedDict):
    """The keyword set :func:`frozen_open_capture` hands the tracker.

    Declared rather than returned as ``dict[str, Any]`` because the call
    site unpacks it (``**frozen_open_capture(event)``) into
    :meth:`~agents.memory.interaction_tracker.InteractionTracker.add_turn`,
    and mypy checks NEITHER key names nor value types through an ``Any``
    splat — while every one of these five has a default on ``add_turn``,
    so a dropped or misspelled key silently takes that default instead of
    raising.  A typo'd ``replayed`` would make every catch-up-opened
    record look live and disarm the ISSUE-0130 skip with a green type
    check (v0.3.15 PR B2 review).  A ``TypedDict`` restores the checking
    the inline keywords used to get.
    """

    classification: str | None
    source_channel_id: str | None
    replayed: bool
    replay_attributed: bool
    speaker_id: str | None


def build_turn_payload(
    event: AgentEvent, summary: str, *, room_close: bool = False,
) -> dict[str, Any]:
    """The in-memory turn payload for a multi-turn ``event``.

    The per-turn ``summary`` / envelope ride the turn so the RFC 0020
    PR 4 summariser sees the legacy single-row episode fields.

    ISSUE-0054 — RFC 0026's facts extractor needs the real message
    body: the combined summarise + extract LLM call at interaction
    close extracts zero facts when fed only the deterministic action
    envelope.  The body rides the in-memory turn under ``text`` and is
    stripped before persistence by ``persist_closed_interaction`` so
    ``context_json`` stays body-free per RFC 0020 §D.

    ``room_close`` stamps :data:`ROOM_CLOSE_TURN_KEY` — set by the
    close-notification room fan, whose one closing message becomes the
    final turn of EVERY sibling record and is therefore the RFC 0020 §G
    exception to single-speaker construction.  The per-event ingest
    leaves it unset: that turn belongs to its own sender's record.
    """
    payload: dict[str, Any] = {
        "summary": summary,
        "event_type": event.event_type.value,
        "sender": event.sender_id,
        "channel_id": event.channel_id,
        "timestamp": event.timestamp,
        # RFC 0020 PR 4: stash sender's participant_type so the
        # close-path ``record_interaction`` can carry the correct
        # ``other_participant_type`` (defaults "agent" downstream).
        "participant_type": event.metadata.get(
            "sender_participant_type", "agent",
        ),
    }
    # ISSUE-0130 shape (b): the wire message id, carried for the replay
    # span identity (:mod:`.replay_identity`) and — like ``text`` above —
    # STRIPPED before the turn reaches ``context_json``, so RFC 0020 §D's
    # "the episodic store is not a message log" stays true and the
    # FTS-indexed column gains no wire ids.  Transient by construction.
    if event.message_id:
        payload["message_id"] = event.message_id
    message_text = (event.payload or {}).get("content")
    if isinstance(message_text, str) and message_text.strip():
        payload["text"] = message_text
    if room_close:
        payload[ROOM_CLOSE_TURN_KEY] = True
    return payload


def frozen_open_capture(event: AgentEvent) -> FrozenOpenCapture:
    """The record fields ``event`` freezes if its turn OPENS the record.

    Extracted from ``_EpisodeRoutingMixin._handle_multi_turn_event``
    (v0.3.15 PR B2) — that module was at the 500-line cap, and this is
    the set the cap kept growing: four captures before this PR, five
    after.  Naming the set is worth more than the lines, because they
    share a rule that is invisible when they are spelled inline among
    ordinary keyword arguments: the tracker honours every one of them
    **only on open**, so a later turn arriving on an already-open record
    cannot relabel it.  ``speaker_id`` is in the set for a different
    reason — it is a KEY axis, so it is only-on-open trivially: a
    different speaker is a different record.

    * ``classification`` / ``source_channel_id`` — RFC 0037 §C, the
      acting channel's verbatim wire classification (through the shared
      drift-pinned reader) and its id.
    * ``replayed`` — ISSUE-0130: this turn came from the on-startup
      catch-up replay, not a live dispatch.
    * ``replay_attributed`` — ISSUE-0130 shape (b): and it carried a
      persisted principal (channel-store v12), so the span it opens may
      derive under that tenant instead of being skipped as
      unattributable.  Read off the metadata rather than the record's
      resolved ``principal_id`` because a seeded ``"local"`` and an
      unseeded default are the same value there — the presence is the
      whole signal, and this is the last point that still has it.  Both
      come from :func:`replay_markers`, shared with the boundary that
      splits on a disagreement.
    * ``speaker_id`` — ISSUE-0131: the turn lands in ITS sender's
      record.  The principal half of the key resolves ambient (the
      ``on_event`` request scope) inside the tracker.
    """
    replayed, replay_attributed = replay_markers(event)
    return {
        "classification": wire_channel_classification(event),
        "source_channel_id": event.channel_id or None,
        "replayed": replayed,
        "replay_attributed": replay_attributed,
        "speaker_id": event.sender_id,
    }
