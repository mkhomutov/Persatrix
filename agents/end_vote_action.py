"""The RFC 0030 Layer 4 vote producer — END_INTERACTION_VOTE publishing.

Carved out of :mod:`agents.action_executor` so that file stays under the
500-line review cap (the ``channel_reply`` / ``wallet_cause`` split
convention). One free function: the executor's
``ActionType.END_INTERACTION_VOTE`` arm delegates here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .channel_publisher import (
    DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ChannelPublisher,
    ChannelsDisabledError,
)
from .channel_wire_metadata import DispatchContext
from .persona_types import VOTE_CLOSE_TOKEN_KEY, AgentAction

if TYPE_CHECKING:
    from .dispatch import EventDispatcher

logger = logging.getLogger(__name__)

# The readable sign-off an END_INTERACTION_VOTE publishes when the persona
# supplies no content of its own (producer plan PR 2, IP6). The vote is a real
# message other participants read — an empty publish would look like a glitch
# rather than a deliberate "nothing further from me".
_END_VOTE_DEFAULT_CONTENT = "I have nothing further to add."

# DM channels are identified by the ``dm:`` channel-id prefix — the same
# convention as ``response_gate.py`` / ``channel_reply.py``. Re-declared
# rather than imported for the same reason ``channel_reply.py`` gives:
# importing it would couple this executor-side module to the persona-runtime
# package for one literal.
_DM_CHANNEL_PREFIX = "dm:"


async def publish_end_interaction_vote(
    publisher: ChannelPublisher | None,
    sender_id: str,
    action: AgentAction,
    *,
    context: DispatchContext,
) -> dict[str, Any]:
    """Publish an RFC 0030 Layer 4 end-of-interaction vote (producer plan
    PR 2, IP6).

    The vote is a REAL channel message — the orchestrator's
    ``processEndVote`` runs post-persistence, scoping the vote to its own
    resolved interaction (IP2), never to anything this side claims — with
    the ``end_interaction_vote: true`` flag merged into the publish
    metadata (the literal mirrors Go's ``endVoteMetadataKey``; pinned by
    the cross-language drift test). Mentions stay empty: a vote addresses
    the room's process, not a member, and must not direct the floor.
    ``context.cascade_depth`` rides verbatim, the send-branch posture.

    A same-channel vote also echoes the context's origin interaction id as
    the wire ``interaction_id`` claim — the RFC 0052 no-reopen latch input (PR #716
    review; see ``ActionExecutor.execute``). A vote is post-persistence
    channel traffic like any reply, so a vote straggling in after a bounded
    close must latch rather than mint fresh and re-fan; the resolver still
    scopes a LIVE vote to its own resolved interaction (IP2 — the claim
    never keys quorum state).

    The legacy in-process dispatcher path keeps the pre-producer
    ``not_implemented`` status — votes are a channels-governance concept
    and the chat path has no interaction router. A vote with no channel
    (the bind seam never fired — e.g. emitted on a TICK turn) cannot be
    scoped to an interaction and is dropped with a distinct status; the
    same strip-then-test as the bind seam, so a whitespace-only claim
    cannot slip past both checks into a junk-channel publish. A vote
    into a DM is dropped too: the prompt snippet's "never vote in a
    direct message" is enforced here as a code gate (the repo's DM
    invariants — must-reply, the ellipsis fallback — all live in code,
    with the prompt as guidance), because the orchestrator's
    ``processEndVote`` has no channel-type exemption and would count a
    DM vote toward a quorum.
    """
    target_channel = str(action.payload.get("channel_id", "") or "").strip()
    # The decide-time park's correlation handle (PR 607 second-pass
    # review): stamped onto the action payload by
    # ``persona_runtime/vote_close.park_end_vote_close``, echoed verbatim
    # on every channel-carrying status so the outcome callback discharges
    # the park that stamped THIS vote — and never published (the wire
    # message below builds its own metadata).  "" for a vote no park
    # covers (a threaded turn, an exempted scope).
    close_token = str(action.payload.get(VOTE_CLOSE_TOKEN_KEY, "") or "")
    if publisher is None:
        logger.info(
            "Agent %s voted to end the interaction but no REST publisher "
            "is configured (legacy in-process path) — vote not published",
            sender_id,
        )
        # ``channel_id`` carried (when bound) so the executor's outcome
        # callback can drop the voter's parked local close — the legacy
        # path publishes nothing, so nothing must read as "ended".
        return {
            "action_type": "end_interaction_vote",
            "status": "not_implemented",
            "channel_id": target_channel,
            "vote_close_token": close_token,
        }
    if not target_channel:
        logger.warning(
            "Agent %s END_INTERACTION_VOTE has no channel_id (non-channel "
            "turn?); a vote cannot be scoped to an interaction — dropped",
            sender_id,
        )
        return {"action_type": "end_interaction_vote", "status": "no_channel_id"}
    if target_channel.startswith(_DM_CHANNEL_PREFIX):
        logger.warning(
            "Agent %s END_INTERACTION_VOTE targets DM channel %s; a DM has "
            "no group discussion to close (see the end-interaction-vote "
            "prompt snippet) — dropped",
            sender_id, target_channel,
        )
        return {
            "action_type": "end_interaction_vote",
            "status": "dm_channel",
            "channel_id": target_channel,
            "vote_close_token": close_token,
        }

    content = str(action.payload.get("content", "") or "").strip()
    if not content:
        content = _END_VOTE_DEFAULT_CONTENT
    metadata: dict[str, Any] = {"end_interaction_vote": True}
    # The RFC 0052 no-reopen claim, via the context's shared rule (see
    # ``DispatchContext.same_channel_claim``). The ``synthesis_reply`` echo
    # rides structurally off the context (PR #718 review): a chair may
    # legitimately answer the §D synthesis directive with a vote whose
    # content IS the synthesis (the ISSUE-0099 outcome-(a) shape) — the echo
    # makes that publish claimable as the closing artifact, same as the
    # plain-reply path.
    claim = context.same_channel_claim(target_channel)
    if claim:
        metadata.update(claim)
    # Whether the publish below rides claimable as the synthesis reply —
    # derived from the claim actually stamped on the wire, not re-derived
    # from the context, so the result flag can never disagree with what Go's
    # fanout-head claim saw (PR #718 review, OQ #6: the discharge's metering
    # input — see the "published" return).
    synthesis_reply = claim is not None and "synthesis_reply" in claim
    try:
        await asyncio.wait_for(
            publisher.publish(
                channel_id=target_channel,
                sender_id=sender_id,
                content=content,
                mentions=[],
                cascade_depth=context.cascade_depth,
                metadata=metadata,
            ),
            timeout=DEFAULT_PUBLISH_TIMEOUT_SECONDS,
        )
    except ChannelsDisabledError:
        logger.debug(
            "END_INTERACTION_VOTE short-circuited (channels disabled at "
            "orchestrator): agent=%s channel=%s",
            sender_id, target_channel,
        )
        return {
            "action_type": "end_interaction_vote",
            "status": "channels_disabled",
            "channel_id": target_channel,
            "vote_close_token": close_token,
        }
    except Exception as exc:  # noqa: BLE001 — surfaced via "failed" status
        logger.warning(
            "End-interaction vote from %s to %s failed: %s",
            sender_id, target_channel, exc,
            exc_info=not isinstance(exc, TimeoutError),
        )
        return {
            "action_type": "end_interaction_vote",
            "status": "failed",
            "channel_id": target_channel,
            "vote_close_token": close_token,
        }
    return {
        "action_type": "end_interaction_vote",
        "status": "published",
        "channel_id": target_channel,
        "vote_close_token": close_token,
        # PR #718 review (OQ #6): True iff the publish above carried the
        # ``synthesis_reply`` echo — when the arm still stands, Go claims
        # that vote as the §D closing artifact BEFORE processEndVote. The
        # executor's outcome callback threads this to the discharge, which
        # then WITHHOLDS its parked local close: the echo says only what
        # the wire carried, never whether Go accepted the claim (a consumed
        # or abandoned arm demotes the vote to an ordinary one, possibly on
        # an interaction deliberately left open), so the close and its
        # metered RFC 0020 summary belong to the close-notification
        # self-echo Go fans iff it closed (``vote_close.py``). Only the
        # "published" status carries the key: a failed publish rode no wire.
        "synthesis_reply": synthesis_reply,
    }


async def notify_end_vote_outcome(
    dispatcher: EventDispatcher | None, agent_id: str, result: dict[str, Any],
) -> None:
    """Discharge the voter's parked local close with the publish outcome
    (PR 607 review finding 5).

    The decide-time path parks the END_INTERACTION_VOTE's local
    interaction close instead of executing it (the vote has not been
    published yet — see ``agents/persona_runtime/vote_close.py``); this
    callback reports how the publish went so the park is closed
    (``status == "published"``) or dropped (any failure status, so a
    vote that never reached the orchestrator leaves no early "ended"
    record).  Best-effort: no dispatcher / unknown agent / an agent
    without the vote-close seam / missing ``channel_id`` (the
    ``no_channel_id`` drop) simply leaves the park to the staleness
    guards, and a callback error must not fail the action whose
    publish already succeeded.

    Lived on :class:`~agents.action_executor.ActionExecutor` as
    ``_notify_end_vote_outcome`` until ISSUE-0118's scope threading pushed
    that module past the 500-line cap; moved here beside its publish
    sibling (same carve rationale as the module docstring), taking the
    executor's dispatcher as a parameter.
    """
    if dispatcher is None:
        return
    agent = dispatcher.get_agent(agent_id)
    if agent is None:
        return
    channel_id = str(result.get("channel_id", "") or "")
    if not channel_id:
        return
    # The dispatcher registry is typed for persona agents but not
    # enforced; an agent without the seam has no parked closes to
    # discharge — skip, keeping the except's WARNING for discharges
    # that actually fail (PR 607 third-pass review).
    resolve = getattr(agent, "resolve_end_vote_publish", None)
    if resolve is None:
        return
    try:
        await resolve(
            channel_id,
            published=result.get("status") == "published",
            # The park's correlation handle (PR 607 second-pass
            # review), echoed by publish_end_interaction_vote off the
            # action payload — "" for a vote the park never stamped,
            # which the discharge treats as not-mine.
            token=str(result.get("vote_close_token", "") or ""),
            # PR #718 review (OQ #6): a published vote that rode the
            # ``synthesis_reply`` echo IS claimable as the §D closing
            # artifact — the discharge marks the chair's record for the
            # metered close summary. Strict ``is True``: only the
            # "published" result carries the key, so a failure status
            # reads unmarked.
            synthesis_reply=result.get("synthesis_reply") is True,
        )
    except Exception:
        logger.warning(
            "End-vote publish-outcome callback failed for agent %s "
            "(channel %s)", agent_id, channel_id, exc_info=True,
        )
