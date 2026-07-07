"""Publish-confirmed local close for the END_INTERACTION_VOTE voter.

PR 607 review finding 5, implemented with producer plan OQ 5: the voter's
local interaction used to close at *decide* time — inside
``_EpisodeRoutingMixin._handle_multi_turn_event``, which runs at action-loop
step 6, BEFORE the executor publishes the vote.  The persona's judgement was
real either way, but the publish can fail (timeout, ``ChannelsDisabledError``,
no REST publisher on the legacy in-process path), and a vote that never
reached the orchestrator counts toward no quorum — the decide-time close left
an early "ended" record for a conversation that, on the wire, never ended.

The seam is two-phase:

* **Decide time** (:func:`park_end_vote_close`, called from
  ``episode_routing``): when the turn's actions vote to end the event's
  conversation (:func:`.interaction_boundary.matching_end_votes`) and the
  resolved scope is a GROUP scope (the only kind with a vote-closeable
  conversation — DM and thread scopes never park, the same scope-kind
  basis the wire seam uses), the close is PARKED instead of executed.
* **Publish outcome** (``agents/action_executor.py``): after
  ``publish_end_interaction_vote`` returns, the executor calls back into the
  voter (``_LLMPersonaAgent.resolve_end_vote_publish`` →
  :func:`discharge_end_vote_publish`).  ``status == "published"`` closes the
  parked scope with ``REASON_STRUCTURAL`` and persists, exactly what the
  decide-time close did; failure statuses drop the park once every
  in-flight vote has reported — the record stays open and closes later
  through the ordinary boundaries (the wire id rotation once a real
  quorum forms, or the idle gap).  The one carve-out: a published vote
  carrying the ``synthesis_reply`` echo closes nothing here — the
  close-notification self-echo owns that close (see the discharge's
  docstring; PR #718 review, twice).

Park/discharge identity (PR 607 second-pass review): the park is
correlated to the votes it covers, not merely to the channel.  The park
stamps one fresh token (:data:`~agents.persona_types.VOTE_CLOSE_TOKEN_KEY`)
onto every matching vote action and remembers how many it stamped;
``end_vote_action`` echoes the token from the action payload into the
result dict (never onto the wire), and the discharge acts only on a
matching token.  This kills two confirmed cross-fires of the
channel-only key: (a) a failed duplicate vote popping the park its
successful sibling still needs — the failure now consumes one in-flight
slot, and the park survives until a success closes it or every slot
fails; (b) a stranded park (the publish coroutine was cancelled before
the callback ran) being discharged by a LATER vote's outcome — a
threaded vote the seam exempts carries no token, an overwriting park's
votes carry a different one.

Re-vote dedup mirror (PR 607 second-pass review): Go's vote gate counts
a participant once per interaction — an in-window re-vote is suppressed
but still commits, so its publish reports ``published``.  The discharge
mirrors that idempotency with a per-scope memory of the wire id it last
vote-closed: a re-vote on the SAME wire interaction (the scope reopened
on continued traffic, stamped with the unrotated id) closes nothing —
the record stays open exactly like Go's interaction — instead of
minting a second "ended" record per re-vote.  A record with no wire id
(legacy/untracked traffic) has nothing to compare and keeps the
pre-memory behaviour.  The memory holds one entry per scope and is
deliberately never pruned (PR 607 third-pass review): a stale entry is
inert — the resolver never re-uses a retired wire id, so it can never
equal a later record's id — while a prune at the wrong moment would
re-enable exactly the fragmentation this map exists to stop; the bound
is the scopes the agent has ever vote-closed, the park map's own bound.

Staleness guards: the park stores the open interaction's id, and the
discharge closes only when the scope still holds that same open
interaction — a max-turns inline close, an idle flush, or a wire
rotation between decide and discharge wins, and the discharge no-ops.
A park that is never discharged (the executor never ran) is overwritten
by the channel's next vote and is inert meanwhile, by the token and
staleness guards.  At most one park per channel, bounded by channels
the agent votes in.

Free functions over the agent (the ``run_salience_gate`` /
``handle_llm_call_exception_with_cost_close`` convention) so
``episode_routing`` stays under the 500-line review cap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

from ..memory.boundary_detectors import REASON_STRUCTURAL
from ..memory.scopes import is_group_scope
from ..persona_types import VOTE_CLOSE_TOKEN_KEY
from .interaction_boundary import matching_end_votes

if TYPE_CHECKING:
    from ..memory.interactions import Interaction
    from ..persona_types import AgentAction, AgentEvent
    from .episode_routing import _EpisodeRoutingMixin

logger = logging.getLogger(__name__)

__all__ = [
    "PendingVoteClose",
    "discharge_end_vote_publish",
    "park_end_vote_close",
]


class PendingVoteClose(NamedTuple):
    """One parked vote close: the scope the voter's record lives under,
    the open interaction's id at decide time (the staleness guard), the
    correlation token stamped onto the covered vote actions, and how
    many of them are still in flight (failures decrement; the first
    success — or the last failure — consumes the park)."""

    scope: str
    interaction_id: str
    token: str
    in_flight: int


def park_end_vote_close(
    agent: _EpisodeRoutingMixin,
    event: AgentEvent,
    *,
    scope: str,
    interaction: Interaction,
    actions: list[AgentAction],
) -> None:
    """Park the voter's local close for the executor's publish outcome
    (module doc).  Runs at decide time, under ``on_event``'s lock.

    Gates on the resolved scope KIND: only group scopes park.  This is
    the vote seam's twin of the wire seam's ``is_thread_scope`` guard —
    one basis for every exemption (DM, thread scope, ``thread:``-prefixed
    channels, the unknown-prefix fallback scope) instead of per-seam
    re-derivations from event fields and re-declared channel-id
    prefixes.  The executor's own gates (``end_vote_action.py``) stay
    authoritative for what actually publishes; an exempted scope's vote
    still publishes (or is dropped) there — it just never closes a
    local record here.
    """
    if not is_group_scope(scope):
        return
    votes = matching_end_votes(event, actions)
    if not votes:
        return
    token = uuid4().hex
    for action in votes:
        action.payload[VOTE_CLOSE_TOKEN_KEY] = token
    agent._pending_vote_closes[event.channel_id or ""] = PendingVoteClose(
        scope=scope,
        interaction_id=interaction.interaction_id,
        token=token,
        in_flight=len(votes),
    )


async def discharge_end_vote_publish(
    agent: _EpisodeRoutingMixin,
    channel_id: str,
    *,
    published: bool,
    token: str,
    synthesis_reply: bool = False,
) -> None:
    """Discharge the parked vote close for ``channel_id`` (module doc).

    Runs under the agent's lock: the park/discharge pair brackets the
    executor's publish, which runs *outside* ``on_event``'s lock, so a
    concurrent event for the same scope could otherwise race the close.
    Idempotent — a success pops the park first, so a duplicate callback
    is a no-op; an outcome whose token does not match the park (a vote
    this park never stamped: a threaded/exempted vote's ``""``, an
    earlier turn's stranded token under a newer park) leaves the park
    alone.

    ``synthesis_reply`` (PR #718 review, OQ #6; revised by the follow-up
    review): ``True`` when the published vote rode the wire claimable as
    the §D closing artifact — a chair answering the synthesis directive
    with a vote whose content IS the synthesis (the ISSUE-0099
    outcome-(a) shape; the executor threads it off ``end_vote_action``'s
    "published" result).  The echo says what the wire claim CARRIED,
    never what Go ACCEPTED — the commit is async, so no acceptance
    signal exists at discharge time — and the claim is refused whenever
    the arm is already gone: consumed by the timeout fire, or abandoned
    by a mid-arm disable / ``max_rounds`` raise, either of which demotes
    the same marked vote to an ordinary quorum vote.  In the raise case
    the interaction is deliberately left OPEN, so the presumptive close
    this discharge used to run buried the chair's live record and billed
    an OQ #6 lease against a discussion the operator just extended,
    accelerating the very cost close the raise deferred.  So a synthesis
    reply closes NOTHING here: the discharge pops the park and defers to
    the close-notification self-echo — the fan Go runs iff it actually
    closed on this reply, and BOTH closing shapes include the vote's
    sender: the armed bounded close by default, and the end-vote quorum
    a DEMOTED synthesis vote completes via its synthesis-echo carve-out
    (``end_vote.go`` keys ``excludeSender`` off the wire marker — the
    ordinary quorum fan excludes its voter precisely because that
    voter's discharge closed locally, which this deferral does not;
    PR #718 review).  The self-echo closes the chair's record with the
    truthful trigger and, on a bounded close, the metering mark
    (``close_notification.py``).  A lost self-echo degrades to the idle
    bury (late, unleased — the close fan's documented fire-and-forget
    residual), never to a wrong close.  The default keeps every
    ordinary vote discharge on the pre-4b-ii close path.
    """
    async with agent._lock:
        pending = agent._pending_vote_closes.get(channel_id)
        if pending is None or token != pending.token:
            return
        if not published:
            if pending.in_flight > 1:
                # A duplicate vote's failure consumes one in-flight slot;
                # a sibling publish can still confirm the close.
                agent._pending_vote_closes[channel_id] = pending._replace(
                    in_flight=pending.in_flight - 1,
                )
                return
            agent._pending_vote_closes.pop(channel_id, None)
            # The honest outcome of finding 5: no publish → no close. INFO,
            # not WARNING — the publish failure itself already logged at
            # warning in end_vote_action; this line records the memory-side
            # consequence (the record stays open for the ordinary closes).
            logger.info(
                "Agent %s: end-vote publish to %s did not complete; the "
                "local interaction record stays open (scope=%s)",
                agent.agent_id, channel_id, pending.scope,
            )
            return
        agent._pending_vote_closes.pop(channel_id, None)
        if synthesis_reply:
            # Docstring: the echo is not an acceptance signal, so the close
            # and its OQ #6 metering belong to the close-notification
            # self-echo, which arrives iff Go closed on this reply. Leave
            # the record open: if the arm was consumed or abandoned the
            # discussion is still live and Go demoted this vote to an
            # ordinary one — the record keeps ingesting its turns.
            logger.info(
                "Agent %s: synthesis-reply vote to %s published; the local "
                "close defers to the close-notification self-echo (scope=%s)",
                agent.agent_id, channel_id, pending.scope,
            )
            return
        open_record = agent._interaction_tracker.get(pending.scope)
        if (
            open_record is None
            or not open_record.is_open
            or open_record.interaction_id != pending.interaction_id
        ):
            # The scope moved on between decide and publish (max-turns
            # inline close, idle flush, wire rotation) — that close already
            # told the truth; do not close its successor.
            return
        wire_id = open_record.wire_interaction_id
        if wire_id and agent._vote_closed_wire_ids.get(pending.scope) == wire_id:
            # Re-vote on a wire interaction this voter already vote-closed
            # (the scope reopened on continued traffic under the SAME id —
            # no quorum formed).  Go deduped the vote but the suppressed
            # duplicate still committed (2xx → "published"); mirror the
            # dedup here so one channel interaction never fragments into
            # N "ended" local records.  The record stays open, like the
            # wire's, and closes on the eventual rotation or idle gap.
            return
        closed = agent._interaction_tracker.close(
            pending.scope, reason=REASON_STRUCTURAL,
        )
        if closed is not None:
            if wire_id:
                agent._vote_closed_wire_ids[pending.scope] = wire_id
            await agent._persist_closed_interaction(closed)
