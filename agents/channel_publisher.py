"""HTTP publisher for the RFC 0011 channels REST surface.

Lives in its own module (rather than in :mod:`agents.dispatch`) so that
:mod:`agents.dispatch` stays free of an :mod:`aiohttp` import — keeping
unit-test fixtures that construct an :class:`ActionExecutor` directly
free of an aiohttp dependency.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

import aiohttp
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from .observability.spans import CHANNEL_PUBLISH_SPAN

logger = logging.getLogger(__name__)

# ISSUE-0032 — business-logic publisher span. Companion to the Go-side
# `channel.dispatch` span emitted by ``internal/channels/grpc_dispatcher.go``
# (PR #286). The autoinstrumentation aiohttp client span already correlates
# the wire hop, but its name carries only the HTTP method; an operator
# pivoting from a trace ID needs a parent span tagged with the publish-path
# vocabulary (``channel.id``, ``channel.sender_id``, ``channel.message_id``,
# ``channel.mentions_count``) so dashboards and queries do not have to
# drill into child-span attributes.
_tracer = trace.get_tracer(__name__)

# Single source of truth for the REST publish timeout (seconds).
#
# PR #250 review (Should-Fix #1): the previous implementation hard-coded
# ``aiohttp.ClientTimeout(total=10)`` here *and* wrapped the publish call
# in ``asyncio.wait_for(timeout=10.0)`` from
# :mod:`agents.action_executor`. Two unrelated 10-second timers tuned to
# the same value but maintained in two places — a tuning footgun for the
# RFC 0009 Phase 4 mTLS cold-start work which will need to raise the
# ceiling. This constant is the one place to change it; the executor's
# ``asyncio.wait_for`` (defense-in-depth ceiling, also covers non-HTTP
# :class:`ChannelPublisher` Protocol implementations) imports and reuses
# this value.
#
# 10 s tolerates a TLS handshake + a slow orchestrator without making a
# stuck server block the executor for a full minute.
DEFAULT_PUBLISH_TIMEOUT_SECONDS: float = 10.0

__all__ = [
    "ChannelPublisher",
    "ChannelsDisabledError",
    "HTTPChannelPublisher",
    "DEFAULT_PUBLISH_TIMEOUT_SECONDS",
]


class ChannelsDisabledError(RuntimeError):
    """Orchestrator returned HTTP 503 from the channels publish endpoint.

    Raised by :class:`HTTPChannelPublisher.publish` after the orchestrator
    signals that the channels subsystem is disabled (per
    ``cmd/orchestrator/channels.go::selectChannelDispatcher``). The flag
    is sticky for the lifetime of the process — subsequent
    :meth:`HTTPChannelPublisher.publish` calls short-circuit with this
    exception and never hit the wire.

    Distinct from a generic transport failure so callers can map the
    deployment-wide gate to a dedicated status taxonomy
    (``send_channel_message`` action result ``status="channels_disabled"``)
    rather than the per-message ``status="failed"`` that would prompt the
    LLM to retry.
    """


@runtime_checkable
class ChannelPublisher(Protocol):
    """Wire-publish seam for ``SEND_CHANNEL_MESSAGE`` (RFC 0011 PR 4a-ii-β-1).

    Translates an outbound action to ``POST /api/v1/channels/{channel_id}/messages``
    against the orchestrator REST surface; the orchestrator then fans out
    via the Go-side ``GRPCMessageDispatcher``.  Implementations MUST be
    safe to call concurrently (the production ``HTTPChannelPublisher``
    reuses a shared :class:`aiohttp.ClientSession`).
    """

    async def publish(
        self,
        *,
        channel_id: str,
        sender_id: str,
        content: str,
        mentions: list[str],
        cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Publish a message; raises on transport / HTTP failures.

        ``cascade_depth`` carries the cooperative-path cascade hop count
        through the REST publish boundary (RFC 0011 amendment
        "Cascade-depth wire propagation"). Default is
        :data:`DEFAULT_MAX_CASCADE_DEPTH` so a call site that omits the
        kwarg (notably the tick scheduler, which has no inbound event
        to derive depth from) gets the orchestrator's
        ``cascade_depth >= max_cascade_depth`` terminate-at-clamp
        behaviour. Callers that legitimately mark a publish as
        chain-origin (chat surface, dispatcher's first hop) pass
        ``cascade_depth=0`` explicitly. See the contract pin in
        :mod:`tests.unit.python.test_tick_cascade_depth_default`.

        ``metadata`` is an optional caller-supplied map merged into the
        wire payload's ``metadata`` object alongside ``cascade_depth``.
        ISSUE-0065: the chat-error reply published by
        ``AgentServiceServicer._dispatch_channel_event`` under
        :class:`BudgetExceededError` rides on this seat with
        ``{"reply_status": "error"}`` so the orchestrator's REST chat
        handler renders ``reply_status="error"`` in the JSON envelope
        instead of the default ``"ok"``. Caller-supplied keys do not
        overwrite ``cascade_depth`` — the publisher reserves that key.
        """
        ...


