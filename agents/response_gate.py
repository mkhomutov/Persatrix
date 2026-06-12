"""RFC 0011 PR 4b — channels response gate.

The gate decides whether the persona-runtime LLM should be invoked for
an inbound :class:`AgentEvent` whose :class:`EventType` is
``CHANNEL_MESSAGE``. It is the canonical enforcement point for the
per-membership ``respond_policy`` declared in
``schemas/channel.schema.json``.

The decision is made **pre-LLM, pre-memory-recall** in
:meth:`agents.persona_runtime._ActionLoopMixin._on_event_inner` — but
**after** the LLM-client / model-config fail-fast checks at the top of
that method. The ordering is intentional (PR #252 review Q-1): a
misconfigured agent surfaces the misconfig to its caller as a
``COMPLETE_TASK("…")`` result, instead of silently swallowing channel
chatter and leaving an operator to wonder for hours why the agent has
gone quiet. A suppressed (gated) message still costs zero LLM tokens
and zero retrieval round-trips — the gate runs before
``_inject_memory_context``. Memory **ingestion** still runs in PR 5 —
the gate's contract is "do not respond", not "do not remember".

Policies (RFC 0011 §D table):

* ``when_mentioned`` — fire the LLM if the agent's id is in
  ``event.payload["mentions"]`` OR the message is a thread reply to a
  message this agent authored (``thread_parent_sender_id == agent_id``).
* ``always`` (a.k.a. the ``participant`` disposition) — fire the LLM
  except when the agent is the sender (the orchestrator's
  :class:`ChannelRouter` already filters the sender on fanout, but the
  receiver re-checks for defence in depth on the cleartext gRPC port) or
  when the message is **directed elsewhere** (RFC 0030 relevance
  amendment Tier A, v0.3.7): a message naming specific other members via
  ``mentions`` — and not an explicit ``@everyone`` broadcast — does not
  draw a reply from a ``participant`` who is not among them. An open-floor
  message (empty ``mentions``) or a broadcast still admits every
  ``participant``. Since the floor-capable-directedness amendment
  (v0.3.8), the suppression basis is the orchestrator-resolved
  ``floor_mentions`` subset when ``floor_mentions_resolved`` is true: a
  message whose mentions name only parties that cannot take the floor
  (the human operator, an ``observer``, a non-member, the sender itself)
  is open floor, not directed.
* ``never`` — always suppress. The orchestrator filters
  ``RespondNever`` members upstream of dispatch, so this branch should
  not normally fire; if it does, it surfaces a policy-routing
  regression and the gate suppresses to fail-closed.

DM channels are documented in the RFC 0011 §D table as
``always``-gated regardless of the per-membership knob (a DM with no
reply is broken by definition). The gate enforces this by treating
``channel_id`` starting with ``dm:`` as ``always``.

For non-CHANNEL_MESSAGE events the gate returns ``True`` unconditionally
— it has no opinion on TICK / TASK_ASSIGNED / etc.

Defense-in-depth ordering is preserved (RFC 0011 PR plan §PR 4 Key
implementation details): gate (primary for per-recipient policy) →
existing ``EventDispatcher.max_cascade_depth=5`` (defense-in-depth for
cross-agent cascade) → REST-side rate limit.

The cascade-depth check fires *before* the gate in
:meth:`EventDispatcher.dispatch`, so the gate never sees an event past
the depth ceiling. The Python check is now the **defense-in-depth
backstop** for the legacy in-process mention cascade and any wire-side
regression: the **primary** cross-agent cascade-depth enforcement lives
in the Go orchestrator's fanout cap (RFC 0011 amendment "Cascade-depth
wire propagation", PR 2 of the v0.3.0 channel test-findings plan — the
orchestrator sits on the trust boundary that agents cannot be relied on
to honour). The backstop is verified by
``tests/unit/python/test_response_gate_cascade_backstop.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .persona_types import AgentEvent, EventType

# The policy-string vocabulary (legacy triple, RFC 0030 disposition
# aliases, the synthetic metric labels, the @everyone sentinel) lives in
# :mod:`agents.response_policies` — reference data whose commentary scales
# with the disposition vocabulary, split out so this module stays under
# the code-line cap (the file_size_allowlist "data vs logic" posture).
# Re-exported here unchanged: every existing import path keeps working.
from .response_policies import (  # noqa: F401  (re-exports)
    _DISPOSITION_ALIASES,
    _DM_CHANNEL_PREFIX,
    MENTION_EVERYONE,
    POLICY_ADDRESSED,
    POLICY_ALWAYS,
    POLICY_CHAIR,
    POLICY_DEFENSE_IN_DEPTH,
    POLICY_LOW_SALIENCE,
    POLICY_NEVER,
    POLICY_OBSERVER,
    POLICY_PARTICIPANT,
    POLICY_UNKNOWN,
    POLICY_WHEN_MENTIONED,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MENTION_EVERYONE",
    "POLICY_ADDRESSED",
    "POLICY_ALWAYS",
    "POLICY_CHAIR",
    "POLICY_DEFENSE_IN_DEPTH",
    "POLICY_LOW_SALIENCE",
    "POLICY_NEVER",
    "POLICY_OBSERVER",
    "POLICY_PARTICIPANT",
    "POLICY_UNKNOWN",
    "POLICY_WHEN_MENTIONED",
    "GateDecision",
    "evaluate_response_gate",
    "is_open_floor_admit",
]


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Outcome of :func:`evaluate_response_gate`.

    Attributes:
        respond: ``True`` when the persona runtime should proceed with
            memory recall + LLM invocation for this event; ``False`` when
            the gate suppresses the response.
        policy: The effective policy for the decision. The gate only ever
            assigns a value from a **bounded** set — the canonical legacy
            triple (:data:`POLICY_WHEN_MENTIONED` / :data:`POLICY_ALWAYS` /
            :data:`POLICY_NEVER`), a synthetic routing-artifact label
            (:data:`POLICY_DEFENSE_IN_DEPTH`, :data:`POLICY_UNKNOWN`), or the
            empty string ``""`` on the two non-enforcing pass-through branches
            (a non-CHANNEL_MESSAGE event or the legacy empty ``channel_id``,
            both ``respond=True``); never a raw/unbounded wire string. On a
            *suppressing* decision this value is the ``policy`` label on the
            ``channel.messages.gated`` metric, so operators can break
            suppression counts down by intent without a cardinality blow-up;
            the ``""`` sentinel never reaches that counter because it only
            fires when ``respond=False``. (The Tier-B salience suppression
            rides the same counter with a :data:`POLICY_LOW_SALIENCE` label,
            but that label is applied by the downstream salience stage in
            :mod:`agents.observability._metrics_salience` — no
            :class:`GateDecision` the gate returns ever carries it.)
        reason: Short, low-cardinality string explaining the branch.
            Suitable for log fields and span attributes; never a free-form
            error string.
    """

    respond: bool
    policy: str
    reason: str


