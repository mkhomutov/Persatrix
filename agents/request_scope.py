"""Combined per-request scope binding (ISSUE-0081 PR 2 + PR 3).

:meth:`agents.persona_runtime._LLMPersonaAgent.on_event` is the single
universal recall chokepoint for every inbound path (the synchronous
``SendChatMessage`` and the fire-and-forget ``ReceiveChannelMessage``
EventLoop drain).  Both the session axis (PR 2) and the tenant/principal
axis (PR 3) must be bound there for the handler's lifetime so the recall
+ write seams inside ``_on_event_inner`` resolve to *this* conversation's
``(session, principal)`` even when a sibling conversation runs
concurrently in-process.

This helper folds the two axis-specific binders into **one** context
manager so ``on_event`` has a single binding site (and so the
persona-runtime module stays under the file-size cap).  Each underlying
binder yields a :func:`~contextlib.nullcontext` when its event-metadata
key is absent, so a tick event carrying neither binds nothing and
call-time resolution falls back to the tiers' construction snapshots —
leaving the single-session / single-tenant / CLI / tick paths unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager

from .principal_id import principal_scope_from_metadata
from .session_id import session_scope_from_metadata


@contextmanager
def request_scope_from_metadata(
    metadata: Mapping[str, object],
) -> Iterator[None]:
    """Bind the session **and** principal scopes for an event's lifetime.

    Enters :func:`agents.session_id.session_scope_from_metadata` and
    :func:`agents.principal_id.principal_scope_from_metadata` together via
    an :class:`~contextlib.ExitStack` so both are restored on exit
    (including on exception).  A no-op for either axis whose key is absent.
    """
    with ExitStack() as stack:
        stack.enter_context(session_scope_from_metadata(metadata))
        stack.enter_context(principal_scope_from_metadata(metadata))
        yield
