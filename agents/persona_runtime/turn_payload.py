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

from typing import TYPE_CHECKING, Any

from ..channel_event_classification import wire_channel_classification
from ..memory.interaction_types import ROOM_CLOSE_TURN_KEY
from ..principal_id import principal_id_from_metadata

if TYPE_CHECKING:
    from ..persona_types import AgentEvent

__all__ = ["build_turn_payload", "frozen_open_capture"]


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


def frozen_open_capture(event: AgentEvent) -> dict[str, Any]:
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
      whole signal, and this is the last point that still has it.
    * ``speaker_id`` — ISSUE-0131: the turn lands in ITS sender's
      record.  The principal half of the key resolves ambient (the
      ``on_event`` request scope) inside the tracker.
    """
    return {
        "classification": wire_channel_classification(event),
        "source_channel_id": event.channel_id or None,
        "replayed": event.metadata.get("replay_mode") is True,
        "replay_attributed": (
            principal_id_from_metadata(event.metadata) is not None
        ),
        "speaker_id": event.sender_id,
    }
