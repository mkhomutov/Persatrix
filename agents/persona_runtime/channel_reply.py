"""Channel-reply synthesis for plain-text persona responses (ISSUE-0048).

The persona runtime parses the LLM's free-text reply via
:func:`_ActionLoopMixin._parse_actions`. When the model emits the
documented JSON action schema (e.g. ``[{"action_type":
"send_channel_message", ...}]``), the parser yields an explicit
:class:`ActionType.SEND_CHANNEL_MESSAGE` action and the executor
publishes via the orchestrator REST surface. When the model instead
responds conversationally — which is the default for a persona that has
not been prompt-trained on the action schema — the parser folds the
text into a single :class:`ActionType.COMPLETE_TASK` action carrying the
reply in ``payload["result"]``.

Without this seam, the executor records ``status=completed`` for the
``COMPLETE_TASK`` and never reaches the channel-publish branch. The
orchestrator-side ``replyWaiter`` therefore never observes a publish
from the agent on the inbound DM channel, and the chat-as-DM REST
round-trip 504s on its ``chatDefaultTimeout``.

This helper closes that gap by promoting the conversational reply into
an explicit :class:`ActionType.SEND_CHANNEL_MESSAGE` bound to the
inbound channel. It is called once per turn, immediately after
``_parse_actions``, and is a no-op for any of:

* Non-channel events (TICK / TASK_ASSIGNED / SUB_AGENT_COMPLETED / …).
* CHANNEL_MESSAGE events with an empty ``channel_id`` — the deprecated
  ``SendChatMessage`` path (cleanup tracked in ISSUE-0035).
* Action lists that already contain a ``SEND_CHANNEL_MESSAGE`` for the
  inbound channel — a well-prompted agent must not be double-published.
* COMPLETE_TASK actions whose ``result`` is empty or whitespace-only,
  **on group channels** — silence is a valid turn outcome under the
  ``reply-discretion`` safety snippet; stamping a blank publish over a
  deliberate silence would defeat the affordance.

On **DM channels** the empty-reply case is not a no-op: the response
gate (``response_gate.py``) forces ``always`` on DMs and the
orchestrator's ``replyWaiter`` 504s if no publish arrives, so this
helper synthesises a minimal ellipsis placeholder to close the
round-trip. The ``reply-discretion`` snippet tells the persona this
directly; the fallback exists for the case where the LLM ignores the
guidance.

The synthesised action mentions the inbound ``sender_id`` so the
priority-1 chat-reply extraction in :mod:`agents.chat_reply` (legacy
``SendChatMessage`` path) picks the reply ahead of its priority-3
``COMPLETE_TASK.result`` fallback. Mentions are otherwise inert on DM
channels because DM membership is the routing primitive (see
:func:`agents.response_gate.evaluate_response_gate`).
"""

from __future__ import annotations

from ..persona_types import ActionType, AgentAction, AgentEvent, EventType

__all__ = [
    "bind_end_vote_channel",
    "fold_prose_into_end_vote",
    "synthesize_channel_reply",
]


# DM channels enforce a "must reply" invariant (RFC 0011 §D,
# ``response_gate.py`` ``dm`` branch): a DM with no reply 504s the
# chat-as-DM REST round-trip on ``chatDefaultTimeout``. The
# ``reply-discretion`` prompt snippet tells the persona this directly,
# but when the LLM still produces no usable reply text — typically the
# model failed to follow the prompt — this fallback closes the loop
# with a minimal placeholder rather than leaving the user staring at a
# spinner. An ellipsis is unambiguous in audit logs and naturalistic
# as a "I have nothing to add" signal; the root cause (model not
# following the discretion guidance) is the right place to fix it, not
# this backstop.
_DM_EMPTY_REPLY_FALLBACK: str = "…"

# DM channels are identified by the ``dm:`` channel-id prefix — same
# convention used by ``response_gate.py`` (see ``_DM_CHANNEL_PREFIX``).
# Keeping the prefix literal here rather than importing avoids a
# circular dependency between this module and the gate.
_DM_CHANNEL_PREFIX: str = "dm:"

# Group channels carry the ``group:`` prefix (RFC 0020 §D scope vocabulary,
# mirrored on the channel id). Re-declared locally for the same reason
# ``_DM_CHANNEL_PREFIX`` is — ``fold_prose_into_end_vote`` gates on it so the
# fold fires only where the vote both publishes (the executor drops DM votes,
# see ``end_vote_action.py``) and participates in the RFC 0030 Layer 4 quorum.
_GROUP_CHANNEL_PREFIX: str = "group:"


