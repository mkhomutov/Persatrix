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
open-scope check, not before it in the caller: ``_store_event_episode``
opens a fresh interaction when none is open, so an unconditional
ingest-then-close would fabricate a 1-turn "ended" record (plus a
summariser LLM call) for a scope that already closed — exactly the
record the no-open-interaction no-op contract promises never to
invent. Sequence:

1. strict marker / event-type / scope checks — an impostor or
   unroutable event touches nothing, not even the staleness pass;
2. the same idle flush every ingest runs (the agent's own boundary
   rules outrank the late signal — see the conservative-choice note
   below);
3. no interaction left open → done: the close stands recorded
   orchestrator-side; nothing here invents a record to mirror it. The
   notification is deliberately NOT ingested in this case — there is
   no open window to land the final turn in, and ingesting would
   either fabricate the record above or leave a 1-turn successor
   dangling toward its own "went idle" burial;
4. otherwise ingest (the closing vote lands as the record's final
   turn) and close with :data:`REASON_STRUCTURAL` — guarded by
   identity: if the ingest itself closed or rotated the interaction
   (max-turns cap, wire-id rotation), that close's own cause stands
   and no second close is layered on a different interaction than the
   one the notification found open.

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

from ..memory.boundary_detectors import REASON_STRUCTURAL

if TYPE_CHECKING:
    from ..memory.interactions import Interaction, InteractionTracker
    from ..persona_types import AgentAction, AgentEvent, EventType

    class _CloseNotificationAgent(Protocol):
        """The composed-agent surface
        :func:`close_interaction_on_notification` needs."""

        _interaction_tracker: InteractionTracker
        _MULTI_TURN_EVENT_TYPES: frozenset[EventType]

        def _scope_for_multi_turn_event(self, event: AgentEvent) -> str | None: ...
        async def _persist_closed_interaction(
            self, interaction: Interaction,
        ) -> None: ...
        async def _store_event_episode(
            self, event: AgentEvent, actions: list[AgentAction],
        ) -> None: ...


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
    for expired in agent._interaction_tracker.idle_check():
        try:
            await agent._persist_closed_interaction(expired)
        except Exception:
            logger.warning(
                "Failed to flush idle interaction before close "
                "notification (scope=%s, interaction_id=%s)",
                expired.scope, expired.interaction_id,
                exc_info=True,
            )
    open_interaction = agent._interaction_tracker.get(scope)
    if open_interaction is None:
        # Already idled out (or never tracked): the close stands
        # recorded orchestrator-side; invent nothing locally.
        return
    # The closing vote lands as the closed record's final turn — ingest
    # BEFORE close (closing first would strand the message in a
    # successor interaction).
    await agent._store_event_episode(event, [])
    if agent._interaction_tracker.get(scope) is not open_interaction:
        # The ingest itself closed or replaced the interaction (the
        # max-turns inline close, a wire-id rotation): that close's own
        # cause stands; never layer a structural close on a different
        # interaction than the one the notification found open.
        return
    closed = agent._interaction_tracker.close(scope, reason=REASON_STRUCTURAL)
    if closed is not None:
        await agent._persist_closed_interaction(closed)
