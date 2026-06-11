"""Channel-side close triggers for the local interaction (RFC 0020 §B).

Extracted from :mod:`agents.persona_runtime.episode_routing` to keep that
module under the 500-line cap (``scripts/checks/file_size.py --strict``).
Houses the three predicates that decide whether a multi-turn event closes
the agent-local :class:`~agents.memory.interactions.InteractionTracker`
scope structurally — the structural-trigger half RFC 0020 deferred to
"the channel pipeline" and the RFC 0030 interaction-id producer made
real:

* :func:`is_session_end_event` — explicit ``chat_end`` / ``session_end``
  metadata flags (RFC 0016 / RFC 0011).
* :func:`ends_interaction_by_vote` — the turn's decided actions carry an
  ``END_INTERACTION_VOTE`` for the event's group channel (RFC 0030
  Layer 4): the persona judged its contribution complete, so its local
  record of the conversation closes at vote time.
* :func:`wire_rotation_closes` — the orchestrator-minted channel
  ``interaction_id`` on the inbound event differs from the one the open
  local interaction was opened under: the channel conversation ended
  (end-vote quorum or idle rotation) and the resolver minted fresh, so
  the local scope must split at the same boundary.

All three are pure functions over the event / actions / open
interaction; the caller (``episode_routing._handle_multi_turn_event``)
owns the actual :meth:`InteractionTracker.close` + persistence calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..memory.boundary_detectors import REASON_IDLE_GAP, REASON_STRUCTURAL
from ..persona_types import ActionType, EventType

if TYPE_CHECKING:
    from ..memory.boundary_detectors import CloseReason
    from ..memory.interactions import Interaction
    from ..persona_types import AgentAction, AgentEvent

# DM channels are identified by the ``dm:`` channel-id prefix — the same
# convention as ``channel_reply.py`` / ``end_vote_action.py``.  Re-declared
# rather than imported from ``agents.memory.scopes`` for the same reason
# those modules give: the prefix is private there, and importing a module
# for one literal couples layers that only share a wire convention.  The
# posture's other half is the lock-step drift guard — this pin is asserted
# equal to every sibling copy (and to Go's DM id builder) by
# ``test_cross_language_dm_prefix_drift.py``; a divergence here would let
# a DM vote the executor drops still close the voter's local record.
_DM_CHANNEL_PREFIX = "dm:"

# Metadata keys that signal an explicit session end on a multi-turn
# event.  Either spelling is accepted so RFC 0016 ("chat_end") and
# RFC 0011 ("session_end") emit a structural close without a second
# adapter layer.
SESSION_END_METADATA_KEYS: frozenset[str] = frozenset({
    "chat_end",
    "session_end",
})

# Strings accepted as ``True`` for session-end metadata flags.
# PR-216 review (High #3): a bare ``bool(meta.get(k))`` truthiness
# check would close on any non-empty string, including ``"false"`` /
# ``"0"`` / ``"no"`` — a footgun for any channel adapter that
# JSON-stringifies booleans (a common interop pattern).  Restrict the
# accepted truthy strings to a small canonical allowlist so
# ``metadata={"chat_end": "false"}`` does not close the interaction.
SESSION_END_TRUTHY_STRINGS: frozenset[str] = frozenset({
    "true", "1", "yes", "y", "on",
})


def is_session_end_event(event: AgentEvent) -> bool:
    """Strict-truthy check for session-end metadata flags.

    PR-216 review (High #3): ``bool("false")`` is ``True``, so the
    prior ``bool(meta.get(k))`` accepted any non-empty string — a
    channel adapter that stringifies booleans would have closed every
    multi-turn interaction unexpectedly.  Accept only:

    * the ``bool`` value ``True`` (and only ``True`` — not any truthy
      non-bool such as a list/object),
    * a non-zero numeric value,
    * a string whose lowercase form is in
      :data:`SESSION_END_TRUTHY_STRINGS`.

    Anything else (``False``, ``0``, ``None``, ``"false"``, ``"0"``,
    empty string, missing key) is treated as not-end.
    """
    meta = event.metadata or {}
    for key in SESSION_END_METADATA_KEYS:
        if key not in meta:
            continue
        val = meta[key]
        if val is True:
            return True
        if isinstance(val, str):
            if val.strip().lower() in SESSION_END_TRUTHY_STRINGS:
                return True
            continue
        # ``bool`` is a subclass of ``int``; ``True`` is handled above
        # and ``False`` falls through to the int branch as ``0``
        # (correctly evaluating not-end).
        if isinstance(val, (int, float)) and val != 0:
            return True
    return False


def ends_interaction_by_vote(
    event: AgentEvent, actions: list[AgentAction],
) -> bool:
    """True when this turn's decided actions vote to end the event's
    group discussion (RFC 0030 Layer 4 → RFC 0020 structural close).

    ``END_INTERACTION_VOTE`` is the channel sibling of RFC 0020 §B's
    explicit ``END_INTERACTION`` structural trigger: the persona just
    judged its contribution complete, so its *local* interaction for
    the conversation closes structurally at vote time — without this,
    the agent-side memory record only closes by idle gap long after
    the orchestrator's quorum close, and the interaction-summary
    surface (``agent interactions``) never shows the converged
    discussion as "ended" (the MT-CHANNEL-GOV-003 Step 3 gap).

    Intent-detection only (PR 607 review finding 5): this runs before
    the executor publishes the vote (the action loop stores the
    episode at step 6 and executes at the dispatcher), so the caller
    PARKS the close instead of executing it — the executor's
    publish-outcome callback (:mod:`.vote_close`) closes the voter's
    scope on success and drops the park on failure, so a vote that
    never reached the orchestrator leaves no early "ended" record.
    A quorum is never assumed (only the voter's own scope closes;
    non-voters close on the wire id rotation,
    :func:`wire_rotation_closes`).

    Mirrors the executor's gates (``end_vote_action.py``): a DM vote
    never closes anything, and a vote bound to a *different* channel
    is not about this conversation.  An unbound vote (no
    ``channel_id``) counts for the inbound channel ONLY on a
    ``CHANNEL_MESSAGE`` event — the exact condition under which
    ``bind_end_vote_channel`` stamps the inbound channel before
    publish.  On any other multi-turn event type (``MENTION``) an
    unbound vote reaches the executor channel-less and is dropped
    there (``status=no_channel_id``), so closing locally would record
    the conversation as ended on the strength of a vote that never
    happened (PR 607 review finding 4).

    A vote decided on a *threaded* turn (``event.thread_id`` set)
    closes nothing here (PR 607 review finding 2): the binding above
    stamps the PARENT channel, so the conversation the vote ends is
    the floor's — a different local scope from the thread scope the
    caller would close.  The voter's floor-scope record closes on the
    floor's wire-id rotation like any non-voter's, and the thread it
    was replying in keeps living.
    """
    if event.thread_id:
        return False
    channel_id = event.channel_id or ""
    if not channel_id or channel_id.startswith(_DM_CHANNEL_PREFIX):
        return False
    for action in actions:
        if action.action_type is not ActionType.END_INTERACTION_VOTE:
            continue
        target = str(action.payload.get("channel_id", "") or "").strip()
        if not target:
            if event.event_type is EventType.CHANNEL_MESSAGE:
                return True
            continue
        if target == channel_id:
            return True
    return False


def wire_rotation_closes(
    open_interaction: Interaction | None, wire_id: str,
) -> bool:
    """True when the inbound wire ``interaction_id`` proves the channel
    conversation the open local interaction belongs to has ended.

    The orchestrator's resolver (RFC 0030 interaction-id producer)
    stamps one shared ``interaction_id`` per channel conversation onto
    every publish, and rotates it when the conversation ends (end-vote
    quorum via the IP8 mark-closed hook, or idle rotation).  The id
    rides the fanout wire to ``event.metadata``
    (``seed_wire_metadata``).  The local tracker keys by *scope* (one
    open interaction per channel), so without this boundary check the
    vote-closed discussion and the channel's next topic merge into one
    local interaction that eventually closes as ``idle_gap``.  The
    rotation IS the channel-side structural close the tracker was
    waiting on; the caller closes the stale local interaction so the
    new turn opens a fresh one, mirroring the resolver's never-reopen
    rule.

    Both ids must be non-empty: untracked traffic (old orchestrator,
    non-channel events, *thread-scoped* events — episode routing never
    stamps a thread scope, see the wire-id guard in
    ``_handle_multi_turn_event`` — and over-length claims dropped at
    the seed point) keeps the v0.3.7 idle-only behaviour.  The
    resolver never reuses a retired id (IP2 overrides inbound claims;
    §C never-reopen), so a differing id is always a rotation, never a
    revert.

    Label fidelity (PR 607 review finding 3, discharged by producer
    plan OQ 5): the rotation's close *cause* now rides the wire — the
    orchestrator stamps the retired id and its trigger
    (``previous_interaction_id`` / ``previous_interaction_close_trigger``)
    onto every publish of the successor interaction, and the caller
    picks the close reason via :func:`wire_rotation_close_reason`
    instead of hardcoding ``REASON_STRUCTURAL``.  An old orchestrator
    (or a post-restart re-mint, which has no retiree to attribute)
    leaves the fields absent and the pre-OQ5 structural label stands.
    """
    return (
        open_interaction is not None
        and open_interaction.is_open
        and bool(wire_id)
        and bool(open_interaction.wire_interaction_id)
        and open_interaction.wire_interaction_id != wire_id
    )


# The resolver's idle-rotation trigger value, as stamped on
# ``previous_interaction_close_trigger`` (producer plan OQ 5).  Re-declared
# rather than imported from ``agents.channel_wire_metadata`` for the same
# layering reason as ``_DM_CHANNEL_PREFIX`` above — the literal is a wire
# convention, not shared code — and asserted equal to Go's ``idleTrigger``
# (``internal/channels/interaction_resolver.go``) and the seed-point
# allowlist by the cross-language drift test.  ``end_votes`` needs no twin
# here: it maps to the same ``REASON_STRUCTURAL`` the fallback already
# yields, so only ``idle`` changes the outcome.
_WIRE_CLOSE_TRIGGER_IDLE = "idle"


def wire_rotation_close_reason(
    open_interaction: Interaction | None,
    event_metadata: dict[str, object],
) -> CloseReason:
    """The close reason for a local interaction being closed by the wire
    id rotation :func:`wire_rotation_closes` detected (producer plan OQ 5).

    The seed point (``agents/channel_wire_metadata.py``) delivers the
    retired id + trigger as a validated pair on the event metadata.  The
    trigger is applied only when the retired id equals the wire id the
    open local record was opened under — a mismatch means this agent
    missed a generation (the channel rotated more than once since it
    last heard anything), so the stamped cause attributes a *different*
    boundary than the one being closed and is discarded.

    Mapping: ``idle`` → :data:`REASON_IDLE_GAP` (the channel's window
    elapsed; "went idle" is the truthful label even when the agent's own
    longer window had not), ``end_votes`` — and any absent, mismatched,
    or unrecognised value — → :data:`REASON_STRUCTURAL`.  The quorum
    close IS the explicit end the structural label claims, and the
    fallback keeps the exact pre-OQ5 behaviour for an old orchestrator
    or a post-restart re-mint (the mixed-version contract).  A neutral
    "unknown" reason was considered for the fallback and rejected: the
    wire cannot distinguish "old producer" from "new producer with no
    retiree", so a neutral label would relabel every legacy deployment's
    rotations while still mislabelling nothing-in-particular — see the
    producer plan OQ 5 notes.
    """
    if open_interaction is None:
        return REASON_STRUCTURAL
    prev_id = str(event_metadata.get("previous_interaction_id", "") or "")
    prev_trigger = str(
        event_metadata.get("previous_interaction_close_trigger", "") or "",
    )
    if (
        prev_id
        and prev_id == open_interaction.wire_interaction_id
        and prev_trigger == _WIRE_CLOSE_TRIGGER_IDLE
    ):
        return REASON_IDLE_GAP
    return REASON_STRUCTURAL


__all__ = [
    "SESSION_END_METADATA_KEYS",
    "SESSION_END_TRUTHY_STRINGS",
    "ends_interaction_by_vote",
    "is_session_end_event",
    "wire_rotation_close_reason",
    "wire_rotation_closes",
]