def synthesize_channel_reply(
    event: AgentEvent,
    actions: list[AgentAction],
    agent_id: str,
) -> list[AgentAction]:
    """Promote a conversational COMPLETE_TASK reply into a channel publish.

    Pure function: the input ``actions`` list is not mutated. When
    synthesis fires the returned list contains a fresh
    ``SEND_CHANNEL_MESSAGE`` prepended in front of the original
    actions; otherwise the original list is returned unchanged.

    ``agent_id`` is accepted for parity with the action-loop call site
    (and for future audit logging keyed on the agent), but is not
    consumed by the current implementation — the executor stamps
    ``sender_id = agent_id`` on the wire publish via
    :meth:`agents.action_executor.ActionExecutor._handle_send_channel_message`,
    so the synthesised action does not carry it.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE:
        return actions

    channel_id = event.channel_id or ""
    if not channel_id:
        return actions

    # RFC 0030 producer plan PR 2 (IP6): reconcile any END_INTERACTION_VOTE
    # with the inbound channel before the promotion logic below — the two
    # concerns share this seam because both bind parsed actions to the
    # channel the turn arrived on.
    actions = bind_end_vote_channel(event, actions)

    # ISSUE-0097 defect 2: fold the turn's free-text into a group-channel vote
    # so it travels as ONE publish. Runs AFTER the bind (so the vote carries
    # its channel and same-channel prose can be matched) and BEFORE the
    # promotion below (so the consumed prose is never promoted into a separate
    # SEND_CHANNEL_MESSAGE).
    actions = fold_prose_into_end_vote(event, actions)

    for action in actions:
        if action.action_type is not ActionType.SEND_CHANNEL_MESSAGE:
            continue
        if action.payload.get("channel_id") == channel_id:
            return actions

    reply_text = ""
    for action in actions:
        if action.action_type is not ActionType.COMPLETE_TASK:
            continue
        candidate = action.payload.get("result", "")
        if isinstance(candidate, str) and candidate.strip():
            reply_text = candidate
            break

    if not reply_text:
        # Group channels: silence is a valid turn outcome — see the
        # ``reply-discretion`` safety snippet. Preserve the no-op path
        # so a persona that deliberately declined does not get its
        # silence stamped over with a publish.
        #
        # DM channels: silence is broken by construction (the
        # response gate forces ``always`` on DMs, and the orchestrator's
        # ``replyWaiter`` 504s if no publish arrives). Synthesize a
        # minimal placeholder so the round-trip closes cleanly.
        if not channel_id.startswith(_DM_CHANNEL_PREFIX):
            return actions
        reply_text = _DM_EMPTY_REPLY_FALLBACK

    mentions = [event.sender_id] if event.sender_id else []
    synthesized = AgentAction(
        action_type=ActionType.SEND_CHANNEL_MESSAGE,
        payload={
            "channel_id": channel_id,
            "content": reply_text,
            "mentions": mentions,
        },
    )
    return [synthesized, *actions]


def bind_end_vote_channel(
    event: AgentEvent,
    actions: list[AgentAction],
) -> list[AgentAction]:
    """Bind a channel-less ``END_INTERACTION_VOTE`` to the inbound channel.

    The RFC 0030 Layer 4 vote (producer plan PR 2, IP6) is a real channel
    publish, so the executor needs a ``channel_id`` — but a persona votes on
    *the conversation it is in*, and requiring it to echo routing details
    back through the action payload invites transcription mistakes (a typo'd
    channel casts the vote into the wrong room, or nowhere). This seam
    stamps the inbound channel onto any vote that omits ``channel_id``,
    exactly the :func:`synthesize_channel_reply` posture for conversational
    replies. An explicit payload value is preserved; non-channel events
    (e.g. a TICK-emitted vote) are left untouched — the executor's
    ``no_channel_id`` status is the backstop there.

    Pure: returns a new list when a binding fires; actions themselves are
    re-created, never mutated.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE or not event.channel_id:
        return actions

    bound: list[AgentAction] = []
    changed = False
    for action in actions:
        if (
            action.action_type is ActionType.END_INTERACTION_VOTE
            and not str(action.payload.get("channel_id", "") or "").strip()
        ):
            bound.append(AgentAction(
                action_type=ActionType.END_INTERACTION_VOTE,
                payload={**action.payload, "channel_id": event.channel_id},
            ))
            changed = True
        else:
            bound.append(action)
    return bound if changed else actions


