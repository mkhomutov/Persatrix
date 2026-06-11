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
* **No watermark, no dedup.** Per OQ #8, watermark + per-tick
  recovery is deferred. v0.3.0 ships on-startup last-N as the only
  trigger, so the fetcher may re-ingest messages from a previous run.
  Self-sender skip handles own outbound; for peer messages,
  ``InteractionTracker.add_turn`` does **not** deduplicate by
  ``message_id`` — it appends every turn. K consecutive restarts
  within the catch-up window produce ``K × N`` turns on the first
  post-restart interaction. Duplicates share the same wire shape and
  scope, but ``turn_count`` grows linearly with restart count.
  (PR-265 review L5: earlier "idempotent in shape" wording was
  misleading — it suggested dedup; the tracker doesn't.)
* **Lifecycle bleed (catch-up → live).** Replay events open
  ``InteractionTracker`` scopes but do **not** close them: replay
  events lack ``chat_end`` / ``session_end`` metadata, and there is
  no synthetic ``REASON_CATCHUP_COMPLETE``. The open interaction
  stays open until the idle-gap timer fires; the next live
  CHANNEL_MESSAGE in the same scope appends to the catch-up
  interaction. ``Interaction.started_at`` is set to *boot time*, not
  the oldest replayed wire timestamp — cross-checks comparing
  ``started_at`` to the earliest turn timestamp will be off by the
  catch-up window. Design intent for v0.3.0; closing the scope on
  catch-up completion is deferred to a watermark-aware revision.
  PR-265 review L6.

The module is independent of :mod:`agents.persona_runtime` so a
non-persona task agent that opted into channel membership could call
the fetcher too — the only contract on the agent is ``agent_id`` and
``async on_event(event)``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import aiohttp

from .channel_history_fetcher import HttpChannelHistoryFetcher
from .channel_validation import (
    parse_channel_timestamp,
    validate_channel_message_dict,
)
from .channel_wire_metadata import seed_replay_metadata
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

# PR-265 review L1 (first pass): ``GET /api/v1/channels`` falls back
# to ``channelDefaultListLimit = 50`` server-side when no ``?limit=``
# is supplied (``internal/server/channel_handlers.go``); an agent
# enrolled in >50 channels would silently miss catch-up for the tail
# of its membership. We pin the request to ``channelMaxLimit = 1000``
# (the orchestrator clamp) so the explicit cap is the contract — the
# silent default cannot drift on either side without breaking the
# request URL.
_CHANNEL_LIST_LIMIT: int = 1000

# PR-265 review L3 (second pass): per-agent wall-clock budget for the
# whole catch-up pass. ``_REQUEST_TIMEOUT_SECONDS × 2N`` is unbounded
# at the ``channelMaxLimit = 1000`` ceiling; this single-budget cap
# means a slow / hung orchestrator cannot block boot regardless of
# fanout. On overrun, ``replay_channel_history`` logs WARN and
# returns; the surrounding ``asyncio.wait_for`` cancels in-flight
# requests. 60s comfortably covers single-digit-channel agents
# (typical pass: ~50–200ms × 2N) with cold-cache headroom.
_CATCHUP_BUDGET_SECONDS: float = 60.0


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

    PR-265 review L3 (second pass): the body is wrapped in
    :func:`asyncio.wait_for` against ``_CATCHUP_BUDGET_SECONDS`` so a
    slow / hung orchestrator cannot stall boot for the worst-case
    ``10s × 2N`` multiplicative bound. On overrun the helper logs WARN
    and returns; in-flight requests are cancelled cleanly by the
    surrounding wait_for.

    PR-265 review L7 (second pass): one INFO log line at the end of the
    pass so operators can confirm catch-up actually fired — no need to
    join ``channel.messages.replayed`` counter values across channels
    to answer "did boot's catch-up run?".
    """
    started_at = time.monotonic()
    counts = {"channels": 0, "events": 0}
    try:
        await asyncio.wait_for(
            _replay_channel_history_inner(
                agent=agent,
                orchestrator_url=orchestrator_url,
                session=session,
                limit=limit,
                counts=counts,
            ),
            timeout=_CATCHUP_BUDGET_SECONDS,
        )
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.info(
            "channels: catch-up complete agent=%s channels=%d events=%d "
            "elapsed_ms=%.0f",
            agent.agent_id,
            counts["channels"],
            counts["events"],
            elapsed_ms,
        )
    except TimeoutError:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        logger.warning(
            "channels: catch-up exceeded %.0fs wall-clock budget for "
            "agent=%s; partial channels=%d events=%d elapsed_ms=%.0f "
            "(remaining channels skipped)",
            _CATCHUP_BUDGET_SECONDS,
            agent.agent_id,
            counts["channels"],
            counts["events"],
            elapsed_ms,
        )


async def _replay_channel_history_inner(
    *,
    agent: _AgentLike,
    orchestrator_url: str,
    session: aiohttp.ClientSession,
    limit: int,
    counts: dict[str, int],
) -> None:
    """Wall-clock-budget-wrapped body of :func:`replay_channel_history`.

    Mutates ``counts`` in-place so the outer wrapper can surface
    per-pass totals on both the success-INFO and budget-WARN paths
    (the latter needs to log how far the partial pass got before the
    cancellation). Mutation is intentional — returning a tuple from a
    function that may be cancelled mid-execution would lose the
    partial progress.
    """
    base = orchestrator_url.rstrip("/")
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    history_fetcher = HttpChannelHistoryFetcher(
        session=session, orchestrator_url=orchestrator_url, timeout=timeout,
    )

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

        messages = await history_fetcher.fetch(channel_id, limit=limit)
        if messages is None:
            continue
        counts["channels"] += 1

        channel_type = ch.get("channel_type")
        if not isinstance(channel_type, str):
            channel_type = ""

        # The orchestrator returns newest-first; reverse so the agent's
        # InteractionTracker sees the turns in conversational order.
        for msg in reversed(messages):
            # PR-265 review L1 (second pass): mirror the live-path
            # validator. REST surface shares the cleartext gRPC port's
            # TLS-deferred trust boundary; without symmetric validation
            # a MITM (or a future writer that bypasses router
            # validation) could drive malformed payloads through this
            # seam. Per-row WARN + skip is "best-effort with
            # bounds-checking", not "all-or-nothing".
            err, _parsed_ts = validate_channel_message_dict(
                msg, channel_type=channel_type,
            )
            if err is not None:
                logger.warning(
                    "channels: catch-up dropped malformed row "
                    "agent=%s channel=%s msg=%s reason=%s",
                    agent.agent_id, channel_id, msg.get("id", ""), err,
                )
                continue
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
                continue
            counts["events"] += 1


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
    — task agents have no ``CHANNEL_MESSAGE`` ingest path. Sequential
    per agent so startup logs stay ordered and N agents do not
    stampede the orchestrator's REST surface at boot. Best-effort:
    any escaping exception is logged and swallowed.
    """
    if session is None:
        return
    # Local import keeps the persona-runtime module graph (LLM client,
    # memory, gate, action loop) out of the public surface for callers
    # that only need ``replay_channel_history`` — a non-persona task
    # agent could opt in. (PR-265 first-pass L3: no actual import
    # cycle; persona_runtime does not import channel_catchup.)
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

    PR-265 review S2: wire ``msg["timestamp"]`` (RFC 3339, set by the
    orchestrator at publish time — see
    ``internal/server/channel_types.go::channelMessageResponse``) is
    parsed to epoch seconds and forwarded into
    ``AgentEvent.timestamp``. Without this, replayed events default to
    ``time.time()`` at boot, defeating RFC 0021 P1 now-anchor / recency
    rendering, poisoning ``Turn.payload["timestamp"]``, and writing
    wrong ``started_at`` on episodic rows. Shared parser with
    ``validate_channel_message_event``.

    Fallback (post-PR-265 L1 second pass): malformed timestamps cannot
    reach this function — the catch-up loop runs every row through
    ``validate_channel_message_dict`` first. The ``parsed_ts is None``
    branch below is defense-in-depth against an impossible state.

    PR-265 review L2: ``thread_parent_sender_id`` is intentionally
    **not** propagated. The field exists on the live proto but **not**
    on ``channelMessageResponse`` JSON shape — nothing to forward.
    Documented gap, not a defect: the only in-tree consumer (the
    response gate) is bypassed by the replay short-circuit. Future
    threading-aware consumers will need a Go-side schema bump.

    PR 607 second-pass review: the row's wire interaction keys
    (``interaction_id`` + the OQ 5 close-cause pair) ARE propagated,
    re-validated by :func:`agents.channel_wire_metadata
    .seed_replay_metadata` with the live seed point's exact rules.
    Without them a replayed span covering a vote-closed conversation
    and the channel's next topic merges into one local record, and the
    merged record opens with no wire id — the first LIVE id then reads
    as adoption-not-rotation, silently disarming the RFC 0030 close
    propagation after every restart.  Replayed rotation closes do run
    the close-path summariser at boot; those conversations genuinely
    closed, so the records (and their one-time summary cost) are the
    feature working, not replay overhead.
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
    metadata: dict[str, Any] = {"replay_mode": True}
    seed_replay_metadata(metadata, msg.get("metadata"))
    event_kwargs: dict[str, Any] = {
        "event_type": EventType.CHANNEL_MESSAGE,
        "payload": payload,
        "channel_id": channel_id,
        "sender_id": msg.get("sender_id"),
        "message_id": msg.get("id"),
        "thread_id": msg.get("thread_id") or None,
        "metadata": metadata,
    }
    if parsed_ts is not None:
        event_kwargs["timestamp"] = parsed_ts
    return AgentEvent(**event_kwargs)
