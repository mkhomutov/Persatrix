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

logger = logging.getLogger(__name__)

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
    ) -> None:
        """Publish a message; raises on transport / HTTP failures."""
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
    ) -> None:
        """POST a single channel message; raise on transport / non-2xx.

        Raises :class:`ChannelsDisabledError` if the orchestrator has
        signalled that the channels subsystem is disabled (HTTP 503 on a
        prior call) — the flag is sticky for the lifetime of the
        instance and subsequent calls short-circuit without an HTTP
        roundtrip. Other 4xx/5xx statuses still raise
        :class:`aiohttp.ClientResponseError` per the existing contract.
        """
        # ISSUE-0026: short-circuit before any work — no URL build, no
        # request, no log line — once the deployment-wide gate has fired.
        if self._disabled:
            raise ChannelsDisabledError(
                "channels disabled at orchestrator (sticky after first HTTP 503)",
            )

        if not channel_id:
            # Defensive: the executor only routes here when channel_id
            # is non-empty, but a future caller path might miss the
            # check.  Surface as an explicit error so the executor's
            # except-clause records ``status="failed"``.
            raise ValueError("channel_id is required for REST publish")

        # PR #250 review (Must-Fix #1, OWASP A03 — URL/path injection):
        # ``channel_id`` originates from ``action.payload`` on the LLM
        # side. Without ``quote(safe="")`` an interpolated ``/``, ``?``,
        # ``#`` or whitespace would escape the intended path segment
        # (extra path components, smuggled query string, silent fragment
        # truncation). The orchestrator's ``validateChannelID`` rejects
        # malformed values once they land, but the malformed request
        # still ships and is recorded in access logs/metrics — encode
        # at the boundary so a hallucinated id can never produce a
        # surprising URL on the wire.
        url = f"{self._base}/api/v1/channels/{quote(channel_id, safe='')}/messages"
        payload: dict[str, Any] = {
            "sender_id": sender_id,
            "content": content,
        }
        if mentions:
            payload["mentions"] = mentions

        async with self._session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            if resp.status == 503:
                # ISSUE-0026: orchestrator-channels-disabled signal. Flip
                # the sticky flag, emit a one-shot diagnostic WARN with
                # the response body, and surface a typed error so the
                # executor maps it to ``status="channels_disabled"``
                # rather than the retry-implying ``status="failed"``.
                # We chose 503 (and only 503) because:
                #   - The Go orchestrator's ``selectChannelDispatcher``
                #     returns 503 specifically when channels are off.
                #   - 500 is treated as transient (orchestrator bug).
                #   - 403/404 are per-message conditions (NOT_MEMBER /
                #     channel-not-found) and must not poison the
                #     publisher for unrelated channels.
                body = await resp.text()
                logger.warning(
                    "channels: orchestrator returned HTTP 503 on publish to "
                    "%s; disabling further publish attempts for this process. "
                    "Response: %s",
                    channel_id, body[:512],
                )
                self._disabled = True
                raise ChannelsDisabledError(
                    f"channels disabled at orchestrator "
                    f"(HTTP 503 on first publish to {channel_id})",
                )

            if resp.status >= 400:
                # Read body to surface structured orchestrator error
                # codes (e.g. NOT_MEMBER, NOT_FOUND) in the warn log
                # without coupling this layer to the response schema.
                body = await resp.text()
                logger.warning(
                    "channels: publish to %s returned HTTP %d: %s",
                    channel_id, resp.status, body[:512],
                )
                resp.raise_for_status()