def is_open_floor_admit(decision: GateDecision) -> bool:
    """Return ``True`` iff ``decision`` is the *open-floor* admit — the one
    branch RFC 0030 Tier B refines.

    Tier B (the leased salience bid, :mod:`agents.salience_bid`) runs
    **only** on the ambiguous open-floor remainder Tier A leaves: a
    ``participant`` (``always``) member admitted with
    ``reason="policy_always"`` to a message that addresses nobody who could
    take the floor — empty ``mentions``, or (since the v0.3.8
    floor-capable-directedness amendment) mentions whose orchestrator-resolved
    ``floor_mentions`` subset is empty. Every *directed*
    admit — a ``@``-mention (``reason="mentioned"``, whether the member's
    disposition is ``when_mentioned`` *or* ``always`` and it was named
    individually), an explicit ``@everyone`` broadcast (``"broadcast"`` — a
    room-wide address whose D3 sentinel contract is "do not suppress"), a DM
    (``"dm"``), a thread-reply-to-self (``"thread_reply_to_self"``) — is
    already the persona's lane and skips the bid (TB1). A suppressed
    decision, an ``observer``, and the self-sender never reach the bid
    either (the gate returned ``respond=False`` for them).

    This is the seam that keeps Tier A pure: the gate decides *eligibility*
    with no LLM/IO; this predicate lets the action-loop caller decide
    whether to layer the leased bid on top.
    """
    return (
        decision.respond
        and decision.policy == POLICY_ALWAYS
        and decision.reason == "policy_always"
    )


