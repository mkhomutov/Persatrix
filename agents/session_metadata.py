"""gRPC session-id metadata extraction (ISSUE-0081 PR 2).

The orchestrator emits the per-request session id as the
:data:`~agents.session_id.SESSION_METADATA_GRPC_KEY` header on every
outbound call.  The two helpers here lift that header off inbound gRPC
metadata so both ``SendChatMessage`` and ``ReceiveChannelMessage`` share
one extraction point.

Kept separate from ``server_servicers`` so the pure extraction logic is
independently testable and the servicer module stays under the file-size
cap.
"""

from collections.abc import Iterable
from typing import cast

import grpc.aio

from .session_id import SESSION_METADATA_GRPC_KEY


def _session_from_metadata(
    metadata: Iterable[tuple[str, str | bytes]] | None,
) -> str | None:
    """Lift the ``persatrix-session`` header off gRPC invocation metadata.

    Reads the :data:`SESSION_METADATA_GRPC_KEY` header off an iterable of
    ``(key, value)`` pairs.  Returns ``None`` when the header is absent or
    blank — a blank value must not bind a blank scope (which would collapse
    to the ``legacy`` carve-out and re-merge conversations), so it is
    treated as "no per-request session" and the caller falls back to its
    construction snapshot.

    Key matching is case-insensitive (HTTP/2 lower-cases header names, but a
    proxy or test harness may present mixed case); first non-empty match wins.
    """
    if metadata is None:
        return None
    for key, value in metadata:
        if isinstance(key, str) and key.lower() == SESSION_METADATA_GRPC_KEY:
            if isinstance(value, str) and value:
                return value
    return None


def _session_from_context(context: grpc.aio.ServicerContext) -> str | None:
    """Read the session header off a live gRPC context.

    Thin bridge over :func:`_session_from_metadata` that isolates a
    ``grpc-stubs`` discrepancy: the stubs model ``Metadata`` as a
    ``Mapping[str, value]`` (so mypy thinks iterating yields *keys*), but the
    runtime ``Metadata.__iter__`` yields the flattened ``(key, value)`` tuples
    ``_session_from_metadata`` consumes.  ``.items()`` is *not* an option — at
    runtime it collapses repeated keys into ``(key, list[value])``.  The cast
    asserts the runtime-true shape over the incorrect stub.
    """
    return _session_from_metadata(
        cast(
            "Iterable[tuple[str, str | bytes]] | None",
            context.invocation_metadata(),
        ),
    )