def fold_prose_into_end_vote(
    event: AgentEvent,
    actions: list[AgentAction],
) -> list[AgentAction]:
    """Fold a turn's free-text into its END_INTERACTION_VOTE ``content``
    (ISSUE-0097 defect 2) so the vote travels as a single channel publish.

    A single LLM turn that emits an agreement/closing block *plus* a vote is
    parsed into two actions — the prose (an explicit ``SEND_CHANNEL_MESSAGE``
    for the channel, or a ``COMPLETE_TASK`` the ISSUE-0048 synthesis would
    promote into its own publish) and the vote. The executor publishes each
    separately, and the RFC 0030 Layer 4 quorum counts published messages as
    turns (``end_vote.go``: ``state.turn++`` per publish, window check
    ``state.turn - voteTurn < w``). The extra prose turn pushes a concurring
    vote one position further from the chair's vote, dropping it out of
    ``end_vote_window`` — the structural cause the PR-2 prompt steer could not
    reach. Folding the prose INTO the vote ``content`` and dropping the prose
    action makes the vote one publish, the single-message shape the chair path
    already produces.

    Gated to **group** channels: that is the only place the vote both
    publishes (``end_vote_action.py`` drops DM votes and never reaches the
    quorum) and counts toward a quorum. On a DM the prose must keep its own
    publish — folding it into a vote that is then dropped would lose the reply
    and 504 the DM-must-reply round-trip (ISSUE-0048). Thread floors do not run
    the end-vote arc, so they are left to the ordinary promotion path too.

    Only the prose ``content`` is folded; an explicit ``SEND_CHANNEL_MESSAGE``'s
    structured ``mentions`` are intentionally NOT carried onto the vote. A vote
    addresses the room's process, not a member — ``end_vote_action.py`` publishes
    every vote with empty mentions, and a turn that has voted to close must not
    draw a mentioned member back in (the folded vote already fans out to all
    members as the terminal signal). The persona's literal "@name" survives
    inside the folded text; only the routing-level mention is dropped, which is
    the vote invariant holding rather than a loss.

    Pure: returns a new list only when a fold fires; actions are re-created,
    never mutated. A no-op when there is no group-channel vote, when the vote
    has no sibling free-text (the clean chair path), or off CHANNEL_MESSAGE
    events.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE or not event.channel_id:
        return actions

    vote_idx = next(
        (
            i for i, a in enumerate(actions)
            if a.action_type is ActionType.END_INTERACTION_VOTE
            and str(a.payload.get("channel_id", "") or "").strip().startswith(
                _GROUP_CHANNEL_PREFIX,
            )
        ),
        None,
    )
    if vote_idx is None:
        return actions
    vote = actions[vote_idx]
    vote_channel = str(vote.payload.get("channel_id", "") or "").strip()

    # Collect the turn's free-text siblings: a SEND_CHANNEL_MESSAGE aimed at
    # the vote's own channel (an explicit publish), and any COMPLETE_TASK
    # result (the parser folds conversational prose — and the chair-stall
    # rescue's surrounding prose — into one). A cross-posted SEND to another
    # channel is not this vote's free-text and is left untouched.
    prose_parts: list[str] = []
    consumed: set[int] = set()
    for i, action in enumerate(actions):
        if i == vote_idx:
            continue
        if (
            action.action_type is ActionType.SEND_CHANNEL_MESSAGE
            and str(action.payload.get("channel_id", "") or "").strip() == vote_channel
        ):
            text = action.payload.get("content", "")
        elif action.action_type is ActionType.COMPLETE_TASK:
            text = action.payload.get("result", "")
        else:
            continue
        if isinstance(text, str) and text.strip():
            prose_parts.append(text.strip())
            consumed.add(i)

    if not prose_parts:
        return actions

    # Join the prose siblings ahead of any content the vote already carried
    # (prose leads, the vote's own statement trails), de-duplicating exact
    # repeats: an LLM that emits the same sentence in BOTH its free-text block
    # and the vote ``content`` would otherwise publish it twice inside one
    # message. Exact (stripped) match only — every part is already stripped, so
    # this drops a verbatim duplicate and leaves distinct-but-similar text
    # untouched, preserving first-occurrence order.
    existing = str(vote.payload.get("content", "") or "").strip()
    seen: set[str] = set()
    ordered: list[str] = []
    for part in [*prose_parts, *([existing] if existing else [])]:
        if part not in seen:
            seen.add(part)
            ordered.append(part)
    folded = "\n\n".join(ordered)

    folded_actions: list[AgentAction] = []
    for i, action in enumerate(actions):
        if i in consumed:
            continue
        if i == vote_idx:
            folded_actions.append(AgentAction(
                action_type=ActionType.END_INTERACTION_VOTE,
                payload={**action.payload, "content": folded},
            ))
        else:
            folded_actions.append(action)
    return folded_actions
