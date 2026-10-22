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

Contract: :meth:`fetch` returns the channel's message list on success,
``[]`` when the response body is not a JSON object or its ``messages``
field is absent / not a list, and ``None`` on any HTTP 4xx/5xx or
transport error (logged WARN). ``None`` means "best-effort failure
already logged" — callers branch on it rather than catching an
exception. The one exception is a 429 for a fetcher built with
``raise_on_rate_limit=True``, as the catch-up's is: it raises
:class:`OrchestratorRateLimitedError` so the pass stops (ISSUE-0187).

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
    "OrchestratorRateLimitedError",
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


class OrchestratorRateLimitedError(Exception):
    """The orchestrator refused a read for going over this agent's budget.

    Raised only where a caller opts in (the start-up catch-up), so that it
    can stop rather than collect one refusal per remaining channel: each
    refusal counts toward the agent's quarantine (ISSUE-0187).
    """


class ChannelHistoryFetcher(Protocol):
    """Minimum surface a caller needs to pull a channel's recent history.

    Structural :class:`typing.Protocol` (not :class:`abc.ABC`) so a test
    fake is a duck-typed object without inheritance ceremony. Not
    decorated ``@runtime_checkable`` — callers depend on static typing
    only; there is no ``isinstance`` site against this Protocol.
    """

    async def fetch(
        self, channel_id: str, *, limit: int,
        as_participant: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Return the last ``limit`` messages for ``channel_id``.

        ``[]`` when the channel is empty, ``None`` on a best-effort
        failure (already logged WARN).

        ``as_participant`` (RFC 0036 §G) scopes the history to one
        participant's channel-membership stints: when set, the server
        returns only messages from windows the participant was present
        for, so a re-added persona's window excludes its removal-gap
        messages. ``None`` (the human / CLI default) requests the full
        unscoped history — byte-identical to the pre-RFC-0036 contract.
        """
        ...


class HttpChannelHistoryFetcher:
    """Production :class:`ChannelHistoryFetcher` backed by ``aiohttp``.

    The :meth:`fetch` body issues the
    ``GET /api/v1/channels/{id}/messages?limit=N`` request originally
    lifted from the former ``agents.channel_catchup._fetch_channel_history``
    helper — same ``None``-on-error / list-on-success contract.

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
        raise_on_rate_limit: bool = False,
    ) -> None:
        self._session = session
        # A 429 raises OrchestratorRateLimitedError instead of returning None.
        self._raise_on_rate_limit = raise_on_rate_limit
        self._base = orchestrator_url.rstrip("/")
        self._timeout = timeout or aiohttp.ClientTimeout(
            total=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )

    async def fetch(
        self, channel_id: str, *, limit: int,
        as_participant: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """``GET /api/v1/channels/{id}/messages?limit=N`` → message list,
        or ``None`` on error.

        When ``as_participant`` is set it is percent-encoded and appended
        as ``&as_participant=`` (RFC 0036 §G), so the server scopes the
        history to that participant's membership stints. A falsy value
        (``None`` or ``""``) omits the param entirely — the full unscoped
        history, matching the Go handler's blank-is-absent treatment.
        """
        url = (
            f"{self._base}/api/v1/channels/{quote(channel_id, safe='')}"
            f"/messages?limit={int(limit)}"
        )
        if as_participant:
            url += f"&as_participant={quote(as_participant, safe='')}"
        try:
            async with self._session.get(url, timeout=self._timeout) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "channels: history fetch %s returned HTTP %d: %s",
                        channel_id, resp.status, body[:256],
                    )
                    if resp.status == 429 and self._raise_on_rate_limit:
                        raise OrchestratorRateLimitedError(channel_id)
                    return None
                data = await resp.json()
            # Shape guard inside the request ``try``: a 2xx response whose
            # JSON body is not an object (a bare array, string, number, or
            # ``null``) must degrade to ``[]`` like a ``dict`` with an
            # unusable ``messages`` field — not raise ``AttributeError`` on
            # ``data.get`` and escape the "never raises across the seam"
            # contract the conversation-window caller depends on.
            messages = data.get("messages") if isinstance(data, dict) else None
        except OrchestratorRateLimitedError:
            raise
        except Exception as exc:
            logger.warning(
                "channels: history fetch %s failed: %s",
                channel_id, exc,
            )
            return None
        if not isinstance(messages, list):
            return []
        return messages
