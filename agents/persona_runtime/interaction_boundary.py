"""Channel-side close triggers for the local interaction (RFC 0020 §B).

Extracted from :mod:`agents.persona_runtime.episode_routing` to keep that
module under the 500-line cap (``scripts/checks/file_size.py --strict``).
Houses the predicates that decide whether a multi-turn event closes the
agent-local :class:`~agents.memory.interactions.InteractionTracker`
scope structurally — the structural-trigger half RFC 0020 deferred to
"the channel pipeline" and the RFC 0030 interaction-id producer made
real:

* :func:`is_session_end_event` — explicit ``chat_end`` / ``session_end``
  metadata flags (RFC 0016 / RFC 0011).
* :func:`matching_end_votes` — the turn's decided actions that carry an
  ``END_INTERACTION_VOTE`` for the event's channel (RFC 0030 Layer 4):
  the persona judged its contribution complete, so the vote-close park
  (:mod:`.vote_close`) covers exactly these actions.
* :func:`wire_rotation_closes` — the orchestrator-minted channel
  ``interaction_id`` on the inbound event differs from the one the open
  local interaction was opened under: the channel conversation ended
  (end-vote quorum or idle rotation) and the resolver minted fresh, so
  the local scope must split at the same boundary.

All are pure functions over the event / actions / open interaction; the
callers (``episode_routing._handle_multi_turn_event`` and
``vote_close.park_end_vote_close``) own the actual
:meth:`InteractionTracker.close` + persistence calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..channel_wire_metadata import WIRE_CLOSE_TRIGGER_IDLE
from ..memory.boundary_detectors import REASON_IDLE_GAP, REASON_STRUCTURAL
from ..persona_types import ActionType

if TYPE_CHECKING:
    from ..memory.boundary_detectors import CloseReason
    from ..memory.interactions import Interaction
    from ..persona_types import AgentAction, AgentEvent

# Metadata keys that signal an explicit session end on a multi-turn
# event.  Either spelling is accepted so RFC 0016 ("chat_end") and
# RFC 0011 ("session_end") emit a structural close without a second
# adapter layer.
_SESSION_END_METADATA_KEYS: frozenset[str] = frozenset({
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
_SESSION_END_TRUTHY_STRINGS: frozenset[str] = frozenset({
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
      :data:`_SESSION_END_TRUTHY_STRINGS`.

    Anything else (``False``, ``0``, ``None``, ``"false"``, ``"0"``,
    empty string, missing key) is treated as not-end.
    """
    meta = event.metadata or {}
    for key in _SESSION_END_METADATA_KEYS:
        if key not in meta:
            continue
        val = meta[key]
        if val is True:
            return True
        if isinstance(val, str):
            if val.strip().lower() in _SESSION_END_TRUTHY_STRINGS:
                return True
            continue
        # ``bool`` is a subclass of ``int``; ``True`` is handled above
        # and ``False`` falls through to the int branch as ``0``
        # (correctly evaluating not-end).
        if isinstance(val, (int, float)) and val != 0:
            return True
    return False


def matching_end_votes(
    event: AgentEvent, actions: list[AgentAction],
) -> list[AgentAction]:
    """The turn's decided ``END_INTERACTION_VOTE`` actions bound to the
    event's channel (RFC 0030 Layer 4 → RFC 0020 structural close).

    ``END_INTERACTION_VOTE`` is the channel sibling of RFC 0020 §B's
    explicit ``END_INTERACTION`` structural trigger: the persona just
    judged its contribution complete, so its *local* interaction for
    the conversation closes structurally once the vote actually lands
    on the wire — without this, the agent-side memory record only
    closes by idle gap long after the orchestrator's quorum close, and
    the interaction-summary surface (``agent interactions``) never
    shows the converged discussion as "ended" (the MT-CHANNEL-GOV-003
    Step 3 gap).  Intent-detection only (PR 607 review finding 5): the
    caller (:func:`.vote_close.park_end_vote_close`) PARKS the close
    for the executor's publish-outcome callback instead of executing
    it, and gates on the resolved scope kind (only group scopes have a
    vote-closeable conversation — DM and thread scopes never park).

    Ordering contract (PR 607 second-pass review): this runs strictly
    AFTER ``bind_end_vote_channel`` (action-loop step 4b, inside
    ``synthesize_channel_reply``) has stamped the inbound channel onto
    unbound votes, so a plain equality test against the bound
    ``channel_id`` is the single source of truth for which conversation
    a vote ends — the binding rule itself has exactly one home
    (``channel_reply.py``).  An action still unbound here (the bind
    seam only fires on CHANNEL_MESSAGE events with a channel) matches
    nothing: the executor drops it as ``no_channel_id`` and a local
    close would record a vote that never happened.
    """
    channel_id = event.channel_id or ""
    if not channel_id:
        return []
    return [
        action
        for action in actions
        if action.action_type is ActionType.END_INTERACTION_VOTE
        and str(action.payload.get("channel_id", "") or "").strip() == channel_id
    ]


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
    the seed point) keeps the v0.3.7 idle-only behaviour.

    Late-delivery defence (PR 607 second-pass review): the resolver
    never *reuses* a retired id (IP2 overrides inbound claims; §C
    never-reopen), but Go's fanout gives no cross-publish per-recipient
    ordering — one detached goroutine per publish, a fresh dial per
    dispatch, no sequence numbers — so a straggler message of the
    RETIRED interaction can arrive after the successor's first message.
    A differing id is therefore a rotation only when it is not the open
    record's known predecessor (``predecessor_wire_id``, stamped from
    the OQ 5 pair the successor's every message carries); the
    straggler's turn appends to the successor's record instead of
    fragmenting it into close/reopen/re-close phantom records.  A
    record whose opening turn carried no pair (old orchestrator,
    post-restart re-mint) has no predecessor to compare and keeps the
    pre-defence behaviour; a straggler older than one generation is
    indistinguishable from a missed-generation rotation and closes —
    the accepted residual, requiring two in-flight rotations at once.

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
        and open_interaction.predecessor_wire_id != wire_id
    )


def wire_rotation_close_reason(
    open_interaction: Interaction,
    event_metadata: dict[str, object],
) -> CloseReason:
    """The close reason for a local interaction being closed by the wire
    id rotation :func:`wire_rotation_closes` detected (producer plan OQ 5).

    The caller passes the same (non-``None``) open interaction the
    predicate just confirmed — the two functions are a guarded pair
    over one value.

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
    prev_id = str(event_metadata.get("previous_interaction_id", "") or "")
    prev_trigger = str(
        event_metadata.get("previous_interaction_close_trigger", "") or "",
    )
    if (
        prev_id
        and prev_id == open_interaction.wire_interaction_id
        and prev_trigger == WIRE_CLOSE_TRIGGER_IDLE
    ):
        return REASON_IDLE_GAP
    return REASON_STRUCTURAL


__all__ = [
    "is_session_end_event",
    "matching_end_votes",
    "wire_rotation_close_reason",
    "wire_rotation_closes",
]