class HTTPChannelPublisher:
    """Production :class:`agents.dispatch.ChannelPublisher` implementation.

    Targets ``POST {orchestrator_url}/api/v1/channels/{channel_id}/messages``
    using a shared :class:`aiohttp.ClientSession`.  Reuses the same session
    that :class:`agents.server.AgentServer` opens for self-registration so
    one process keeps a single connection pool to the orchestrator.

    The ``sender_id`` is provided by the action executor at call time and
    is the agent's framework-known registered ID — the executor never
    forwards an LLM-supplied value here.  This is the security-critical
    invariant from the RFC 0011 amendment §"DM gate-bypass": the
    orchestrator trusts ``sender_id`` because it arrives over a path the
    LLM cannot influence.
    """

    def __init__(
        self,
        *,
        orchestrator_url: str,
        session: aiohttp.ClientSession,
        timeout: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self._base = orchestrator_url.rstrip("/")
        self._session = session
        # Stored so ``publish`` can hand it to :class:`aiohttp.ClientTimeout`
        # on every call. Per-instance (rather than per-call) because the
        # publisher is constructed once at server startup and the timeout
        # is a deployment knob, not a per-message decision.
        self._timeout = timeout
        # ISSUE-0026: sticky disabled-flag flipped on the first HTTP 503 from
        # the channels publish endpoint. Same agent build runs against
        # orchestrators with and without channels enabled (deferred-by-default
        # phase model in ``cmd/orchestrator/channels.go::selectChannelDispatcher``);
        # without this short-circuit every action burned an HTTP RTT and
        # logged a per-call WARN.
        self._disabled = False

    async def publish(
        self,
        *,
        channel_id: str,
        sender_id: str,
        content: str,
        mentions: list[str],
        cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """POST a single channel message; raise on transport / non-2xx.

        Raises :class:`ChannelsDisabledError` if the orchestrator has
        signalled that the channels subsystem is disabled (HTTP 503 on a
        prior call) — the flag is sticky for the lifetime of the
        instance and subsequent calls short-circuit without an HTTP
        roundtrip. Other 4xx/5xx statuses still raise
        :class:`aiohttp.ClientResponseError` per the existing contract.

        ISSUE-0032: emits a ``channel.publish`` business-logic span with
        ``channel.id``, ``channel.sender_id``, ``channel.mentions_count``
        and (on success) ``channel.message_id`` so an operator pivoting
        from a trace ID can find the publish attempt without drilling
        into the autoinstrumented aiohttp child span.
        """
        # Span wraps the *entire* publish attempt — including the
        # sticky-disabled short-circuit — so an operator looking at the
        # action executor's parent span never sees "agent decided to
        # publish" with no child evidence of the attempt. Status is left
        # UNSET on the 503 / sticky-disable paths because that branch is
        # a deployment-wide signal (channels off), not an internal
        # failure; mirroring the Go-side ``channel.dispatch`` discipline
        # for best-effort no-ops keeps error-rate dashboards honest.
        # ``set_status_on_exception=False`` and ``record_exception=False``
        # disable OTel's auto-status / auto-record so the 503 sticky-disable
        # branch can record the exception event WITHOUT promoting status to
        # ERROR (deployment signal, not an internal failure — see the
        # except-clause docstring below).
        with _tracer.start_as_current_span(
            CHANNEL_PUBLISH_SPAN,
            attributes={
                "channel.id": channel_id,
                "channel.sender_id": sender_id,
                "channel.mentions_count": len(mentions),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                # ISSUE-0026: short-circuit before any work — no URL build,
                # no request, no log line — once the deployment-wide gate
                # has fired.
                if self._disabled:
                    raise ChannelsDisabledError(
                        "channels disabled at orchestrator "
                        "(sticky after first HTTP 503)",
                    )

                if not channel_id:
                    # Defensive: the executor only routes here when
                    # channel_id is non-empty, but a future caller path
                    # might miss the check.  Surface as an explicit
                    # error so the executor's except-clause records
                    # ``status="failed"``.
                    raise ValueError(
                        "channel_id is required for REST publish",
                    )

                # PR #250 review (Must-Fix #1, OWASP A03 — URL/path
                # injection): ``channel_id`` originates from
                # ``action.payload`` on the LLM side. Without
                # ``quote(safe="")`` an interpolated ``/``, ``?``, ``#``
                # or whitespace would escape the intended path segment
                # (extra path components, smuggled query string, silent
                # fragment truncation). The orchestrator's
                # ``validateChannelID`` rejects malformed values once
                # they land, but the malformed request still ships and
                # is recorded in access logs/metrics — encode at the
                # boundary so a hallucinated id can never produce a
                # surprising URL on the wire.
                url = (
                    f"{self._base}/api/v1/channels/"
                    f"{quote(channel_id, safe='')}/messages"
                )
                payload: dict[str, Any] = {
                    "sender_id": sender_id,
                    "content": content,
                }
                if mentions:
                    payload["mentions"] = mentions
                # RFC 0011 amendment "Cascade-depth wire propagation"
                # (PR 3 of the v0.3.0 channel test-findings plan):
                # forward the cooperative-path hop count via the existing
                # REST ``metadata`` seat so the orchestrator's publish
                # handler can clamp + fanout-cap on it (PR 2). Zero is
                # the cascade-origin value and is indistinguishable from
                # unset on the proto3 side; omit the ``metadata`` map
                # entirely on zero so non-cascade publishes keep the
                # previously-clean POST body shape (no ``"metadata": {}``
                # on every publish — that would be operational noise
                # without carrying a signal).
                #
                # ISSUE-0065: caller-supplied ``metadata`` (e.g. the
                # chat-error envelope's ``{"reply_status": "error"}``
                # discriminator) merges in alongside ``cascade_depth``.
                # ``cascade_depth`` is reserved — caller keys named
                # ``cascade_depth`` are ignored to preserve the
                # publisher's invariant on that key. Build the map
                # whenever there is *anything* to send so error envelopes
                # at cascade-origin (depth=0) still reach the wire.
                wire_metadata: dict[str, Any] = {}
                if metadata:
                    wire_metadata.update(
                        {k: v for k, v in metadata.items() if k != "cascade_depth"},
                    )
                if cascade_depth:
                    wire_metadata["cascade_depth"] = cascade_depth
                if wire_metadata:
                    payload["metadata"] = wire_metadata

                async with self._session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status == 503:
                        # ISSUE-0026: orchestrator-channels-disabled
                        # signal. Flip the sticky flag, emit a one-shot
                        # diagnostic WARN with the response body, and
                        # surface a typed error so the executor maps it
                        # to ``status="channels_disabled"`` rather than
                        # the retry-implying ``status="failed"``.
                        # We chose 503 (and only 503) because:
                        #   - The Go orchestrator's
                        #     ``selectChannelDispatcher`` returns 503
                        #     specifically when channels are off.
                        #   - 500 is treated as transient
                        #     (orchestrator bug).
                        #   - 403/404 are per-message conditions
                        #     (NOT_MEMBER / channel-not-found) and must
                        #     not poison the publisher for unrelated
                        #     channels.
                        body = await resp.text()
                        logger.warning(
                            "channels: orchestrator returned HTTP 503 "
                            "on publish to %s; disabling further "
                            "publish attempts for this process. "
                            "Response: %s",
                            channel_id, body[:512],
                        )
                        self._disabled = True
                        raise ChannelsDisabledError(
                            f"channels disabled at orchestrator "
                            f"(HTTP 503 on first publish to "
                            f"{channel_id})",
                        )

                    if resp.status >= 400:
                        # Read body to surface structured orchestrator
                        # error codes (e.g. NOT_MEMBER, NOT_FOUND) in
                        # the warn log without coupling this layer to
                        # the response schema.
                        body = await resp.text()
                        logger.warning(
                            "channels: publish to %s returned HTTP %d: %s",
                            channel_id, resp.status, body[:512],
                        )
                        resp.raise_for_status()

                    # Happy path: capture the orchestrator-assigned
                    # message_id so this span joins the Go-side
                    # ``channel.dispatch`` span (which carries the same
                    # id under the same attribute key) in trace storage.
                    # Best-effort — a malformed JSON body or missing
                    # ``id`` field does not fail the publish; the message
                    # has already been accepted by the orchestrator.
                    message_id = await _read_message_id(resp)
                    if message_id:
                        span.set_attribute(
                            "channel.message_id", message_id,
                        )
            except ChannelsDisabledError as exc:
                # Deployment signal, not an internal failure: record the
                # exception event so operators can still pivot to the
                # disabled state via an ``event.name = "exception"``
                # query, but leave status UNSET so error-rate dashboards
                # do not light up on every channels-off run. Mirrors the
                # Go-side resolver-not-found discipline (best-effort
                # no-op, no SetStatus Error).
                span.record_exception(exc)
                raise
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise


async def _read_message_id(resp: aiohttp.ClientResponse) -> str | None:
    """Best-effort extraction of the orchestrator-assigned ``id``.

    The publish endpoint returns ``{"id": "<uuid>", ...}`` on 201
    (see ``internal/server/channel_handlers.go::messageToResponse``).
    A malformed body or missing field returns ``None`` rather than
    raising — the publish itself succeeded; only the trace-correlation
    attribute is degraded.
    """
    try:
        body = await resp.json()
    except (aiohttp.ContentTypeError, ValueError):
        return None
    if isinstance(body, dict):
        msg_id = body.get("id")
        if isinstance(msg_id, str) and msg_id:
            return msg_id
    return None
