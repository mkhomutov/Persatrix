"""Task-local binding of the acting channel's §A classification.

RFC 0037 §C (v0.3.12 PR 3) — the identity write-through fires at the
``store_note`` tool boundary (:mod:`agents.tools.identity_write_through`),
which runs deep inside the action loop with no handle on the originating
event; the §C rule it must apply ("the cross-room write-through proceeds
only when the acting classification is ≤ ``internal``") therefore needs
the acting level recoverable without threading the event through tool
dispatch.  This module binds that one value as a task-local
:class:`~contextvars.ContextVar` for the lifetime of an event handler —
the exact :mod:`agents.sender_type` precedent, folded into
:func:`agents.request_scope.request_scope_from_metadata` beside the
session / principal / epoch / sender-type axes.

The bound value is the VERBATIM wire classification the PR 2 ingress
seams seeded onto ``AgentEvent.metadata``
(:mod:`agents.channel_event_classification` owns the key and the
seed-verbatim rationale).  No lattice import, no allowlist, no default is
applied here — §A splits fail-closed into three direction-flipping rules,
each owned by exactly one named resolver in
``agents/persona_runtime/classification.py``, and this module must stay
importable from the executor entry points (``request_scope``), which must
not grow a hard dep on the persona subpackage.  An unbound context reads
as ``None``, which every consumer resolves through the rule it is
applying (the write-through via ``acting_at_or_below_internal`` — rule
(b)'s ``public`` floor, so a tick / CLI / pre-v0.3.12 turn proceeds
exactly as before).

The RFC 0037 PR 4 ``classification_scope(L)`` turn entry (the §D gate's
acting level + the autonomous-tick ``public`` floor) builds on this same
seam rather than a second contextvar.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from .channel_event_classification import CHANNEL_CLASSIFICATION_METADATA_KEY

__all__ = [
    "acting_classification_scope_from_metadata",
    "current_acting_classification",
]

_ACTIVE_ACTING_CLASSIFICATION: ContextVar[str | None] = ContextVar(
    "active_acting_classification", default=None,
)


def current_acting_classification() -> str | None:
    """Return the acting channel's wire classification bound for this
    event, or ``None`` when unbound / the event carried none.

    ``None`` — never ``""`` — so the return feeds the rule-(b) resolvers
    (``acting_rank`` / ``acting_at_or_below_internal``) directly; their
    ``None`` arm IS the §A ``public`` acting floor.
    """
    return _ACTIVE_ACTING_CLASSIFICATION.get()


@contextmanager
def acting_classification_scope_from_metadata(
    metadata: Mapping[str, object],
) -> Iterator[None]:
    """Bind the acting classification from an event's metadata.

    A no-op (binds nothing) when the
    :data:`~agents.channel_event_classification.CHANNEL_CLASSIFICATION_METADATA_KEY`
    key is absent or carries a non-string / blank value — the PR 2 seed
    already enforces the byte bound and never writes an empty value, so
    this re-check is the same tolerant-reader posture as
    ``wire_channel_classification``.  Restores the previous value on
    exit, including on exception, via the saved
    :class:`contextvars.Token`.
    """
    raw = metadata.get(CHANNEL_CLASSIFICATION_METADATA_KEY) if metadata else None
    if not isinstance(raw, str) or not raw:
        yield
        return
    token = _ACTIVE_ACTING_CLASSIFICATION.set(raw)
    try:
        yield
    finally:
        _ACTIVE_ACTING_CLASSIFICATION.reset(token)
