"""On-startup channel catch-up fetch (RFC 0011 PR 5 follow-up).

Resolves [RFC 0011 OQ #8](docs/rfcs/0011-channels-bridges.md):
"Missed-message recovery protocol". On persona-runtime boot, each
subscribed channel is queried for the most recent N messages
(``GET /api/v1/channels/{id}/messages?limit=50``) and replayed through
the agent's ``on_event`` with ``metadata["replay_mode"] = True`` so the
action-loop's replay-mode short-circuit ingests memory **without**
firing the LLM and **without** producing an outbound
``SEND_CHANNEL_MESSAGE``.

Behaviour:

* **Best-effort.** A transport or HTTP error on any endpoint logs a
  warning and continues — startup must not block on a flapping
  orchestrator. The agent accepts at-most-once delivery; the missed
  catch-up window becomes an operational signal via
  ``channel.delivery.missed`` (RFC 0011 OQ #2).
* **Membership filter.** The fetcher only replays history for channels
  where the agent appears in the member list. Pulling history for
  non-member channels would (a) waste ~50 messages × N channels of REST
  traffic and (b) silently ingest content the agent never received via
  the live dispatch path.
* **Oldest-first replay.** The orchestrator returns history newest-first
  (RFC 0011 §C); the fetcher reverses before replay so
  ``InteractionTracker`` sees turns in conversational order.
* **No watermark.** Per OQ #8, watermark + per-tick recovery is
  deferred until operational data justifies it. v0.3.0 ships
  on-startup last-N as the only catch-up trigger; that means the
  fetcher may re-ingest messages the agent already saw on a previous
  run. The action-loop's defense-in-depth ``sender_id == agent_id``
  skip handles the agent's own outbound; for peer messages, the
  ``InteractionTracker`` gracefully accepts the duplicate turns (same
  scope, ``add_turn`` is idempotent in shape) and the worst observable
  effect is a slightly inflated ``turn_count`` on the first
  post-restart interaction.

The module is independent of :mod:`agents.persona_runtime` so a
non-persona task agent that opted into channel membership could call
the fetcher too — the only contract on the agent is ``agent_id`` and
``async on_event(event)``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import aiohttp

from .channel_validation import parse_channel_timestamp
from .persona_types import AgentEvent, EventType

if TYPE_CHECKING:
    from .base import BaseAgent

__all__ = [
    "DEFAULT_CATCHUP_LIMIT",
    "replay_channel_history",
    "replay_for_persona_agents",
]

logger = logging.getLogger(__name__)


# Default page size pinned at 50 to match
# ``internal/server/channel_handlers.go::channelDefaultHistoryLimit``
# and the OQ #8 recommendation in RFC 0011. A future change to either
# side should cross-reference the other to keep ingest depth aligned
# with the operator-facing CLI default (`persatrix channel history`
# also defaults to 50).
DEFAULT_CATCHUP_LIMIT: int = 50

# Per-request timeout (seconds). Short enough that a stuck orchestrator
# does not freeze startup for minutes; long enough to tolerate cold-cache
# disk reads on large channel-history rows. Symmetric with
# :data:`agents.channel_publisher.DEFAULT_PUBLISH_TIMEOUT_SECONDS` so the
# two REST surfaces share one tunable mental model.
_REQUEST_TIMEOUT_SECONDS: float = 10.0

# PR-265 review L1: ``GET /api/v1/channels`` falls back to
# ``channelDefaultListLimit = 50`` server-side when no ``?limit=`` is
# supplied (``internal/server/channel_handlers.go``); an agent enrolled
# in >50 channels would silently miss catch-up for the tail of its
# membership. We pin the request to ``channelMaxLimit = 1000`` (the
# orchestrator clamp) so the explicit cap is the contract — the silent
# default cannot drift on either side without breaking the request URL.
_CHANNEL_LIST_LIMIT: int = 1000


class _AgentLike(Protocol):
    """Minimum surface the catch-up fetcher needs from an agent.

    Structural :class:`Protocol` so the fetcher can be exercised by
    lightweight test spies without booting the full persona runtime;
    the production caller passes the
    :class:`agents.persona_runtime._LLMPersonaAgent` instance. Not
    decorated with ``@runtime_checkable`` because the fetcher relies on
    static typing only — there is no ``isinstance`` site against this
    Protocol.
    """

    agent_id: str

    async def on_event(self, event: AgentEvent) -> Any: ...


async def replay_channel_history(
    *,
    agent: _AgentLike,
    orchestrator_url: str,
    session: aiohttp.ClientSession,
    limit: int = DEFAULT_CATCHUP_LIMIT,
) -> None:
    """Fetch recent channel history and replay it through the agent.

    See module docstring for the contract. This function never raises;
    every failure path is best-effort with a WARN log line.
    """
    base = orchestrator_url.rstrip("/")
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)

    channels = await _fetch_channel_list(session, base, timeout)
    if channels is None:
        return

    for ch in channels:
        channel_id = ch.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            continue

        membership = await _fetch_channel_membership(
            session, base, channel_id, timeout,
        )
        if membership is None:
            continue
        respond_policy = _resolve_respond_policy(membership, agent.agent_id)
        if respond_policy is None:
            # Agent is not a member — skip without fetching history.
            continue

        messages = await _fetch_channel_history(
            session, base, channel_id, limit, timeout,
        )
        if messages is None:
            continue

        # The orchestrator returns newest-first; reverse so the agent's
        # InteractionTracker sees the turns in conversational order.
        for msg in reversed(messages):
            event = _build_replay_event(msg, channel_id, respond_policy, ch)
            try:
                await agent.on_event(event)
            except Exception:
                # An agent-side exception on a single replayed event
                # must not abort the rest of the catch-up. The persona
                # runtime swallows its own errors at the
                # ``_store_event_episode`` boundary, so reaching this
                # ``except`` is a programming error somewhere upstream
                # — log and continue.
                logger.exception(
                    "channels: catch-up replay raised on agent=%s channel=%s msg=%s",
                    agent.agent_id, channel_id, msg.get("id", ""),
                )


# ─── Internal helpers ──────────────────────────────────────


async def _fetch_channel_list(
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


async def _fetch_channel_membership(
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


async def _fetch_channel_history(
    session: aiohttp.ClientSession,
    base: str,
    channel_id: str,
    limit: int,
    timeout: aiohttp.ClientTimeout,
) -> list[dict] | None:
    """``GET /api/v1/channels/{id}/messages?limit=N`` → message list,
    or ``None`` on error."""
    url = (
        f"{base}/api/v1/channels/{quote(channel_id, safe='')}"
        f"/messages?limit={int(limit)}"
    )
    try:
        async with session.get(url, timeout=timeout) as resp:
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


def _resolve_respond_policy(
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


async def replay_for_persona_agents(
    *,
    agents: dict[str, BaseAgent],
    orchestrator_url: str,
    session: aiohttp.ClientSession | None,
    limit: int = DEFAULT_CATCHUP_LIMIT,
) -> None:
    """Run catch-up for every persona agent in ``agents``.

    Iterates only over :class:`agents.persona_runtime._LLMPersonaAgent`
    instances — task agents have no ``CHANNEL_MESSAGE`` ingest path.
    Sequential per agent so startup logs stay observably ordered and
    the orchestrator does not see N agents stampede the REST surface
    at boot.

    Best-effort: any exception raised by ``replay_channel_history``
    (which itself is best-effort) is logged and swallowed so a
    flapping orchestrator cannot keep the agent process from booting.
    """
    if session is None:
        return
    # Local import: ``persona_runtime`` is a substantial module graph
    # (LLM client, episodic / working memory, response gate, action
    # loop) and ``channel_catchup``'s public surface
    # (``replay_channel_history``) is intentionally narrower than
    # ``_LLMPersonaAgent`` so a non-persona task agent could opt in.
    # Keeping the persona-runtime dependency local to the one site
    # that needs it preserves that narrower surface for callers who
    # import only ``replay_channel_history``. (PR-265 review L3:
    # there is no actual import cycle — ``persona_runtime`` does not
    # import ``channel_catchup`` — earlier wording was inaccurate.)
    from .persona_runtime import _LLMPersonaAgent

    for agent_id, agent in agents.items():
        if not isinstance(agent, _LLMPersonaAgent):
            continue
        try:
            await replay_channel_history(
                agent=agent,
                orchestrator_url=orchestrator_url,
                session=session,
                limit=limit,
            )
        except Exception:
            logger.exception(
                "channels: catch-up replay aborted for agent %s", agent_id,
            )


def _build_replay_event(
    msg: dict,
    channel_id: str,
    respond_policy: str,
    channel: dict,
) -> AgentEvent:
    """Build a CHANNEL_MESSAGE ``AgentEvent`` matching the shape that
    ``ReceiveChannelMessage`` produces on the live path.

    ``metadata["replay_mode"] = True`` is the marker the action-loop
    short-circuit reads; without it the runtime would treat the row as
    live traffic and fire the LLM.

    PR-265 review S2: the wire ``msg["timestamp"]`` (RFC 3339, set by
    the orchestrator at publish time — see
    ``internal/server/channel_types.go::channelMessageResponse``) is
    parsed to epoch seconds and forwarded into ``AgentEvent.timestamp``.
    Without this, replayed events default to ``time.time()`` at boot,
    which (a) lies to the PR-4 summariser via
    ``Turn.payload["timestamp"]``, (b) writes incorrect ``started_at``
    on episodic rows, and (c) defeats the RFC 0021 P1 now-anchor /
    recency rendering — the runtime would render replayed history as
    "just now" instead of its actual age. The parser is shared with
    ``validate_channel_message_event`` so the live and replay paths
    agree on what counts as a valid timestamp.

    Fallback: a missing or unparseable timestamp falls through to the
    dataclass default (``time.time()``) rather than dropping the row.
    Best-effort catch-up keeps the message in memory; the worst
    observable effect is the recency drift this fix exists to prevent,
    bounded to the small minority of malformed wire rows.
    """
    payload: dict[str, Any] = {
        "content": msg.get("content", ""),
        "channel_type": channel.get("channel_type", ""),
        "mentions": list(msg.get("mentions") or []),
        "respond_policy": respond_policy,
    }
    raw_ts = msg.get("timestamp")
    parsed_ts = (
        parse_channel_timestamp(raw_ts) if isinstance(raw_ts, str) else None
    )
    event_kwargs: dict[str, Any] = {
        "event_type": EventType.CHANNEL_MESSAGE,
        "payload": payload,
        "channel_id": channel_id,
        "sender_id": msg.get("sender_id"),
        "message_id": msg.get("id"),
        "thread_id": msg.get("thread_id") or None,
        "metadata": {"replay_mode": True},
    }
    if parsed_ts is not None:
        event_kwargs["timestamp"] = parsed_ts
    return AgentEvent(**event_kwargs)
