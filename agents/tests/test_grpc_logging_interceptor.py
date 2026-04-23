"""Unit tests for the cross-process correlation gRPC interceptor
(RFC 0018 Phase 3).

Asserts:
  1. ``persatrix-*`` metadata keys are bound onto structlog contextvars while
     the wrapped handler runs.
  2. Contextvars are unbound after the handler returns (success and error
     paths) so background log records do not inherit stale IDs.
  3. Missing metadata keys do not crash the interceptor.
  4. Mixed-case metadata keys are accepted (gRPC normalises to lowercase on
     the wire but local fixtures may send mixed case).
  5. ``service.kind`` / ``service.instance`` contextvars set by
     ``configure_logging`` survive the interceptor's bind/unbind cycle —
     i.e. the interceptor only unbinds the four keys it set.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import grpc
import grpc.aio
import pytest
import structlog

from agents.observability.grpc_logging import (
    LoggingMetadataInterceptor,
    _bind_from_metadata,
    _wrap_handler,
)


@pytest.fixture(autouse=True)
def _reset_contextvars() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# ─── _bind_from_metadata ──────────────────────────────────────────────────


class TestBindFromMetadata:
    def test_binds_all_four_keys(self) -> None:
        md = (
            ("persatrix-execution-id", "exec-1"),
            ("persatrix-step-id", "step-A"),
            ("persatrix-agent-id", "ember-owl"),
            ("persatrix-workflow-id", "wf-7"),
        )
        bound = _bind_from_metadata(md)
        assert bound == {
            "execution_id": "exec-1",
            "step_id": "step-A",
            "agent_id": "ember-owl",
            "workflow_id": "wf-7",
        }
        ctx = structlog.contextvars.get_contextvars()
        assert ctx["execution_id"] == "exec-1"
        assert ctx["step_id"] == "step-A"
        assert ctx["agent_id"] == "ember-owl"
        assert ctx["workflow_id"] == "wf-7"

    def test_missing_keys_not_bound(self) -> None:
        md = (("persatrix-agent-id", "ember-owl"),)
        bound = _bind_from_metadata(md)
        assert bound == {"agent_id": "ember-owl"}
        ctx = structlog.contextvars.get_contextvars()
        assert "execution_id" not in ctx
        assert "step_id" not in ctx
        assert "workflow_id" not in ctx

    def test_empty_value_skipped(self) -> None:
        md = (
            ("persatrix-execution-id", ""),
            ("persatrix-agent-id", "ember-owl"),
        )
        bound = _bind_from_metadata(md)
        assert bound == {"agent_id": "ember-owl"}
        assert "execution_id" not in structlog.contextvars.get_contextvars()

    def test_unrelated_keys_ignored(self) -> None:
        md = (("authorization", "Bearer x"), ("persatrix-agent-id", "ember-owl"))
        bound = _bind_from_metadata(md)
        assert bound == {"agent_id": "ember-owl"}

    def test_mixed_case_keys_normalised(self) -> None:
        md = (("Persatrix-Execution-Id", "exec-1"),)
        bound = _bind_from_metadata(md)
        assert bound == {"execution_id": "exec-1"}

    def test_none_metadata_returns_empty(self) -> None:
        assert _bind_from_metadata(None) == {}


# ─── _wrap_handler ─────────────────────────────────────────────────────────


class _FakeContext:
    def __init__(self, metadata: tuple[tuple[str, str], ...]) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


def _make_unary_handler(callable_: Any) -> grpc.RpcMethodHandler:
    """Build a unary-unary handler via the public grpc factory.

    ``grpc.unary_unary_rpc_method_handler`` returns a ``namedtuple`` whose
    ``_replace(...)`` is what the interceptor uses to rebuild a wrapped
    handler — using the factory keeps the test on the same surface as
    the production gRPC codegen output.
    """
    return grpc.unary_unary_rpc_method_handler(callable_)


class TestWrapHandler:
    async def test_contextvars_visible_inside_handler_and_cleared_after(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        async def handler(_request: Any, _context: Any) -> str:
            captured["inside"] = dict(structlog.contextvars.get_contextvars())
            return "ok"

        wrapped = _wrap_handler(_make_unary_handler(handler))
        ctx = _FakeContext(
            (
                ("persatrix-execution-id", "exec-1"),
                ("persatrix-agent-id", "ember-owl"),
            )
        )
        result = await wrapped.unary_unary(object(), ctx)  # type: ignore[misc,arg-type]

        assert result == "ok"
        assert captured["inside"]["execution_id"] == "exec-1"
        assert captured["inside"]["agent_id"] == "ember-owl"
        # Outside the handler — contextvars should be cleared.
        assert "execution_id" not in structlog.contextvars.get_contextvars()
        assert "agent_id" not in structlog.contextvars.get_contextvars()

    async def test_handler_exception_still_unbinds(self) -> None:
        async def handler(_request: Any, _context: Any) -> str:
            raise RuntimeError("boom")

        wrapped = _wrap_handler(_make_unary_handler(handler))
        ctx = _FakeContext((("persatrix-execution-id", "exec-2"),))

        with pytest.raises(RuntimeError, match="boom"):
            await wrapped.unary_unary(object(), ctx)  # type: ignore[misc,arg-type]

        # Cleanup must run even on exception.
        assert "execution_id" not in structlog.contextvars.get_contextvars()

    async def test_preserves_unrelated_contextvars(self) -> None:
        # Simulate ``configure_logging`` having bound service.* before any RPC.
        structlog.contextvars.bind_contextvars(
            **{"service.kind": "agent", "service.instance": "ember-owl"}
        )

        async def handler(_request: Any, _context: Any) -> str:
            return "ok"

        wrapped = _wrap_handler(_make_unary_handler(handler))
        ctx = _FakeContext((("persatrix-execution-id", "exec-3"),))
        await wrapped.unary_unary(object(), ctx)  # type: ignore[misc,arg-type]

        remaining = structlog.contextvars.get_contextvars()
        assert remaining.get("service.kind") == "agent"
        assert remaining.get("service.instance") == "ember-owl"
        assert "execution_id" not in remaining


# ─── LoggingMetadataInterceptor.intercept_service ──────────────────────────


class TestInterceptor:
    async def test_returns_none_when_continuation_returns_none(self) -> None:
        interceptor = LoggingMetadataInterceptor()
        continuation = AsyncMock(return_value=None)
        details = AsyncMock()
        result = await interceptor.intercept_service(continuation, details)
        assert result is None

    async def test_wraps_resolved_handler(self) -> None:
        async def handler(_request: Any, _context: Any) -> str:
            return "ok"

        original = _make_unary_handler(handler)
        interceptor = LoggingMetadataInterceptor()
        continuation = AsyncMock(return_value=original)
        details = AsyncMock()
        wrapped = await interceptor.intercept_service(continuation, details)

        assert wrapped is not None
        # Wrapped handler must still be unary_unary-shaped but a different
        # callable than the original.
        assert wrapped.unary_unary is not None
        assert wrapped.unary_unary is not original.unary_unary
