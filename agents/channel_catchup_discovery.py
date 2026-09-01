"""Which channels a catch-up pass replays, and under what policy.

Split out of :mod:`agents.channel_catchup` (v0.3.15 PR B2 review), which
sat at the 500-line cap ``scripts/checks/file_size.py --strict`` enforces.
The seam is the one the pass itself already has: DISCOVERY — the two REST
reads that answer "which rooms is this agent in, and how does it respond
there?" — runs to completion before a single row is replayed, and it is
the half that knows nothing about interactions, tracker records or
derivation.  ``channel_catchup`` keeps the replay loop and its ISSUE-0130
completeness bookkeeping; the history rows themselves already come from
:mod:`agents.channel_history_fetcher`.

Every function here is best-effort by contract: ``None`` means "failed,
already logged at WARN", and the caller skips that channel rather than
aborting the pass.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import aiohttp

__all__ = [
    "fetch_channel_list",
    "fetch_channel_membership",
    "resolve_respond_policy",
]

logger = logging.getLogger(__name__)

# PR-265 review L1 (first pass): ``GET /api/v1/channels`` falls back
# to ``channelDefaultListLimit = 50`` server-side when no ``?limit=``
# is supplied (``internal/server/channel_handlers.go``); an agent
# enrolled in >50 channels would silently miss catch-up for the tail
# of its membership. We pin the request to ``channelMaxLimit = 1000``
# (the orchestrator clamp) so the explicit cap is the contract — the
# silent default cannot drift on either side without breaking the
# request URL.
_CHANNEL_LIST_LIMIT: int = 1000


async def fetch_channel_list(
    session: aiohttp.ClientSession,
    base: str,
    timeout: aiohttp.ClientTimeout,
) -> list[dict] | None:
    """``GET /api/v1/channels?limit=N`` → list of channel JSON, or
    ``None`` on error.  ``None`` means "best-effort failure already
    logged".

    PR-265 review L1: the explicit ``?limit=`` is mandatory — without
    it the Go orchestrator returns at most ``channelDefaultListLimit =
    50`` channels, silently capping catch-up for high-fanout agents.
    """
    url = f"{base}/api/v1/channels?limit={_CHANNEL_LIST_LIMIT}"
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    "channels: catch-up list returned HTTP %d: %s",
                    resp.status, body[:256],
                )
                return None
            data = await resp.json()
    except Exception as exc:
        logger.warning("channels: catch-up list failed: %s", exc)
        return None
    channels = data.get("channels")
    if not isinstance(channels, list):
        return []
    return channels


async def fetch_channel_membership(
    session: aiohttp.ClientSession,
    base: str,
    channel_id: str,
    timeout: aiohttp.ClientTimeout,
) -> list[dict] | None:
    """``GET /api/v1/channels/{id}`` → member list, or ``None`` on error."""
    url = f"{base}/api/v1/channels/{quote(channel_id, safe='')}"
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    "channels: catch-up get-channel %s returned HTTP %d: %s",
                    channel_id, resp.status, body[:256],
                )
                return None
            data = await resp.json()
    except Exception as exc:
        logger.warning(
            "channels: catch-up get-channel %s failed: %s",
            channel_id, exc,
        )
        return None
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return members


def resolve_respond_policy(
    members: list[dict], agent_id: str,
) -> str | None:
    """Return the agent's ``respond`` policy from the member list, or
    ``None`` when the agent is not a member.
    """
    for m in members:
        if m.get("id") == agent_id:
            policy = m.get("respond")
            if isinstance(policy, str) and policy:
                return policy
            return "when_mentioned"
    return None
