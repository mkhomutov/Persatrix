"""The originating dispatch's ambient context, threaded through the executor.

Extracted verbatim from :mod:`agents.channel_wire_metadata` (ISSUE-0118)
when the per-request epoch/session threading pushed that module past the
500-line review cap (``scripts/checks/file_size.py``); that module
re-exports :class:`DispatchContext` and :func:`wire_interaction_id` so
every existing import keeps working.  The two are a cohesive concern —
``for_event`` derives its origin pair through the drift-pinned reader —
so they move together.

ISSUE-0118 (v0.3.13 PR 1) adds the epoch/session axes to the context:
the ``on_event`` handler binds the per-request scopes task-locally
(``request_scope_from_metadata``), but everything the
:class:`~agents.action_executor.ActionExecutor` runs AFTER the handler
returns — the end-vote publish-outcome discharge and its interaction
persistence, the legacy in-process cascade's child dispatches — executes
on the dispatching task, where those ContextVars never reach
(``asyncio`` task-locals do not cross the queued ``EventLoop`` hop).
RFC 0037 PR 7 (#788) hit the same seam for acting classification and
threaded it structurally (``event.metadata`` → ``for_event`` → the
``origin_tripwire_watch`` field); epoch and session now ride the same
rail, re-entered around action processing via :meth:`DispatchContext.
request_scopes` so the executor-side memory seams resolve the request's
``(epoch, session)`` instead of their construction snapshots.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from .confidentiality_tripwire import tripwire_watch_from_event
from .epoch_id import epoch_id_from_metadata, epoch_scope
from .principal_id import principal_id_from_metadata, principal_scope
from .session_id import session_id_from_metadata, session_scope

if TYPE_CHECKING:
    from .confidentiality_tripwire import TripwireWatch
    from .persona_types import AgentEvent

__all__ = [
    "DispatchContext",
    "wire_interaction_id",
]


def wire_interaction_id(event: AgentEvent) -> str:
    """The interaction id ``event`` was dispatched under, read tolerantly off
    the metadata key the wire-ingress lifts seed (``seed_wire_metadata`` /
    ``seed_replay_metadata`` in :mod:`agents.channel_wire_metadata`) — the ONE
    shared reader behind the RFC 0052 no-reopen claim's origin threading
    (PR #716 review: the executor entry points each re-implemented this read
    inline, OUTSIDE the cross-language drift pin, so a coordinated rename of
    the pinned sites would have left them silently returning ``""`` — no
    claim echoed, the latch blind, and a post-close straggler minting fresh
    and reopening the closed discussion).  Absent and non-string values read
    as ``""``, the untracked posture.

    Also the body behind ``wallet_cause.lease_interaction_id_for_event`` and
    the rotation-boundary wire-id read (``episode_routing``), which delegate
    here rather than keeping byte-identical copies (PR #716 review): a
    semantics change applied to one copy and not the others would have wallet
    spend billed under an id the no-reopen latch and the soft-budget close
    trigger never see. The import direction is persona_runtime → here — the
    executor entry points must not grow a hard dep on the persona subpackage.
    """
    value = event.metadata.get("interaction_id", "")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class DispatchContext:
    """The originating dispatch's ambient context, threaded WHOLE through the
    executor entry points: ``cascade_depth`` plus the RFC 0052 no-reopen
    claim's origin pair (the inputs :meth:`same_channel_claim` builds the wire
    claim from).

    One required parameter instead of three parallel defaulted kwargs
    (PR #716 review): with per-value threading, "unstamped" was the silent
    fallback for any entry point or action arm that forgot one kwarg — a
    claim-less publish the no-reopen latch cannot see, post-close stragglers
    minting fresh and reopening the closed discussion, with only
    site-enumerating pin tests as protection. A required ``context``
    parameter makes the choice visible at every call site: event-driven
    ingress builds via :meth:`for_event` (the origin pair cannot be
    half-threaded), and an event-less caller constructs the origin-less
    posture explicitly.

    ``cascade_depth`` keeps the terminate-at-clamp default the executor's
    kwarg carried: a caller with no inbound event to derive depth from (the
    tick scheduler) publishes at the cap so the orchestrator's per-hop clamp
    drops fan-out — the v0.3.0 demo runaway was the consequence of a
    publish-at-depth-0 default. Chain-origin callers (chat surface, the
    dispatcher's first hop) state their depth explicitly.

    ``origin_synthesis_turn`` (PR #718 review) records whether the
    originating dispatch WAS the RFC 0052 §D synthesis directive (the
    ``synthesis_turn`` payload marker, strict ``is True`` like the gate and
    framing reads). :meth:`same_channel_claim` stamps it onto the publish as
    the ``synthesis_reply`` marker — the fanout-head claim's third conjunct
    (``claimSynthesisReply``, internal/channels/synthesis_claim.go): the
    interaction id spans every round and every reply echoes it, so without
    this echo an ordinary chair reply from an earlier round still in flight
    at the bound is indistinguishable from the synthesis and would be fanned
    to every member as the closing artifact. Both the derivation (here) and
    the stamping (the method below) are structural, for the same reason as
    the origin pair: a per-call-site kwarg would make "unmarked" the silent
    fallback of any site that forgot it — and an unmarked synthesis reply is
    silently withheld orchestrator-side, the close degrading to the timeout
    net on every close of that path. The only per-site choice left is the
    method's NAMED opt-out, for the one publish that must never be claimable
    as the artifact (the error-reply apology).

    ``origin_epoch_id`` / ``origin_session_id`` (ISSUE-0118, v0.3.13 PR 1):
    the per-request run-isolation epoch and room-continuity session the
    dispatch was delivered under, lifted structurally by :meth:`for_event`
    through the SAME leaf readers the ``on_event`` scope binders consume
    (:func:`agents.epoch_id.epoch_id_from_metadata` /
    :func:`agents.session_id.session_id_from_metadata`) — one validation
    seam per axis, so the handler-side binding and this executor-side lift
    cannot drift on the key name or rule.  ``""`` (an event-less context, a
    tick, a pre-rail producer) means :meth:`request_scopes` re-enters
    nothing and executor-side resolution keeps its construction-snapshot
    fallback — the additive contract every scope axis ships with.

    ``origin_principal_id`` (PR #809 review finding 4) rides the same
    rail.  It is DORMANT today — no producer emits principals until the
    ISSUE-0082 Part 2 orchestrator emission (v0.3.14) — but threading it
    now, with the same leaf reader / re-entry / cascade seeding, means
    the activation inherits no executor-hop gap: without this, the leak
    class ISSUE-0118 closed for epoch would resurface on the tenant axis
    the day the rail lights up.
    """

    cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH
    origin_channel_id: str = ""
    origin_interaction_id: str = ""
    origin_synthesis_turn: bool = False
    # RFC 0037 §G (PR 7): the turn's tripwire watch — §D-withheld entries'
    # span fingerprints, stamped by ``_inject_memory_context`` and lifted
    # structurally by :meth:`for_event` (same rationale as the origin pair:
    # a per-site kwarg would make "unwatched" the silent fallback).  ``None``
    # (the common case — nothing withheld, or an origin-less context) means
    # the executor's §G check no-ops.
    origin_tripwire_watch: TripwireWatch | None = None
    # ISSUE-0118: the per-request epoch/session axes — see the class
    # docstring.  Threaded structurally, not per-site, for the same
    # silent-fallback reason as every other field here.  Principal (PR
    # #809 review finding 4) is the dormant third axis on the same rail.
    origin_epoch_id: str = ""
    origin_session_id: str = ""
    origin_principal_id: str = ""

    @classmethod
    def for_event(cls, event: AgentEvent, *, cascade_depth: int) -> DispatchContext:
        """The context for actions produced in reply to ``event``: the origin
        pair is derived here, structurally — through the drift-pinned
        :func:`wire_interaction_id` reader — so an ingress site cannot thread
        the channel without the interaction id (or vice versa). The synthesis
        marker rides the same derivation (strict ``is True``, matching the
        gate's admission read) so the reply-echo contract cannot be
        half-threaded either.  The epoch/session/principal axes
        (ISSUE-0118; principal per PR #809 review finding 4) ride the same
        derivation through the leaf readers the ``on_event`` binders
        consume, so the handler-side scopes and the executor-side re-entry
        always agree on the request's ``(epoch, session, principal)``."""
        return cls(
            cascade_depth=cascade_depth,
            origin_channel_id=event.channel_id or "",
            origin_interaction_id=wire_interaction_id(event),
            origin_synthesis_turn=(event.payload or {}).get("synthesis_turn") is True,
            origin_tripwire_watch=tripwire_watch_from_event(event),
            origin_epoch_id=epoch_id_from_metadata(event.metadata) or "",
            origin_session_id=session_id_from_metadata(event.metadata) or "",
            origin_principal_id=principal_id_from_metadata(event.metadata) or "",
        )

    def request_scopes(self) -> AbstractContextManager[None]:
        """Re-enter the per-request epoch/session/principal scopes for the
        executor's action processing (ISSUE-0118; principal per PR #809
        review finding 4 — dormant until the rail's v0.3.14 producer).

        The ``on_event`` handler binds these scopes task-locally for its own
        lifetime, but the executor runs on the DISPATCHING task — across the
        queued ``EventLoop`` hop the ContextVars never cross, so before this
        seam the executor-side memory reads and writes (the end-vote close
        discharge persisting the voter's interaction, a child cascade's
        recall) resolved the tiers' construction snapshots: boot epoch
        ``live`` / legacy session, the exact F-3 fallback ISSUE-0118 pins.
        Entering the scopes around action execution makes call-time
        resolution (``resolve_active_epoch`` / ``_resolve_session_list``)
        see the request's world instead.

        Empty fields enter nothing (a shared :func:`~contextlib.nullcontext`
        when all are empty), so event-less contexts, ticks, and
        pre-rail producers keep the construction-snapshot fallback — the
        same additive contract as the handler-side binders.
        """
        if not (
            self.origin_epoch_id
            or self.origin_session_id
            or self.origin_principal_id
        ):
            return nullcontext()
        return self._enter_request_scopes()

    @contextmanager
    def _enter_request_scopes(self) -> Iterator[None]:
        """Body behind :meth:`request_scopes` — session, then principal,
        then epoch, matching ``request_scope_from_metadata``'s binder
        order."""
        with ExitStack() as stack:
            if self.origin_session_id:
                stack.enter_context(session_scope(self.origin_session_id))
            if self.origin_principal_id:
                stack.enter_context(principal_scope(self.origin_principal_id))
            if self.origin_epoch_id:
                stack.enter_context(epoch_scope(self.origin_epoch_id))
            yield

    def same_channel_claim(
        self, target_channel: str, *, claim_synthesis_reply: bool = True,
    ) -> dict[str, object] | None:
        """Build a publish's RFC 0052 no-reopen claim from THIS context: a
        SAME-channel publish echoes its dispatched-under interaction id as
        the wire ``interaction_id`` claim; a cross-channel publish — or an
        origin-less context (IP8 re-convene) — claims nothing (``None``).
        The claim is the no-reopen latch's sole production input on every
        same-channel publish path (the reply, end-vote, and error-recovery
        publishes), so the rule lives here, beside the origin fields it
        reads, rather than once per publish site (PR #716 review): a rule
        change applied to one site and not the others would leave that
        path's post-close stragglers minting fresh and re-fanning into the
        closed discussion. The resolver stays authoritative (IP2): the claim
        never keys governance state, it only lets the latch recognise
        post-close traffic.

        The ``synthesis_reply`` echo (PR #718 review) rides structurally off
        :attr:`origin_synthesis_turn` — a context method rather than the
        earlier per-call-site kwarg, for the class docstring's own reason: a
        defaulted kwarg made "unmarked" the silent fallback of any publish
        site that forgot to thread it. A publish authored in reply to the §D
        synthesis directive additionally carries the marker — the
        discriminator ``claimSynthesisReply``
        (internal/channels/synthesis_claim.go) requires beside sender+claim
        to recognise the closing artifact, because the id claim alone is
        shared with every ordinary reply in the interaction. It rides BESIDE
        the id claim (never instead of it): the id claim is what lets an
        orphaned reply — the arm disarmed by a racing end-vote close — land
        in the no-reopen latch instead of minting fresh and reopening.
        Stamped only on a same-channel claim: a cross-channel or origin-less
        publish cannot be the closing artifact of the interaction it does
        not claim. ``claim_synthesis_reply=False`` is the NAMED opt-out for
        the one publish that must never be claimable as the artifact — the
        error-reply apology (``chat_reply.publish_chat_error_on_channel``) —
        so the withhold is spelled at the seam instead of implied by an
        omitted kwarg.
        """
        if self.origin_interaction_id and target_channel == self.origin_channel_id:
            claim: dict[str, object] = {"interaction_id": self.origin_interaction_id}
            if claim_synthesis_reply and self.origin_synthesis_turn:
                claim["synthesis_reply"] = True
            return claim
        return None
