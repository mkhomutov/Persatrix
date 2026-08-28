"""End-vote close notification → RFC 0020 close path (CP3, agent half).

The orchestrator's `end_votes` close suppresses the closing vote's
fanout, so the close must be *delivered*: the close-notification
dispatch (the end-vote-close-propagation amendment, CP1/CP2) re-sends
the closing message to every dispatch-served member with the
``interaction_close_notification`` marker. Receiver-side the marked
event is control, never stimulus — the response gate refuses it
pre-LLM (reason ``close_notification``) and the action loop's suppress
path calls :func:`close_interaction_on_notification` so the channel
scope's open interaction closes NOW with the established ``end_votes``
mapping — :data:`~agents.memory.boundary_detectors.REASON_STRUCTURAL`,
the "ended" render (:mod:`.interaction_boundary`: the quorum close IS
the explicit end the structural label claims) — instead of burying the
converged discussion as "went idle" an idle window later.

This dispatch owns the WHOLE receiver arc — including the final-turn
ingest (PR #614 review finding 3). The ingest must come after the
open-records check, not before it in the caller: an unconditional
ingest-then-close would fabricate a 1-turn "ended" record (plus a
summariser LLM call) for a scope that already closed — exactly the
record the no-open-interaction no-op contract promises never to
invent. Since the v0.3.15 ``(principal, speaker, scope)`` re-key
(ISSUE-0123 part 3) the notified scope holds one record per speaker
per tenant, and the notification is a ROOM event, so the arc FANS:
the closing message lands as the final turn of EVERY record it may
close, and every one of them closes with the truthful cause. Sequence:

1. strict marker / event-type / scope checks — an impostor or
   unroutable event touches nothing, not even the staleness pass;
2. the same idle flush every ingest runs (the agent's own boundary
   rules outrank the late signal — see the conservative-choice note
   below);
3. no records left open → done: the close stands recorded
   orchestrator-side; nothing here invents a record to mirror it. The
   notification is deliberately NOT ingested in this case — there is
   no open window to land the final turn in, and ingesting would
   either fabricate the record above or leave a 1-turn successor
   dangling toward its own "went idle" burial;
4. otherwise ingest (the closing message lands as the final turn of
   each record the wire-id conjunct admits — a direct per-record
   append through the shared :mod:`.turn_payload` builder, never the
   per-event path, which would route the turn to the closing sender's
   key alone or fabricate a record where the sender has none) and
   close each — with :data:`REASON_STRUCTURAL`, or the truthful
   :data:`REASON_COST` when the notification carries the RFC 0052
   bounded close's ``cost`` trigger (PR 4b-ii; the same typed field's
   presence marks each closed record for the OQ #6 metered summary).
   SKIP the ingest for a marked RE-delivery (PR 4b-ii — the floor-path
   bounded close's closing message already reached every member live
   inside its round; re-ingesting it duplicated the final turn) and for
   a self-echo (the RFC 0052 bounded close fans to the round-
   triggering sender, so the convener/chair receives its own message;
   ingesting it would write a ``sender == agent_id`` turn and inflate
   ``turn_count``, the self-echo the gate keeps out of memory), closing
   the records without it so the sender still authors its summaries.
   The direct append cannot close, rotate, or replace a record, so the
   old post-ingest identity re-check retired with the per-event ingest
   path that needed it; the wire-id conjunct (step 4's admission rule)
   is what keeps a pre-rotation straggler from closing a successor
   record.

Conservative choice, deliberately: an interaction whose idle window
expired BEFORE the notification landed closes by the idle rule (step
2), not as "ended" — relabelling it would put a structural cause on a
window the agent's own boundary contract already ended, and the
orchestrator's authoritative "ended" record exists regardless. The
amendment fixes the *timely* path; a notification a full idle window
late has, by the agent's own rules, missed the conversation.

Extracted as a free function taking the composed persona agent — the
:mod:`.cost_close` / :mod:`.vote_close` sibling, same extraction idiom,
so ``action_loop.py`` stays a one-liner at the call site and under the
500-line review cap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from ..channel_wire_metadata import (
    WIRE_BOUNDED_CLOSE_TRIGGERS,
    WIRE_CLOSE_TRIGGER_COST,
    wire_interaction_id,
)
from ..memory.boundary_detectors import REASON_COST, REASON_STRUCTURAL
from ..memory.scopes import is_thread_scope
from .close_path import persist_fanned_closes
from .interaction_boundary import wire_admits_record
from .turn_payload import build_turn_payload

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker
    from ..persona_types import AgentEvent, EventType

    class _CloseNotificationAgent(Protocol):
        """The composed-agent surface
        :func:`close_interaction_on_notification` needs."""

        agent_id: str
        _interaction_tracker: InteractionTracker
        _MULTI_TURN_EVENT_TYPES: frozenset[EventType]

        def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None: ...
        async def _persist_closed_interaction(
            self, interaction: Interaction,
        ) -> bool | None: ...


logger = logging.getLogger(__name__)

__all__ = ["close_interaction_on_notification"]


async def close_interaction_on_notification(
    agent: _CloseNotificationAgent, event: AgentEvent,
) -> None:
    """Ingest the final turn and close the open interaction in the
    notified scope — or no-op entirely when nothing is open.

    Called from the action loop's gate-suppress path only for the
    dedicated ``close_notification`` refusal; owns the ingest too (see
    the module docstring for the ordering rationale). Defence-in-depth
    re-checks the marker here regardless (CP3's strict-bool rule): a
    truthy non-bool on the cleartext port must not fabricate a close —
    burying an active discussion is exactly the failure mode the
    strictness blocks — so an impostor is a no-op even if a future
    caller wires this off a looser signal (a payload-less event reads
    as unmarked for the same reason). Scope resolution rides the same
    multi-turn routing as :func:`.cost_close.close_interaction_on_cost`'s
    channel branch: a notification is channel traffic by construction
    (the orchestrator re-dispatches a channel publish), so a
    non-multi-turn event type has no scope to close and returns quietly.
    """
    payload = event.payload or {}
    marked = payload.get("interaction_close_notification") is True
    if not marked:
        return
    if event.event_type not in agent._MULTI_TURN_EVENT_TYPES:
        return
    scope = agent._scope_for_multi_turn_event(event)
    if scope is None:
        return
    # The staleness pass every ingest runs (episode_routing's flush
    # loop, same warn contract): without it, ``get`` below would report
    # an idle-EXPIRED interaction as open, the ingest's own internal
    # flush would then close it as idle mid-arc, and the close at the
    # bottom would pop the fresh 1-turn successor — the fabrication
    # this function exists to prevent, one branch over.
    # Grouped per scope like the ingest-path flush (PR #846 re-review):
    # one conversation going quiet is one ``conversation_lead``, and each
    # persist is guarded with the failing record's identity.
    expired_by_scope: dict[str, list[Interaction]] = {}
    for expired in agent._interaction_tracker.idle_check():
        expired_by_scope.setdefault(expired.scope, []).append(expired)
    for expired_records in expired_by_scope.values():
        await persist_fanned_closes(
            expired_records, agent._persist_closed_interaction,
        )
    records = agent._interaction_tracker.records_for_scope(scope)
    if not records:
        # Already idled out (or never tracked): the close stands
        # recorded orchestrator-side; invent nothing locally.
        return
    # Wire-id conjunct (PR #718 review) — per RECORD since the
    # ``(principal, speaker, scope)`` re-key (v0.3.15 residuals PR 3):
    # the notification's metadata bag is cloned verbatim off the closing
    # message, so it carries the CLOSED interaction's id; this handler is
    # otherwise scope-keyed and would apply the close — and, below, the
    # OQ #6 metering mark — to WHATEVER records are open in the scope. A
    # rotation that landed BEFORE this notification (Go's fan is
    # fire-and-forget with no cross-publish per-recipient ordering, so a
    # successor interaction's first publish can overtake it) leaves
    # successor records here under a DIFFERENT wire id — closing one
    # would mislabel a live discussion's record, and the metered summary
    # would bill the successor's id against a reserve carved for the
    # predecessor. A record whose known id disagrees is the no-open case
    # one reorder later and is skipped; a blank on either side (an
    # unstamped fresh record, an old producer) keeps the scope-keyed
    # behaviour — the tolerant-wire-reader posture. Mixed scopes close
    # the matching records and leave the successors to their own
    # boundaries.
    notified_wire_id = wire_interaction_id(event)
    to_close: list[Interaction] = []
    for record in records:
        if record.replayed:
            # PR #846 review: a replay-opened record belongs to the
            # catch-up pass — its close is the pass-end
            # ``REASON_CATCHUP_COMPLETE`` sweep, and a live closing turn
            # must never land inside a flagged span (derivation is skipped
            # for it wholesale, so the turn would be silently discarded
            # and the close mislabelled).  Leave it to its own sweep.
            continue
        if not wire_admits_record(record, notified_wire_id):
            logger.info(
                "Agent %s: close notification for interaction %s found %s "
                "open on scope %s; stale straggler, leaving that record",
                agent.agent_id, notified_wire_id,
                record.wire_interaction_id, scope,
            )
            continue
        to_close.append(record)
    if not to_close:
        return
    if notified_wire_id and not is_thread_scope(scope):
        # PR #846 review: restore the retired ingest path's wire-id
        # backfill (the ``episode_routing`` stamp the direct append no
        # longer routes through) — a blank-stamped record the tolerant
        # conjunct admitted is being closed AS the notified conversation,
        # so stamp it: the metered summary leases against this id
        # (``summarize_close`` skips the lease on a blank id) and the
        # persisted episode keeps the governance cross-reference.  Thread
        # scopes are wire-UNTRACKED (PR 607 review finding 1) — a threaded
        # notification carries the parent FLOOR's id, which must not become
        # the thread episode's governance cross-reference (re-review).
        for record in to_close:
            if not record.wire_interaction_id:
                record.wire_interaction_id = notified_wire_id
    # The closing message lands as the FINAL TURN OF EACH record to
    # close, then every one of them closes with the truthful cause —
    # the room fan (ISSUE-0123 part 3): since the ``(principal,
    # speaker, scope)`` re-key a room holds N records, and a close
    # notification is a room event.  The ingest is a DIRECT per-record
    # append (``append_turn`` + the shared :func:`build_turn_payload`),
    # not a ``_store_event_episode`` pass: the per-event path would
    # deliver the turn to the closing sender's key alone — or fabricate
    # a fresh record where the sender has none, exactly the record the
    # no-open contract above refuses to invent.  Ingest before close,
    # as ever (closing first would strand the message in a successor
    # interaction); the direct append cannot rotate or replace a
    # record, so the old post-ingest identity re-check is gone with the
    # path that needed it.  The max-turns cap is deliberately not
    # enforced on this one append — the record closes in the same step,
    # and the notification's truthful trigger outranks the cap label
    # (``append_turn``'s contract).
    #
    # EXCEPT a SELF-echo (RFC 0052 bounded-close fix): the bounded close fans
    # the notification to the round-triggering sender too
    # ([ChannelRouter.boundedClose] passes ``excludeSender=false``), so the
    # convener/chair receives its OWN message back — with a fresh wire id, so
    # the conversation-window dedup does not catch it. Ingesting it would append
    # a turn whose ``payload.sender == agent_id`` and inflate ``turn_count`` —
    # the exact self-echo the gate's ``POLICY_DEFENSE_IN_DEPTH`` refusal keeps
    # out of episodic memory (:mod:`.gate_suppress`), and which the end-vote
    # close never hit because it excludes the sender. The sender's own final
    # words already ride its record as the action envelope of the turn it
    # replied on, so the record needs no echo turn; it needs only to CLOSE so
    # the sender authors its RFC 0020 summary. So skip the ingest for a
    # self-echo and fall straight through to the structural close, matching the
    # end-vote voter's own no-self-ingest close. An inbound closing message (any
    # other sender — every end-vote recipient, and every non-triggering member
    # on a bounded close) ingests as before.
    #
    # ... and skip it for a marked RE-delivery (PR 4b-ii, resolving the
    # 4b-i round-5 KNOWN LIMIT): on a FLOOR-CONTROLLED bounded close the
    # bounding stimulus already reached every member live inside its floor
    # round (unlike the end-vote close, whose vote's own fanout is
    # suppressed, and the concurrent-path bounded close, which withholds
    # the bounding dispatch — both sole-delivery), so ingesting it again
    # appended a duplicate final turn and inflated ``turn_count`` by one
    # per non-sender member per close. "Re-delivery, close only" vs "sole
    # delivery, ingest then close" is not decidable locally (the fresh
    # wire id defeats any id-based check by design), so the producer says
    # which it is via the typed ``close_notification_redelivery`` field —
    # strict ``is True``, because a spoofed truthy non-bool suppressing
    # the ingest would LOSE a sole-delivered closing turn.
    # RFC 0052 PR 4b-ii: the truthful bounded-close cause. The wire lift
    # (``channel_wire_metadata.channel_event_payload``) allowlists the field
    # to the two causes the bounded close stamps, and this consumer
    # re-checks (defence-in-depth, the strict-marker posture): ``cost``
    # closes with the truthful :data:`REASON_COST` — the wallet soft-budget
    # close IS a cost close, not the "ended" the structural label claims —
    # while ``structural`` (max_rounds) and every unmarked/unrecognised
    # notification keep the established :data:`REASON_STRUCTURAL` mapping.
    # Resolved BEFORE the ingest so the OQ #6 metering mark can ride the
    # record whichever path closes it (see below).
    trigger = payload.get("close_notification_close_trigger")
    bounded = trigger in WIRE_BOUNDED_CLOSE_TRIGGERS
    reason = REASON_COST if trigger == WIRE_CLOSE_TRIGGER_COST else REASON_STRUCTURAL
    if bounded:
        # OQ #6 (PR 4b-ii; PR #718 review): a bounded close is autonomous by
        # construction (the trigger field is stamped by nothing else), so its
        # per-persona RFC 0020 summary must draw a lease against the mandatory
        # cap the PR 4a reserve was carved from — ``summarize_close.py``
        # threads that lease off ``meter_close_summary``.  Marked on every
        # record the fan will close: each ``(principal, speaker)`` record
        # authors its own summary, and each of those draws its own lease
        # (the reserve multiplier residuals PR 4 re-sizes).  Interim
        # consequence, stated (PR #846 review): on the COST trigger the
        # residual hard-cap headroom is at most the old ``1 + N`` reserve
        # by construction, so a multi-speaker room's ~N×S leases can
        # over-commit it and late summaries degrade to the unavailable
        # placeholder until the PR 4 re-size lands.  The
        # pre-ingest/post-close double-mark the per-event ingest needed is
        # gone with it — the direct append below cannot close a record, so
        # one mark on the live records covers the only path left.
        for record in to_close:
            record.meter_close_summary = True
    redelivery = payload.get("close_notification_redelivery") is True
    # Co-gate the redelivery ingest-skip on ``bounded`` (PR #718 review): the
    # skip is safe ONLY for a recognized bounded close, whose closing message
    # genuinely already reached every member live inside the floor round. A
    # ``redelivery`` marker on a notification that is NOT a bounded close — an
    # ``idle``/``end_votes``/garbage trigger from a non-Go (or compromised)
    # producer, so ``bounded`` is False — is a SOLE delivery, and skipping its
    # ingest would LOSE the closing turn. The Go orchestrator only ever stamps
    # ``redelivery`` alongside a bounded trigger, so this is producer-hardening
    # in the same tolerant-wire-reader posture as the trigger allowlist above,
    # never a change to the live path.
    # One room event, one instant (v0.3.15 PR 3 review fix): read the
    # tracker's clock seam ONCE and hand every appended final turn and
    # every ``closed_at`` below the same timestamp — per-call reads gave
    # the one closing message N different ``Turn.at`` values across the
    # sibling records.
    now = agent._interaction_tracker.now()
    if event.sender_id != agent.agent_id and not (redelivery and bounded):
        # The same envelope shape the per-event path builds — one
        # builder, two consumers (:mod:`.turn_payload`).  ``[]`` actions:
        # the notification is control, never stimulus, so no action loop
        # ran for it.
        turn = build_turn_payload(
            event, f"Event: {event.event_type.value} → Actions: []",
        )
        for record in to_close:
            agent._interaction_tracker.append_turn(record, turn, now=now)
    # Close every admitted record first, then persist each behind its
    # own guard (``persist_fanned_closes``, the same review fix): the
    # closes pop the records from the open map, so one persist failure
    # must not discard the siblings.
    closed_records: list[Interaction] = []
    for record in to_close:
        closed = agent._interaction_tracker.close_record(
            record, reason=reason, now=now,
        )
        if closed is not None:
            closed_records.append(closed)
    await persist_fanned_closes(
        closed_records, agent._persist_closed_interaction,
    )
