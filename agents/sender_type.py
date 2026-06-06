"""Task-local binding of the inbound event sender's *participant type*.

RFC 0031 amendment (F-7 Option D, ISSUE-0093) **PR D2** — the identity
write-through fires at the ``store_note`` tool boundary
(:func:`agents.tools.builtin.create_memory_tools`), which runs deep
inside the action loop with no handle on the originating event.  The
relationship row identity lands on is keyed by ``other_participant_type``
(``"user"`` vs ``"agent"``), and the *recall* side already reads that
type from ``event.metadata["sender_participant_type"]``
(:func:`agents.persona_runtime.relationship_section.recall_relationship_summary`).
For the write to land on the **same** row the read later queries, the
write must use the same participant type.

This module binds that one value as a task-local :class:`ContextVar` for
the lifetime of an event handler — mirroring the session / principal /
epoch scopes folded into
:func:`agents.request_scope.request_scope_from_metadata` — so the
write-through can recover it without threading the event through tool
dispatch.  When the key is absent (a tick event, a CLI turn, an
agent-to-agent message that carries no type) resolution falls back to
``"agent"``, matching the recall side's identical default.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "SENDER_PARTICIPANT_TYPE_KEY",
    "current_sender_type",
    "normalize_sender_type",
    "sender_type_scope_from_metadata",
]

#: Event-metadata key carrying the sender's participant type.  Shared with
#: the recall side (which reads the same key off ``event.metadata``).
SENDER_PARTICIPANT_TYPE_KEY = "sender_participant_type"

#: Default when no type is bound — matches the recall-side default in
#: ``relationship_section`` so write and read agree on the same row.
_DEFAULT_SENDER_TYPE = "agent"

_ACTIVE_SENDER_TYPE: ContextVar[str | None] = ContextVar(
    "active_sender_type", default=None,
)


def normalize_sender_type(raw: object) -> str:
    """Resolve a raw ``sender_participant_type`` metadata value to the
    participant type used as the relationship-row key.

    The single rule both sides of the identity flow funnel through — the
    write side via :func:`sender_type_scope_from_metadata` (which binds the
    normalized value), and the read side via
    :func:`agents.persona_runtime.relationship_section.recall_relationship_summary`
    (which normalizes ``event.metadata`` directly, since the recall runs
    with the originating event in hand).  Sharing the rule guarantees the
    write and the later read resolve to the *same* row: a non-string or
    blank value falls back to :data:`_DEFAULT_SENDER_TYPE`, and surrounding
    whitespace is stripped, so e.g. ``" user "`` and ``"user"`` are not two
    different rows.
    """
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _DEFAULT_SENDER_TYPE


def current_sender_type() -> str:
    """Return the participant type bound for this event, or ``"agent"``.

    Reads the task-local :class:`ContextVar` only (never an env var); an
    unbound context resolves to :data:`_DEFAULT_SENDER_TYPE`, exactly the
    fallback the recall side applies, so the identity write and the later
    identity read query the same relationship row.
    """
    return _ACTIVE_SENDER_TYPE.get() or _DEFAULT_SENDER_TYPE


@contextmanager
def sender_type_scope_from_metadata(
    metadata: Mapping[str, object],
) -> Iterator[None]:
    """Bind the sender participant type from an event's metadata.

    A no-op (binds nothing) when the
    :data:`SENDER_PARTICIPANT_TYPE_KEY` key is absent or carries a
    non-string / blank value, so a tick / CLI / typeless event leaves
    :func:`current_sender_type` at its ``"agent"`` default.  Restores the
    previous value on exit, including on exception, via the saved
    :class:`contextvars.Token`.
    """
    raw = metadata.get(SENDER_PARTICIPANT_TYPE_KEY) if metadata else None
    if not isinstance(raw, str) or not raw.strip():
        yield
        return
    token = _ACTIVE_SENDER_TYPE.set(normalize_sender_type(raw))
    try:
        yield
    finally:
        _ACTIVE_SENDER_TYPE.reset(token)
