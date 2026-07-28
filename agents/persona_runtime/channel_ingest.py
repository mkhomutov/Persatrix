"""Inbound CHANNEL_MESSAGE sanitization seam (RFC 0011 PR 5).

Extracted from ``action_loop.py`` so the file stays under the 500-line
review-friendly limit and so the helper (which does not access agent
state) can be unit-tested as a pure function. Imported by the
``_ActionLoopMixin._sanitize_inbound_event`` delegation in
``action_loop.py``.

Why a free function: the helper takes an ``AgentEvent`` and returns an
``AgentEvent`` with no reference to ``self``. Keeping it as a method
implied access to agent state that is not actually used.
"""

from __future__ import annotations

import logging

from ..persona_types import AgentEvent, EventType
from ..security import CONTEXT_SOURCE_CHANNEL_MESSAGE, sanitize

__all__ = ["sanitize_inbound_event"]

logger = logging.getLogger(__name__)


def sanitize_inbound_event(event: AgentEvent) -> AgentEvent:
    """Run ``sanitize`` over inbound CHANNEL_MESSAGE content.

    Runs once on ingest so the LLM prompt, ``InteractionTracker``, and
    persistence path all see the cleared text. Non-channel events pass
    through unchanged. Returns a new ``AgentEvent`` with a copied
    payload when a substitution lands, so the content swap never leaks
    to callers holding the original — but the metadata dict is
    deliberately SHARED, not copied: mid-turn metadata stamps (the RFC
    0037 §G tripwire watch, written by ``_inject_memory_context``) must
    stay visible on the outer event the dispatch side lifts its
    ``DispatchContext`` from, and dispatch has already deep-copied
    metadata per target so intra-turn sharing is isolation-safe.

    In the v0.3.0 passthrough configuration the rebuild branch is
    unreachable — :func:`agents.security.sanitize` only substitutes
    content under ``SANITIZER_ACTION_QUARANTINE``, which is not yet
    wired through the agent config (RFC 0009 §C). The rebuild branch
    is the seam where a future quarantine-action wiring activates;
    today the early-return on byte-identical content always wins.
    (PR-263 review L-1.)
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE:
        return event
    payload = event.payload or {}
    content = payload.get("content", "")
    if not isinstance(content, str):
        # PR-263 review L-3: a future bridge wire bug delivering
        # non-str content (forgotten ``.decode()``, missing-field
        # ``None``) would otherwise silently bypass ``sanitize()``'s
        # WARN audit signal — the only operator-visible signal until
        # the Go-side audit chain is wired for inbound channel
        # messages (RFC 0009 §G). Surface the wire bug at the seam.
        logger.warning(
            "channel-message content has non-string type %s; "
            "skipping sanitize()",
            type(content).__name__,
        )
        return event
    if not content:
        # Empty-string content is a valid wire payload, not a wire
        # bug — nothing to flag, no operator signal owed.
        return event
    result = sanitize(content, source=CONTEXT_SOURCE_CHANNEL_MESSAGE)
    # Skip rebuild whenever content is byte-identical: the flag state
    # is not propagated through the AgentEvent (it lives on the WARN
    # log line in ``security.sanitize`` and the Go-side audit chain),
    # so a flagged-but-passthrough message has nothing to rebuild.
    if result.content == content:
        return event
    new_payload = dict(payload)
    new_payload["content"] = result.content
    return AgentEvent(
        event_type=event.event_type,
        payload=new_payload,
        channel_id=event.channel_id,
        sender_id=event.sender_id,
        message_id=event.message_id,
        thread_id=event.thread_id,
        timestamp=event.timestamp,
        # Shared, not copied — mid-turn stamps (§G tripwire watch) must
        # land on the dict the outer event holds (see docstring).
        metadata=event.metadata,
    )
