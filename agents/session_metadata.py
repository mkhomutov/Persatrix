"""gRPC scope-axis metadata extraction (ISSUE-0081 PR 2/3 · ISSUE-0085 PR 4).

The orchestrator emits the per-request session id as the
:data:`~agents.session_id.SESSION_METADATA_GRPC_KEY` header, the
per-process epoch as the
:data:`~agents.epoch_id.EPOCH_METADATA_GRPC_KEY` header (``live`` in
production, a per-job id in CI), and (once RFC 0039 auth lands) the
per-request tenant as the
:data:`~agents.principal_id.PRINCIPAL_METADATA_GRPC_KEY` header on every
outbound call.  The helpers here lift those headers off inbound gRPC
metadata so both ``SendChatMessage`` and ``ReceiveChannelMessage`` share
one extraction point per axis.

Kept separate from ``server_servicers`` so the pure extraction logic is
independently testable and the servicer module stays under the file-size
cap.
"""

from collections.abc import Iterable
from typing import cast

import grpc.aio

from .epoch_id import EPOCH_METADATA_GRPC_KEY
from .principal_id import PRINCIPAL_METADATA_GRPC_KEY
from .session_id import SESSION_METADATA_GRPC_KEY

_Metadata = Iterable[tuple[str, str | bytes]] | None


def _header_from_metadata(metadata: _Metadata, header: str) -> str | None:
    """Lift a single lower-cased ``header`` off gRPC invocation metadata.

    Returns ``None`` when the header is absent or blank — a blank value
    must not bind a blank scope (which would collapse to a carve-out /
    default and re-merge requests), so it is treated as "no per-request
    value" and the caller falls back to its construction snapshot.

    Key matching is case-insensitive (HTTP/2 lower-cases header names, but
    a proxy or test harness may present mixed case); first non-empty match
    wins.  ``header`` must already be the canonical lower-case form.
    """
    if metadata is None:
        return None
    for key, value in metadata:
        if isinstance(key, str) and key.lower() == header:
            if isinstance(value, str) and value:
                return value
    return None


def _session_from_metadata(metadata: _Metadata) -> str | None:
    """Lift the ``persatrix-session`` header off gRPC invocation metadata."""
    return _header_from_metadata(metadata, SESSION_METADATA_GRPC_KEY)


def _principal_from_metadata(metadata: _Metadata) -> str | None:
    """Lift the ``persatrix-principal`` header off gRPC invocation metadata."""
    return _header_from_metadata(metadata, PRINCIPAL_METADATA_GRPC_KEY)


def _epoch_from_metadata(metadata: _Metadata) -> str | None:
    """Lift the ``persatrix-epoch`` header off gRPC invocation metadata."""
    return _header_from_metadata(metadata, EPOCH_METADATA_GRPC_KEY)


def _invocation_metadata(context: grpc.aio.ServicerContext) -> _Metadata:
    """Return a live context's invocation metadata as ``(key, value)`` pairs.

    Isolates a ``grpc-stubs`` discrepancy: the stubs model ``Metadata`` as
    a ``Mapping[str, value]`` (so mypy thinks iterating yields *keys*), but
    the runtime ``Metadata.__iter__`` yields the flattened ``(key, value)``
    tuples the extractors consume.  ``.items()`` is *not* an option — at
    runtime it collapses repeated keys into ``(key, list[value])``.  The
    cast asserts the runtime-true shape over the incorrect stub.
    """
    return cast(
        "Iterable[tuple[str, str | bytes]] | None",
        context.invocation_metadata(),
    )


def _session_from_context(context: grpc.aio.ServicerContext) -> str | None:
    """Read the session header off a live gRPC context."""
    return _session_from_metadata(_invocation_metadata(context))


def _principal_from_context(context: grpc.aio.ServicerContext) -> str | None:
    """Read the principal header off a live gRPC context."""
    return _principal_from_metadata(_invocation_metadata(context))


def _epoch_from_context(context: grpc.aio.ServicerContext) -> str | None:
    """Read the epoch header off a live gRPC context."""
    return _epoch_from_metadata(_invocation_metadata(context))
