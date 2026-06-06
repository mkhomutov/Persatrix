"""Combined per-request scope binding (ISSUE-0081 PR 2/3 · ISSUE-0085 PR 4).

:meth:`agents.persona_runtime._LLMPersonaAgent.on_event` is the single
universal recall chokepoint for every inbound path (the synchronous
``SendChatMessage`` and the fire-and-forget ``ReceiveChannelMessage``
EventLoop drain).  All three scope axes — session (PR 2), tenant/principal
(PR 3), and epoch (ISSUE-0085 PR 4) — must be bound there for the
handler's lifetime so the recall + write seams inside ``_on_event_inner``
resolve to *this* request's ``(session, principal, epoch)`` even when a
sibling conversation runs concurrently in-process.

This helper folds the three axis-specific binders into **one** context
manager so ``on_event`` has a single binding site (and so the
persona-runtime module stays under the file-size cap).  Each underlying
binder yields a :func:`~contextlib.nullcontext` when its event-metadata
key is absent, so a tick event carrying none binds nothing and call-time
resolution falls back to the tiers' construction snapshots — leaving the
single-session / single-tenant / single-world / CLI / tick paths
unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager

from .epoch_id import epoch_scope_from_metadata
from .principal_id import principal_scope_from_metadata
from .sender_type import sender_type_scope_from_metadata
from .session_id import session_scope_from_metadata


@contextmanager
def request_scope_from_metadata(
    metadata: Mapping[str, object],
) -> Iterator[None]:
    """Bind the session, principal, epoch **and** sender-type scopes for an
    event's life.

    Enters :func:`agents.session_id.session_scope_from_metadata`,
    :func:`agents.principal_id.principal_scope_from_metadata`,
    :func:`agents.epoch_id.epoch_scope_from_metadata` and
    :func:`agents.sender_type.sender_type_scope_from_metadata` together via
    an :class:`~contextlib.ExitStack` so all are restored on exit (including
    on exception).  A no-op for any axis whose key is absent.

    The sender-type binding (RFC 0031 amendment, F-7 Option D, ISSUE-0093
    PR D2) carries the inbound sender's participant type to the identity
    write-through at the ``store_note`` tool boundary, so a ``contact:<id>``
    note's identity lands on the same relationship row the recall side
    later queries.
    """
    with ExitStack() as stack:
        stack.enter_context(session_scope_from_metadata(metadata))
        stack.enter_context(principal_scope_from_metadata(metadata))
        stack.enter_context(epoch_scope_from_metadata(metadata))
        stack.enter_context(sender_type_scope_from_metadata(metadata))
        yield
