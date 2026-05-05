"""HTTP publisher for the RFC 0011 channels REST surface.

Lives in its own module (rather than in :mod:`agents.dispatch`) so that
:mod:`agents.dispatch` stays free of an :mod:`aiohttp` import — keeping
unit-test fixtures that construct an :class:`ActionExecutor` directly
free of an aiohttp dependency.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import aiohttp

logger = logging.getLogger(__name__)

__all__ = ["ChannelPublisher", "HTTPChannelPublisher"]


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
    ) -> None:
        self._base = orchestrator_url.rstrip("/")
        self._session = session

    async def publish(
        self,
        *,
        channel_id: str,
        sender_id: str,
        content: str,
        mentions: list[str],
    ) -> None:
        """POST a single channel message; raise on transport / non-2xx."""
        if not channel_id:
            # Defensive: the executor only routes here when channel_id
            # is non-empty, but a future caller path might miss the
            # check.  Surface as an explicit error so the executor's
            # except-clause records ``status="failed"``.
            raise ValueError("channel_id is required for REST publish")

        url = f"{self._base}/api/v1/channels/{channel_id}/messages"
        payload: dict[str, Any] = {
            "sender_id": sender_id,
            "content": content,
        }
        if mentions:
            payload["mentions"] = mentions

        async with self._session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
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
