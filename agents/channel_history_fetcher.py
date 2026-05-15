"""Channel-history fetch behind a Protocol (RFC 0034 Phase 1 PR 1).

Factored out of :mod:`agents.channel_catchup` (where the fetch lived as
the private ``_fetch_channel_history`` helper) so two callers can share
one history-fetch contract:

* the on-startup catch-up replay (:mod:`agents.channel_catchup`), and
* the persona-runtime conversation-window substrate landing in
  [RFC 0034](docs/rfcs/0034-persona-conversational-working-memory.md)
  Phase 1 PR 2.

The :class:`ChannelHistoryFetcher` :class:`typing.Protocol` is the seam:
production code binds :class:`HttpChannelHistoryFetcher`, tests bind a
duck-typed fake without inheritance ceremony.

Contract (verbatim from the lifted catch-up helper): :meth:`fetch`
returns the channel's message list on success, ``[]`` when the
``messages`` field is absent or not a list, and ``None`` on any HTTP
4xx/5xx or transport error (logged WARN). ``None`` means "best-effort
failure already logged" — callers branch on it rather than catching an
exception, so the catch-up call site's ``if messages is None: continue``
guard is unchanged.

This module imports nothing from :mod:`agents.channel_catchup` or
:mod:`agents.persona_runtime`, so either side can depend on it without
pulling in the other's module graph.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "ChannelHistoryFetcher",
    "HttpChannelHistoryFetcher",
]

logger = logging.getLogger(__name__)

# Per-request timeout (seconds) for the default-constructed fetcher.
# Mirrors the value :data:`agents.channel_catchup._REQUEST_TIMEOUT_SECONDS`
# uses: short enough that a stuck orchestrator does not freeze a caller
# for minutes, long enough to tolerate cold-cache disk reads on large
# channel-history rows. The catch-up path passes its own
# :class:`aiohttp.ClientTimeout` explicitly, so the two values are
# independent — this default applies only to callers that omit
# ``timeout`` (e.g. the RFC 0034 conversation window).
DEFAULT_REQUEST_TIMEOUT_SECONDS: float = 10.0


class ChannelHistoryFetcher(Protocol):
    """Minimum surface a caller needs to pull a channel's recent history.

    Structural :class:`typing.Protocol` (not :class:`abc.ABC`) so a test
    fake is a duck-typed object without inheritance ceremony. Not
    decorated ``@runtime_checkable`` — callers depend on static typing
    only; there is no ``isinstance`` site against this Protocol.
    """

    async def fetch(
        self, channel_id: str, *, limit: int,
    ) -> list[dict[str, Any]] | None:
        """Return the last ``limit`` messages for ``channel_id``.

        ``[]`` when the channel is empty, ``None`` on a best-effort
        failure (already logged WARN).
        """
        ...


class HttpChannelHistoryFetcher:
    """Production :class:`ChannelHistoryFetcher` backed by ``aiohttp``.

    The :meth:`fetch` body is the verbatim
    ``GET /api/v1/channels/{id}/messages?limit=N`` request lifted from
    the former ``agents.channel_catchup._fetch_channel_history`` helper —
    same ``None``-on-error / list-on-success contract.

    ``session``, ``orchestrator_url`` and ``timeout`` are resolved once
    at construction; :meth:`fetch` takes only the per-call ``channel_id``
    and ``limit`` so the method matches the :class:`ChannelHistoryFetcher`
    Protocol. The ``aiohttp`` session is owned by the caller — this class
    does not open or close it (no shared-session refactor; the catch-up
    path keeps passing its boot-scoped session).
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        orchestrator_url: str,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        self._session = session
        self._base = orchestrator_url.rstrip("/")
        self._timeout = timeout or aiohttp.ClientTimeout(
            total=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )

    async def fetch(
        self, channel_id: str, *, limit: int,
    ) -> list[dict[str, Any]] | None:
        """``GET /api/v1/channels/{id}/messages?limit=N`` → message list,
        or ``None`` on error."""
        url = (
            f"{self._base}/api/v1/channels/{quote(channel_id, safe='')}"
            f"/messages?limit={int(limit)}"
        )
        try:
            async with self._session.get(url, timeout=self._timeout) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "channels: catch-up history %s returned HTTP %d: %s",
                        channel_id, resp.status, body[:256],
                    )
                    return None
                data = await resp.json()
        except Exception as exc:
            logger.warning(
                "channels: catch-up history %s failed: %s",
                channel_id, exc,
            )
            return None
        messages = data.get("messages")
        if not isinstance(messages, list):
            return []
        return messages
