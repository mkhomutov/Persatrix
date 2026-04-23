"""Cross-process correlation gRPC interceptor (RFC 0018 Phase 3).

Reads the four ``persatrix-*`` metadata keys (defined in
:doc:`docs/observability.md` § D and pinned in
``internal/observability/grpcmeta/grpcmeta.go``) off every incoming
``grpc.aio`` server call and binds them to ``structlog``'s contextvars for
the duration of the handler.  After the handler returns (success or error)
the contextvars are reset so subsequent log records emitted outside the
request scope (background tasks, ticks) do not inherit stale IDs.

Registration order
------------------
This interceptor MUST be installed **after** ``GrpcAioInstrumentorServer``
from RFC 0019 Phase 1.  The OTEL instrumentor establishes the active span
for the handler; the OTEL trace processor in
:mod:`agents.observability.logging` reads ``trace.get_current_span()`` and
joins ``trace_id`` / ``span_id`` to log records.  If this interceptor ran
first, the OTEL context would not yet exist and log lines from inside the
handler would carry ``execution_id`` / ``step_id`` but no trace IDs —
silently breaking the log↔trace pivot the operator-facing CLI relies on.

Wire form
---------
Metadata keys are lowercase kebab-case per gRPC spec (and RFC 6648):

============================ =====================
gRPC metadata key            structlog contextvar
============================ =====================
``persatrix-execution-id``   ``execution_id``
``persatrix-step-id``        ``step_id``
``persatrix-agent-id``       ``agent_id``
``persatrix-workflow-id``    ``workflow_id``
============================ =====================

Missing keys produce *no* contextvar binding — absence is preserved through
to the schema's "Optional" emission contract (RFC 0018 § B).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, cast

import grpc
import grpc.aio
import structlog

# ─── Metadata key ↔ contextvar mapping (single source of truth) ─────────────

#: ``persatrix-`` prefixed gRPC metadata keys → structlog contextvar names.
#: The Go side defines the same constants in ``internal/observability/grpcmeta``.
_METADATA_TO_CONTEXTVAR: dict[str, str] = {
    "persatrix-execution-id": "execution_id",
    "persatrix-step-id": "step_id",
    "persatrix-agent-id": "agent_id",
    "persatrix-workflow-id": "workflow_id",
}


def _bind_from_metadata(
    md: grpc.aio.Metadata | tuple[tuple[str, str], ...] | None,
) -> dict[str, str]:
    """Read the four ``persatrix-*`` keys off ``md`` and return the bound subset.

    Returns the map actually bound (caller-friendly for tests + logs).
    Missing keys are not bound — callers downstream rely on the schema's
    Optional contract (absent → omitted, not emitted as empty strings).
    """
    if md is None:
        return {}

    bound: dict[str, str] = {}

    # grpc.aio.Metadata is iterable as (key, value) pairs; tests may also
    # pass a plain tuple of pairs (matches the bufconn fixtures used in
    # ``tests/integration/test_logs_correlation.py``).  ``cast`` here
    # narrows the union to the iterable shape mypy can reason about —
    # both ``Metadata`` and the test tuples satisfy it at runtime.
    for key, value in cast(Iterable[tuple[str, str]], md):
        # gRPC normalises metadata keys to lowercase on the wire, but local
        # test fixtures may construct mixed-case keys.  Normalise here to
        # match the wire contract regardless.
        ctx_name = _METADATA_TO_CONTEXTVAR.get(key.lower())
        if ctx_name is None or not value:
            continue
        bound[ctx_name] = value

    if bound:
        structlog.contextvars.bind_contextvars(**bound)
    return bound


class LoggingMetadataInterceptor(grpc.aio.ServerInterceptor):
    """Bind correlation IDs from incoming gRPC metadata to structlog contextvars.

    Lifecycle per RPC:

    1. ``intercept_service`` reads the incoming metadata via the handler
       call details and binds the four IDs to contextvars.
    2. The wrapped handler runs.  All log records emitted from within
       inherit the IDs because :mod:`agents.observability.logging`'s
       ``merge_contextvars`` processor sits at the head of the chain.
    3. On return (or exception) the contextvars are unbound so subsequent
       background-task log records do not carry stale IDs.

    Concurrency note: ``structlog.contextvars`` are backed by Python's
    ``contextvars.ContextVar`` which is task-local under
    ``asyncio``-based servers — concurrent RPCs in the same process see
    independent values, so there is no race even when the agent process
    handles many in-flight requests.
    """

    async def intercept_service(
        self,
        continuation: Callable[
            [grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler | None]
        ],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:  # type: ignore[override]
        # ServerInterceptor sees the handler-resolution call, not the per-RPC
        # invocation.  Wrap the resolved handler so the contextvar bind/unbind
        # happens on every actual call, not once at registration time.
        #
        # The ``# type: ignore[override]`` hides a stub-only mismatch: the
        # ``grpc.aio.ServerInterceptor`` stubs declare the return type as
        # ``RpcMethodHandler[_TRequest, _TResponse]`` (non-Optional and
        # parametric), while the runtime contract documented at
        # https://grpc.github.io/grpc/python/grpc_asyncio.html actually
        # allows returning ``None`` to reject the call (treated as
        # UNIMPLEMENTED by grpc-python).  We honour the runtime contract.
        handler = await continuation(handler_call_details)
        if handler is None:
            return cast(grpc.RpcMethodHandler, None)
        return _wrap_handler(handler)


def _wrap_handler(
    handler: grpc.RpcMethodHandler,
) -> grpc.RpcMethodHandler:
    """Return a handler that binds metadata contextvars around each call.

    grpc-python exposes four handler shapes (unary-unary, unary-stream,
    stream-unary, stream-stream).  All four expose a callable that takes
    ``(request_or_iterator, context)`` where ``context.invocation_metadata()``
    returns the incoming metadata.  We wrap whichever of the four function
    pointers is set on the handler, leaving the rest untouched.

    The grpc-python stubs in ``types-grpcio`` describe ``RpcMethodHandler``
    as an abstract class (its concrete shape is the
    ``grpc._utilities.RpcMethodHandler`` ``NamedTuple`` returned by the
    public ``grpc.unary_unary_rpc_method_handler`` factory et al).  The
    stubs therefore omit ``_replace`` and the per-call shape's async
    semantics — the ``# type: ignore`` markers below cover that gap.
    """
    if handler.unary_unary is not None:
        original = handler.unary_unary

        async def wrapped_unary_unary(request: Any, context: Any) -> Any:
            tokens = _bind_from_metadata(context.invocation_metadata())
            try:
                return await original(request, context)
            finally:
                _unbind(tokens)

        return handler._replace(unary_unary=wrapped_unary_unary)  # type: ignore[attr-defined,no-any-return]

    if handler.unary_stream is not None:
        original_us = handler.unary_stream

        async def wrapped_unary_stream(request: Any, context: Any) -> Any:
            tokens = _bind_from_metadata(context.invocation_metadata())
            try:
                async for item in original_us(request, context):  # type: ignore[attr-defined]
                    yield item
            finally:
                _unbind(tokens)

        return handler._replace(unary_stream=wrapped_unary_stream)  # type: ignore[attr-defined,no-any-return]

    if handler.stream_unary is not None:
        original_su = handler.stream_unary

        async def wrapped_stream_unary(
            request_iterator: Any, context: Any
        ) -> Any:
            tokens = _bind_from_metadata(context.invocation_metadata())
            try:
                return await original_su(request_iterator, context)
            finally:
                _unbind(tokens)

        return handler._replace(stream_unary=wrapped_stream_unary)  # type: ignore[attr-defined,no-any-return]

    if handler.stream_stream is not None:
        original_ss = handler.stream_stream

        async def wrapped_stream_stream(
            request_iterator: Any, context: Any
        ) -> Any:
            tokens = _bind_from_metadata(context.invocation_metadata())
            try:
                async for item in original_ss(request_iterator, context):  # type: ignore[attr-defined]
                    yield item
            finally:
                _unbind(tokens)

        return handler._replace(stream_stream=wrapped_stream_stream)  # type: ignore[attr-defined,no-any-return]

    # Unknown handler shape (a future grpc-python release adds a fifth?) —
    # return unchanged rather than crashing.  Logging would itself emit
    # through the chain we just failed to wire, so use stderr as a last
    # resort.  This branch is intentionally untested.
    return handler


def _unbind(bound: dict[str, str]) -> None:
    """Unbind the keys this interceptor set, leaving prior contextvars
    (e.g. ``service.kind`` / ``service.instance`` from
    :func:`agents.observability.logging.configure_logging`) intact."""
    if bound:
        structlog.contextvars.unbind_contextvars(*bound.keys())


__all__ = [
    "LoggingMetadataInterceptor",
    "_METADATA_TO_CONTEXTVAR",
]
