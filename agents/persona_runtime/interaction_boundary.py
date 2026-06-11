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

from ..persona_types import ActionType

if TYPE_CHECKING:
    from ..memory.interactions import Interaction
    from ..persona_types import AgentAction, AgentEvent

# DM channels are identified by the ``dm:`` channel-id prefix — the same
# convention as ``channel_reply.py`` / ``end_vote_action.py``.  Re-declared
# rather than imported from ``agents.memory.scopes`` for the same reason
# those modules give: the prefix is private there, and importing a module
# for one literal couples layers that only share a wire convention.
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

    Intent-based by design: this runs before the executor publishes
    the vote (the action loop stores the episode at step 6 and
    executes at the dispatcher), so a publish failure leaves a
    slightly-early local close — the persona's judgement was real
    either way, and a quorum is never assumed (only the voter's own
    scope closes; non-voters close on the wire id rotation,
    :func:`wire_rotation_closes`).

    Mirrors the executor's gates (``end_vote_action.py``): a DM vote
    never closes anything, and a vote bound to a *different* channel
    is not about this conversation.  An unbound vote (no
    ``channel_id``) counts for the inbound channel — the same binding
    ``bind_end_vote_channel`` applies before publish.
    """
    channel_id = event.channel_id or ""
    if not channel_id or channel_id.startswith(_DM_CHANNEL_PREFIX):
        return False
    for action in actions:
        if action.action_type is not ActionType.END_INTERACTION_VOTE:
            continue
        target = str(action.payload.get("channel_id", "") or "").strip()
        if not target or target == channel_id:
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
    non-channel events, over-length claims dropped at the seed point)
    keeps the v0.3.7 idle-only behaviour.  The resolver never reuses a
    retired id (IP2 overrides inbound claims; §C never-reopen), so a
    differing id is always a rotation, never a revert.
    """
    return (
        open_interaction is not None
        and open_interaction.is_open
        and bool(wire_id)
        and bool(open_interaction.wire_interaction_id)
        and open_interaction.wire_interaction_id != wire_id
    )


__all__ = [
    "SESSION_END_METADATA_KEYS",
    "SESSION_END_TRUTHY_STRINGS",
    "ends_interaction_by_vote",
    "is_session_end_event",
    "wire_rotation_closes",
]
