"""RFC 0052 §E — the persona-side convene trigger client.

Lives in its own module (rather than in :mod:`agents.tick`) so the scheduler
stays free of an :mod:`aiohttp` import in unit-test fixtures, mirroring why
:mod:`agents.channel_publisher` is split out of :mod:`agents.dispatch`.

When a standing channel's RFC 0024 timer fires on the convener
(``ScheduledWake(callback_kind="convene")``), the convener-side handler
(:mod:`agents.tick`) recovers the channel id from the timer id
(:func:`agents.convene_timer.parse_standing_convene_timer_id`) and calls
:meth:`ConveneClient.convene`, which POSTs to
``{orchestrator_url}/api/v1/channels/{id}/convene`` — the SAME endpoint the
``persatrix channel convene`` CLI verb and the web "Convene" button hit
(``internal/server/channel_convene_handlers.go``).

Routing the fired schedule through that endpoint is the load-bearing safety
choice, not an implementation convenience: the §E aggregate ceilings
(``max_convenings`` count, ``standing_budget_tokens`` spend) are enforced
server-side in ``ChannelRouter.ConveneChannel`` (PR 7b), so a timer that
convenes via this client is bounded by exactly the same gates a human operator
is — a timer must never bypass the bounds §E built for it. A convening declined
at a bound (HTTP 429), or refused because a prior convening is still live (409)
or the convener is unreachable (503), is an *expected* outcome on an unattended
channel; the client surfaces it as :class:`aiohttp.ClientResponseError` and the
wake handler logs-and-drops it rather than crashing the event loop.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

# The convene POST carries no request body — the orchestrator reads the channel
# from the path — so the timeout only needs to cover the connection + the
# convener-dispatch ack. Reuses the publish timeout's rationale (a TLS handshake
# plus a slow orchestrator, without a stuck server blocking for a full minute).
DEFAULT_CONVENE_TIMEOUT_SECONDS: float = 10.0

__all__ = [
    "ConveneClient",
    "HTTPConveneClient",
    "DEFAULT_CONVENE_TIMEOUT_SECONDS",
]


@runtime_checkable
class ConveneClient(Protocol):
    """The convener-side trigger the scheduler calls on a convene wake.

    A Protocol (not a concrete base) so test fixtures can inject a fake without
    an :mod:`aiohttp` dependency, exactly as :class:`agents.dispatch.ChannelPublisher`
    is a Protocol over :class:`agents.channel_publisher.HTTPChannelPublisher`.
    """

    async def convene(self, channel_id: str) -> None:
        """Trigger a convening of ``channel_id`` via the orchestrator; raise on
        transport error or non-2xx status."""
        ...


class HTTPConveneClient:
    """Production :class:`ConveneClient` — POSTs the orchestrator convene endpoint.

    Reuses the shared :class:`aiohttp.ClientSession` that
    :class:`agents.server.AgentServer` opens for self-registration, so one
    process keeps a single connection pool to the orchestrator (the
    :class:`agents.channel_publisher.HTTPChannelPublisher` precedent).
    """

    def __init__(
        self,
        *,
        orchestrator_url: str,
        session: aiohttp.ClientSession,
        timeout: float = DEFAULT_CONVENE_TIMEOUT_SECONDS,
    ) -> None:
        self._base = orchestrator_url.rstrip("/")
        self._session = session
        self._timeout = timeout

    async def convene(self, channel_id: str) -> None:
        """POST ``/api/v1/channels/{channel_id}/convene``; raise on non-2xx.

        Non-2xx is logged with the orchestrator's structured error body (which
        carries the §E bound / conflict reason) before the raise, so an operator
        can see *why* a scheduled convening was declined without coupling this
        layer to the response schema — the publisher's non-2xx discipline.
        """
        if not channel_id:
            # Defensive: the handler only routes here after
            # ``parse_standing_convene_timer_id`` returns a non-empty
            # ``group:<name>``, but surface an explicit error rather than POST a
            # malformed URL if a future caller path misses the check.
            raise ValueError("channel_id is required for convene")

        # Path-encode the id (``safe=""``) exactly as the message publisher does:
        # ``group:planning`` -> ``group%3Aplanning``, so the colon cannot escape
        # the intended path segment. The orchestrator's router decodes it back.
        url = (
            f"{self._base}/api/v1/channels/"
            f"{quote(channel_id, safe='')}/convene"
        )
        async with self._session.post(
            url, timeout=aiohttp.ClientTimeout(total=self._timeout),
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    "convene of %s returned HTTP %d: %s",
                    channel_id, resp.status, body[:512],
                )
                resp.raise_for_status()
