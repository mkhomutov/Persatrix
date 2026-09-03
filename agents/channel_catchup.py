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
* **No watermark; dedup at the DERIVATION boundary, not at ingest.**
  Per OQ #8, the ``?since=`` watermark and per-tick recovery are still
  deferred, so the fetcher re-ingests the last-N window on every boot
  and ``InteractionTracker.add_turn`` still appends every turn without
  deduplicating by ``message_id``. What v0.3.15 PR B2 added is one
  step further down: a replayed span that would derive an episode
  identical to one an earlier boot already derived does not derive it
  again (``persona_runtime.replay_identity``). The ingest duplication
  is therefore still real and still visible in ``turn_count``, and it
  is bounded where it used to compound — in the persisted memory.
  (PR-265 review L5: earlier "idempotent in shape" wording was
  misleading — it suggested ingest dedup; the tracker doesn't.)
* **Catch-up → live boundary.** Replay events carry no ``chat_end`` /
  ``session_end`` metadata, so PR-265 review L6 left the scopes they
  open bleeding into the next live turn. ISSUE-0130 closed that:
  a replay-opened span derives under its own attribution or not at
  all, so a live turn joining it would be summarised as part of a
  span it does not belong to — and, before shape (b) seeded the
  principal, under a tenant that was not its own. Both ends now
  close with ``REASON_CATCHUP_COMPLETE`` —
  every replay-opened scope at pass end, plus any a live turn reaches
  first (dispatch is already serving while catch-up runs), which the
  persona splits on ingest. ``Interaction.started_at`` is still *boot*
  time, not the oldest replayed wire timestamp, so cross-checks against
  the earliest turn stay off by the window until the watermark revision.

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

import aiohttp

from .channel_catchup_discovery import (
    fetch_channel_list,
    fetch_channel_membership,
    resolve_respond_policy,
)
from .channel_history_fetcher import HttpChannelHistoryFetcher
from .channel_replay_event import build_replay_event
from .channel_replay_outcome import ReplayPassOutcome
from .channel_validation import validate_channel_message_dict
from .persona_types import AgentEvent

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

    def note_replay_gap(self, channel_id: str, speaker_id: str) -> None:
        """Tell the agent a replayed ROW never reached its tracker.

        Required, not optional: the pass ``outcome`` below is read only at
        pass END, while the ingest-time segmentation door closes and
        DERIVES a record mid-pass, so
        ``close_path.close_stale_records`` has to learn the gap from the
        tracker instead.  An implementer that dropped it would silently
        derive a span with a hole in it, so this Protocol asks for it and
        the type checker holds every caller to it.
        """
        ...


async def replay_channel_history(
    *,
    agent: _AgentLike,
    orchestrator_url: str,
    session: aiohttp.ClientSession,
    limit: int = DEFAULT_CATCHUP_LIMIT,
    outcome: ReplayPassOutcome | None = None,
) -> None:
    """Fetch recent channel history and replay it through the agent.

    See module docstring for the contract. This function never raises;
    every failure path is best-effort with a WARN log line.

    ``outcome`` records what the pass learned about its own completeness —
    the ISSUE-0130 (b) derivation gate (``close_replayed_scopes``), since
    a record holding a prefix of its window would claim a span identity no
    later boot can recompute.  It is MUTATED IN PLACE and never returned,
    exactly like ``counts``: the caller owns it and reads it in a
    ``finally``, so a partial pass still reports what finished.  See
    :class:`ReplayPassOutcome` for why completeness has two axes.

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
    if outcome is None:
        outcome = ReplayPassOutcome()
    try:
        await asyncio.wait_for(
            _replay_channel_history_inner(
                agent=agent,
                orchestrator_url=orchestrator_url,
                session=session,
                limit=limit,
                counts=counts,
                outcome=outcome,
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
        # The channels that finished BEFORE the budget ran out are
        # complete and derive normally; only the one it cut off (and any
        # it never reached) is held back.


async def _replay_channel_history_inner(
    *,
    agent: _AgentLike,
    orchestrator_url: str,
    session: aiohttp.ClientSession,
    limit: int,
    counts: dict[str, int],
    outcome: ReplayPassOutcome,
) -> None:
    """Wall-clock-budget-wrapped body of :func:`replay_channel_history`.

    Mutates ``counts`` and ``outcome`` in-place so the outer wrapper
    can surface per-pass totals on both the success-INFO and budget-WARN
    paths (the latter needs to log how far the partial pass got before
    the cancellation). Mutation is intentional — returning a tuple from a
    function that may be cancelled mid-execution would lose the
    partial progress, and ``outcome`` is load-bearing on exactly that
    path: the channels finished before a budget overrun are the ones the
    ISSUE-0130 (b) derivation gate must still let through.
    """
    base = orchestrator_url.rstrip("/")
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
    history_fetcher = HttpChannelHistoryFetcher(
        session=session, orchestrator_url=orchestrator_url, timeout=timeout,
    )

    channels = await fetch_channel_list(session, base, timeout)
    if channels is None:
        return

    for ch in channels:
        channel_id = ch.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            continue

        membership = await fetch_channel_membership(
            session, base, channel_id, timeout,
        )
        if membership is None:
            continue
        respond_policy = resolve_respond_policy(membership, agent.agent_id)
        if respond_policy is None:
            # Agent is not a member — skip without fetching history.
            continue

        # RFC 0036 §G: scope the replay to the agent's membership stints via
        # as_participant, so episodic seeding excludes pre-join / removal-gap
        # messages — a re-added persona does not re-ingest the gap it missed.
        # The current-state member check above is the cheap pre-filter (skip
        # non-member channels entirely); this scopes the *rows* server-side.
        messages = await history_fetcher.fetch(
            channel_id, limit=limit, as_participant=agent.agent_id,
        )
        if messages is None:
            continue
        counts["channels"] += 1

        channel_type = ch.get("channel_type")
        if not isinstance(channel_type, str):
            channel_type = ""

        # ISSUE-0130 (b): this channel's window counts as replayed to
        # COMPLETION only if every row in it reached the tracker.  A row
        # dropped by the validator does not disqualify it — that is
        # deterministic, so the next boot drops the same row and computes
        # the same span identity — but a row whose ``on_event`` RAISED
        # does: the record then holds a gap this boot invented, and
        # deriving from it claims an id no later boot recomputes.
        channel_complete = True
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
            event = build_replay_event(msg, channel_id, respond_policy, ch)
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
                # PER SENDER where the row names one (PR B2 review round
                # 3).  The hole this leaves is in the record keyed by THAT
                # sender, and only that one — so disqualifying the whole
                # channel made one deterministically raising row cost every
                # other speaker in the room their derivation, on every boot,
                # with nothing to distinguish it from replay having stopped.
                # A row whose sender cannot be read is the case that still
                # takes the channel down: an unattributable gap could be in
                # any record.
                sender = msg.get("sender_id")
                if isinstance(sender, str) and sender:
                    outcome.speaker_gaps.add((channel_id, sender))
                else:
                    channel_complete = False
                # BOTH derivation doors have to learn this, and the outcome
                # above only reaches the pass-end sweep (PR B2 review).  A
                # blank sender is a gap that could be in any record, so the
                # agent-side note takes the whole channel down.
                agent.note_replay_gap(
                    channel_id, sender if isinstance(sender, str) else "",
                )
                continue
            counts["events"] += 1
        if channel_complete:
            outcome.completed.add(channel_id)


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
        # Owned HERE, and mutated in place by the call below, so the
        # ``finally`` still sees what finished even when the call raises
        # something its own timeout branch does not catch.
        outcome = ReplayPassOutcome()
        try:
            await replay_channel_history(
                agent=agent,
                orchestrator_url=orchestrator_url,
                session=session,
                limit=limit,
                outcome=outcome,
            )
        except Exception:
            logger.exception(
                "channels: catch-up replay aborted for agent %s", agent_id,
            )
        finally:
            # ISSUE-0130: pop the scopes this pass opened — in ``finally`` so a
            # budget overrun closes them too, or the next LIVE turn merges
            # into one.  Best-effort, does not raise:
            # ``close_path.close_replayed_scopes``.
            #
            # ``derive_channels`` carries which channels actually FINISHED
            # (v0.3.15 PR B2 review).  A channel cut short by the
            # wall-clock budget or by a raising row has ingested a PREFIX
            # of its window, and the shape-(b) span identity is computed
            # from the turns the record holds — so deriving that prefix
            # claims an id no later boot can ever recompute, and the next
            # complete boot derives the whole window again on top of it.
            # That is not the documented "moved window" residual; the
            # window never moved.  Closing without deriving costs this
            # boot's derivation, which catch-up re-reads anyway (no
            # watermark, RFC 0011 OQ #8), and keeps the identity honest.
            #
            # PER CHANNEL, because that is the granularity of the hazard:
            # the first cut passed one boolean for the whole agent, which
            # threw away every completed channel's window whenever any
            # later channel overran, and still said "complete" for a
            # window a raising ``on_event`` had left a hole in.
            await agent.close_replayed_interactions(
                derive_channels=frozenset(outcome.completed),
                speaker_gaps=frozenset(outcome.speaker_gaps),
            )