def evaluate_response_gate(event: AgentEvent, *, agent_id: str) -> GateDecision:
    """Decide whether the persona runtime should respond to ``event``.

    The function is **pure** — it consumes the event payload and the
    agent id, returns a :class:`GateDecision`, and does not mutate either
    input. The caller (``_on_event_inner``) emits metrics and logs based
    on the decision.

    Non-CHANNEL_MESSAGE events return ``respond=True`` unconditionally
    so callers can apply the gate uniformly without an event-type
    pre-check at every site.
    """
    if event.event_type is not EventType.CHANNEL_MESSAGE:
        return GateDecision(respond=True, policy="", reason="not_channel_message")

    # The legacy ``AgentService.SendChatMessage`` RPC builds a
    # CHANNEL_MESSAGE event without a ``channel_id`` (it predates the
    # chat-as-DM unification and is deferred for cleanup in
    # ``docs/issues/ISSUE-0035``). Until that issue lands, the gate
    # bypasses CHANNEL_MESSAGE events with an empty channel_id so the
    # legacy path keeps working — those events do not flow through the
    # channels subsystem and have no per-membership policy to enforce.
    channel_id = event.channel_id or ""
    if not channel_id:
        return GateDecision(respond=True, policy="", reason="no_channel_id")

    payload = event.payload or {}
    raw_policy = payload.get("respond_policy", "")
    policy = raw_policy if isinstance(raw_policy, str) else ""
    # Defence-in-depth: collapse a disposition value to its legacy alias so
    # the branches below (and the metric ``policy`` label) read the
    # canonical legacy value. Normally a no-op — the Go loader already
    # normalized the wire value.
    policy = _DISPOSITION_ALIASES.get(policy, policy)

    # DM channels override the per-membership policy: a DM with no reply
    # is broken by construction (RFC 0011 §D). The orchestrator-side
    # ``GetOrCreateDM`` always inserts both members with
    # ``RespondAlways``, so this override is consistent with the wire
    # value — but enforcing it explicitly here makes the gate robust to
    # an operator who hand-edits a DM membership row.
    if channel_id.startswith(_DM_CHANNEL_PREFIX):
        if event.sender_id == agent_id:
            # See POLICY_DEFENSE_IN_DEPTH for the labeling rationale.
            return GateDecision(
                respond=False,
                policy=POLICY_DEFENSE_IN_DEPTH,
                reason="dm_self_sender",
            )
        return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="dm")

    # Sender-side filter (defence in depth). The router already drops
    # the sender on fanout; the gate re-checks because the cleartext
    # gRPC port cannot be trusted to carry a non-spoofed ``sender_id``.
    # The configured wire policy is intentionally **not** used as the
    # metric label here — see POLICY_DEFENSE_IN_DEPTH.
    if event.sender_id == agent_id:
        return GateDecision(
            respond=False,
            policy=POLICY_DEFENSE_IN_DEPTH,
            reason="self_sender",
        )

    if policy == POLICY_NEVER:
        # Fail-closed. The orchestrator filters ``RespondNever`` members
        # upstream of dispatch, so a ``never`` reaching the gate is a
        # policy-routing regression — log at warn so operators see the
        # drift surface in their logs even though the gate already
        # suppressed the response.
        logger.warning(
            "Agent %s: respond_policy=never reached the gate (channel=%s); "
            "orchestrator should have filtered upstream",
            agent_id, channel_id,
        )
        return GateDecision(respond=False, policy=POLICY_NEVER, reason="policy_never")

    # End-vote-close-propagation amendment (CP3): the orchestrator's close
    # NOTIFICATION — the closing quorum vote re-dispatched after an
    # `end_votes` close so the local tracker can close with the truthful
    # cause. Control, never stimulus: refused for EVERY policy, before any
    # admitting lane (a notification may well @-mention the room — it is
    # still not an invitation to speak), so no turn, no Tier B bid, no LLM
    # call ever runs on it; the action loop's ingest-on-suppress keeps the
    # closing message in the window, and its suppress path runs the
    # tracker-close dispatch off this dedicated reason. Strict `is True` —
    # the `floor_mentions_resolved` posture: a spoofed truthy non-bool on
    # the cleartext port must not fabricate a close signal (it falls
    # through to the ordinary policy branches below).
    if payload.get("interaction_close_notification") is True:
        return GateDecision(
            respond=False, policy=policy, reason="close_notification",
        )

    # Chair-stall-escalation amendment (§C item 2): the orchestrator's forced
    # turn after a stalled floor round admits down the directed lane for
    # either canonical non-`never` policy (CE2 allows an `addressed` chair,
    # whose unmarked gate would suppress an unmentioned stimulus). The
    # dedicated reason keeps it out of :func:`is_open_floor_admit` — which is
    # what skips the Tier B bid (TB1: re-running it would re-produce the very
    # silence being escalated). Defence-in-depth mirrors
    # `floor_mentions_resolved`: strict `is True`, the fail-closed branches
    # above (DM self-sender / self-sender / `never`) already won, and an
    # unknown wire policy still falls through to the fail-closed
    # `unknown_policy` suppress (bounded-label discipline preserved).
    if payload.get("chair_escalation") is True and policy in (
        POLICY_ALWAYS, POLICY_WHEN_MENTIONED,
    ):
        return GateDecision(respond=True, policy=policy, reason="chair_escalation")

    if policy == POLICY_ALWAYS:
        # RFC 0030 relevance amendment Tier A (v0.3.7): the directed-elsewhere
        # filter. A ``participant`` (``always``) member no longer answers a
        # message addressed to *other* members — that was the v0.3.6 pile-on
        # defect ("how about you @ember-owl?" drew a reply from everyone).
        # Suppress iff the message names recipients the floor could actually
        # pass to (the suppression *basis* below is non-empty — raw
        # ``mentions`` pre-amendment, the resolved floor-capable subset since
        # v0.3.8), this agent is not among the raw mentions, and it is not an
        # explicit broadcast (``MENTION_EVERYONE`` absent, decision D3). A
        # participant named *individually* is addressed directly and admits
        # with the ``mentioned`` reason (its lane — RFC 0030 Tier B TB1 keeps
        # it out of the salience bid); an explicit ``@everyone`` broadcast
        # admits with the directed ``broadcast`` reason (also TB1 — the
        # sentinel's "do not suppress" contract is its lane). Only a message
        # that addresses nobody who could take the floor — empty ``mentions``,
        # or (v0.3.8) mentions resolved to an empty floor-capable subset —
        # admits with the open-floor ``policy_always``, the ambiguous
        # remainder Tier B (the v0.3.8 salience bid that decides who actually
        # has something to add) refines. The decision keeps ``policy=always``
        # in every branch: a gated-counter fire with ``policy=always`` is, by
        # construction,
        # exactly a directed-elsewhere suppression (a self-sender ``always`` is
        # labelled ``defense_in_depth``), so the RFC 0011 §D
        # ``{channel_id, policy}`` label set surfaces it without a new
        # ``reason`` dimension.
        mentions = payload.get("mentions") or []
        if isinstance(mentions, list) and mentions:
            if agent_id in mentions:
                # RFC 0030 Tier B TB1: an ``always`` member named explicitly
                # is being *addressed directly* — its lane, exactly like an
                # ``addressed`` (when_mentioned) member that was named. Admit
                # it with the directed ``mentioned`` reason, **not** the
                # open-floor ``policy_always``: ``is_open_floor_admit`` keys
                # on ``policy_always``, so collapsing this case into it would
                # let the salience bid run on (and possibly silence) a
                # directly-asked persona, breaking the explicit-address
                # contract. A broadcast that *also* names this member counts
                # as directed too — an explicit name is stronger than the
                # room-wide sentinel.
                return GateDecision(
                    respond=True, policy=POLICY_ALWAYS, reason="mentioned",
                )
            if MENTION_EVERYONE in mentions:
                # RFC 0030 Tier B: an explicit ``@everyone`` broadcast is an
                # explicit *room-wide* address, and the sentinel's documented
                # v0.3.7 contract (decision D3) is precisely "do not suppress
                # — every participant reaches the turn". That makes it the
                # personas' lane just like an individual @-mention (TB1): it
                # must **not** be routed through the bias-to-silence salience
                # bid, which would re-suppress the very broadcast the sentinel
                # exists to force through. Admit with a distinct directed
                # ``broadcast`` reason so ``is_open_floor_admit`` (which keys
                # on ``policy_always``) leaves it out of Tier B. ``respond``
                # stays True, so every participant still reaches the quality
                # turn exactly as in v0.3.7 — only the (dormant) bid is
                # skipped. The metric ``policy`` label stays ``always``.
                return GateDecision(
                    respond=True, policy=POLICY_ALWAYS, reason="broadcast",
                )
            # Floor-capable-directedness amendment (v0.3.8, §C item 3): the
            # suppression basis is the orchestrator-resolved *floor-capable*
            # subset (`floor_mentions` — members whose normalized policy is
            # not `never`, excluding the sender) when, and only when, the
            # producer declared it resolved. The switch keys on the
            # `floor_mentions_resolved` flag — never on the list's own
            # presence or emptiness, which the wire cannot express (proto3
            # repeated fields have no presence): a flag-true *empty* subset
            # is the motivating case itself (a sole mention of the human
            # operator) and reclassifies to the open-floor admit below,
            # where the Tier B bid still applies (the amendment moves the
            # message between two existing lanes; no third lane). Flag
            # false/absent (an old orchestrator, the legacy in-process
            # path) — and a malformed non-list or absent list under a true
            # flag — fall back to the raw-mentions basis: today's behaviour,
            # degrading
            # toward *over*-suppression, never under-suppression. The
            # `mentioned`/`broadcast` admits above stay on raw `mentions`
            # (amendment OQ 3). `is True` is deliberate: the gRPC servicer
            # lifts a real bool, and a spoofed truthy non-bool on the
            # cleartext port must not widen admission.
            basis = mentions
            if payload.get("floor_mentions_resolved") is True:
                floor_mentions = payload.get("floor_mentions")
                if isinstance(floor_mentions, list):
                    basis = floor_mentions
            if basis:
                return GateDecision(
                    respond=False,
                    policy=POLICY_ALWAYS,
                    reason="directed_elsewhere",
                )
        # Open floor — the genuinely ambiguous remainder that the v0.3.8
        # salience bid refines: empty ``mentions``, or (v0.3.8) mentions that
        # resolved to no floor-capable addressee. Both carry the
        # ``policy_always`` reason so :func:`is_open_floor_admit` routes them
        # into Tier B identically.
        return GateDecision(respond=True, policy=POLICY_ALWAYS, reason="policy_always")

    if policy == POLICY_WHEN_MENTIONED:
        mentions = payload.get("mentions") or []
        if isinstance(mentions, list) and agent_id in mentions:
            return GateDecision(
                respond=True, policy=POLICY_WHEN_MENTIONED, reason="mentioned",
            )
        # Thread-reply-to-self trigger (RFC 0011 §D table). Activates
        # when the agent authored the parent message of this thread,
        # even if the reply does not explicitly mention the agent. The
        # parent sender id is pre-resolved by the router so the gate
        # need not look the parent up itself.
        thread_id = event.thread_id
        thread_parent_sender_id = payload.get("thread_parent_sender_id", "")
        if (
            thread_id
            and thread_parent_sender_id == agent_id
        ):
            return GateDecision(
                respond=True,
                policy=POLICY_WHEN_MENTIONED,
                reason="thread_reply_to_self",
            )
        return GateDecision(
            respond=False,
            policy=POLICY_WHEN_MENTIONED,
            reason="not_mentioned",
        )

    # Unknown / empty policy — fail-closed with a warn. The wire-side
    # validator already rejects unknown values, so this branch should
    # not fire in production; it is a belt-and-braces guard for tests
    # or future additive policies that have not been wired through the
    # gate yet.
    logger.warning(
        "Agent %s: unknown respond_policy %r on channel %s; suppressing",
        agent_id, raw_policy, channel_id,
    )
    # Bounded metric label, not the raw ``policy`` string — see POLICY_UNKNOWN.
    return GateDecision(respond=False, policy=POLICY_UNKNOWN, reason="unknown_policy")
