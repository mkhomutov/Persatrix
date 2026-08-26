"""The multi-turn ingest payload — one builder, two consumers.

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

if TYPE_CHECKING:
    from ..persona_types import AgentEvent

__all__ = ["build_turn_payload"]


def build_turn_payload(event: AgentEvent, summary: str) -> dict[str, Any]:
    """The in-memory turn payload for a multi-turn ``event``.

    The per-turn ``summary`` / envelope ride the turn so the RFC 0020
    PR 4 summariser sees the legacy single-row episode fields.

    ISSUE-0054 — RFC 0026's facts extractor needs the real message
    body: the combined summarise + extract LLM call at interaction
    close extracts zero facts when fed only the deterministic action
    envelope.  The body rides the in-memory turn under ``text`` and is
    stripped before persistence by ``persist_closed_interaction`` so
    ``context_json`` stays body-free per RFC 0020 §D.
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
    message_text = (event.payload or {}).get("content")
    if isinstance(message_text, str) and message_text.strip():
        payload["text"] = message_text
    return payload
