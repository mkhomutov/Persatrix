"""ActionExecutor — executes ``AgentAction`` lists produced by persona agents.

Extracted from :mod:`agents.dispatch` to keep that module focused on the
:class:`agents.dispatch.EventDispatcher` and to keep both modules under the
project file-size limit.  Public callers should keep importing
:class:`ActionExecutor` from :mod:`agents.dispatch` — the re-export there is
the stable public surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from .channel_publisher import (
    DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ChannelPublisher,
    ChannelsDisabledError,
)
from .channel_wire_metadata import DispatchContext
from .confidentiality_tripwire import run_channel_message_tripwire
from .end_vote_action import notify_end_vote_outcome, publish_end_interaction_vote
from .epoch_id import EVENT_EPOCH_METADATA_KEY
from .observability.spans import SUBAGENT_SPAWN_SPAN
from .persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
)
from .principal_id import EVENT_PRINCIPAL_METADATA_KEY
from .session_id import EVENT_SESSION_METADATA_KEY

if TYPE_CHECKING:
    from .dispatch import EventDispatcher

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)

__all__ = ["ActionExecutor"]

# Maximum mentions per SEND_CHANNEL_MESSAGE action — caps fan-out for the
# legacy in-process cascade so an LLM-generated mentions list cannot trigger
# an N^D dispatch storm (N=mentions, D=max_cascade_depth).
# (PR #55 review: unbounded mentions list → resource exhaustion.)
_MAX_MENTIONS_PER_ACTION = 10

# Per-dispatch timeout (seconds) for the legacy in-process cascade. Bounds a
# single hop so a hung target agent cannot block the sender indefinitely.
# TODO(v0.3): make configurable via config["dispatch_timeout"].
# (PR #60 review: hard-coded 60s dispatch timeout.)
_DEFAULT_DISPATCH_TIMEOUT: float = 60.0

# Per-publish HTTP timeout (seconds): a defense-in-depth ceiling wrapping any
# :class:`ChannelPublisher` impl (the HTTP one self-times via
# :class:`aiohttp.ClientTimeout`, but the Protocol allows non-HTTP impls that
# may not). PR #250 review (Should-Fix #1): aliased to the publisher's
# :data:`DEFAULT_PUBLISH_TIMEOUT_SECONDS` so both timers always agree —
# raise the ceiling in :mod:`agents.channel_publisher`.
_DEFAULT_PUBLISH_HTTP_TIMEOUT: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS


class ActionExecutor:
    """Executes ``AgentAction`` lists produced by persona agents.

    Handles each action type exhaustively. ``SEND_CHANNEL_MESSAGE`` either
    publishes via REST (when a :class:`ChannelPublisher` is configured) or
    falls back to the in-process :class:`EventDispatcher` cascade.
    ``DELEGATE`` and ``SPAWN_SUB_AGENT`` are TODO stubs for future RFCs.
    """

    def __init__(
        self,
        dispatcher: EventDispatcher | None = None,
        channel_publisher: ChannelPublisher | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._channel_publisher = channel_publisher

    def set_channel_publisher(self, publisher: ChannelPublisher | None) -> None:
        """Inject the REST publisher post-construction."""
        self._channel_publisher = publisher

    @property
    def channel_publisher(self) -> ChannelPublisher | None:
        """Public accessor for the configured REST publisher (may be ``None``).

        ISSUE-0065 — exposed so the inbound channel-event processing path
        (:func:`agents.chat_reply.process_inbound_channel_event`, run by the
        event loop's ``on_inbound`` callback) can publish a structured-error
        reply on the originating channel when the persona action loop raises
        :class:`BudgetExceededError`, without reaching into the executor's
        private attribute.
        """
        return self._channel_publisher

    @property
    def dispatcher(self) -> EventDispatcher | None:
        """Public accessor for the owning :class:`EventDispatcher` (may be
        ``None`` for session-less fixtures).

        Lets the running-loop inbound path
        (:meth:`agents.tick.TickScheduler._handle_inbound_event`) read the
        dispatcher's configured ``max_cascade_depth`` instead of hardcoding
        the default — so all three inbound paths share one ceiling
        (PR 4 review (1))."""
        return self._dispatcher

    async def execute(
        self,
        agent_id: str,
        actions: list[AgentAction],
        *,
        context: DispatchContext,
    ) -> list[dict[str, Any]]:
        """Execute actions and return per-action status dicts.

        Non-fatal failures are logged but do not propagate.

        ``context`` is the originating dispatch's ambient context, threaded
        WHOLE (PR #716 review — previously three parallel defaulted kwargs,
        where "unstamped" was the silent fallback for a caller that forgot
        one):

        * ``cascade_depth`` propagates to child dispatches so the cascade
          depth limit is enforced across the full event chain; its dataclass
          default keeps the terminate-at-clamp posture (see
          :class:`DispatchContext` for the v0.3.0 runaway rationale).
        * ``origin_channel_id`` / ``origin_interaction_id``: the inbound
          event's channel and dispatched-under interaction id (seeded by
          ``seed_wire_metadata``; derived structurally by
          :meth:`DispatchContext.for_event`). A SAME-channel publish echoes
          the id as its ``interaction_id`` claim — the RFC 0052 no-reopen
          latch's read (PR #716 review: unstamped post-close stragglers
          minted fresh and REOPENED the closed discussion). The resolver
          stays authoritative (IP2); an origin-less context stamps nothing
          (IP8 re-convene).
        * ``origin_epoch_id`` / ``origin_session_id`` / the dormant
          ``origin_principal_id`` (ISSUE-0118; PR #809 review finding 4):
          the per-request scope axes,
          re-entered as task-local scopes around the whole action loop via
          :meth:`DispatchContext.request_scopes`.  ``on_event`` binds these
          scopes only for its own lifetime; this method runs on the
          DISPATCHING task — across the queued ``EventLoop`` hop the
          handler's ContextVars never reach here — so before this seam
          every executor-side memory read/write (the end-vote close
          discharge persisting the voter's interaction, a child cascade's
          recall) resolved the tiers' construction snapshots (boot epoch
          ``live`` / legacy session): the F-3 fallback ISSUE-0118 pins.
          Empty fields enter nothing, keeping the event-less / tick /
          pre-rail postures unchanged.
        """
        results: list[dict[str, Any]] = []
        with context.request_scopes():
            for action in actions:
                result = await self._execute_one(agent_id, action, context=context)
                results.append(result)
        return results

    async def _execute_one(
        self,
        agent_id: str,
        action: AgentAction,
        *,
        context: DispatchContext,
    ) -> dict[str, Any]:
        """Execute a single action; status contract is documented in the README of this module.

        Returned dict always carries ``action_type`` and ``status``.
        SEND_CHANNEL_MESSAGE additionally carries both ``channel_id`` and
        ``dispatched_to`` regardless of branch (REST publish vs in-process
        dispatch). Fields not applicable to the chosen branch are set to
        ``None`` rather than omitted, so consumers can read either field
        without shape-defensive code (ISSUE-0027). Specifically:

        * REST branch: ``dispatched_to`` is ``None`` because the
          orchestrator owns fan-out — the agent has no per-recipient
          count to report.
        * Legacy branch: ``channel_id`` is the value the agent emitted
          (empty string when the payload omitted it).
        """
        match action.action_type:
            case ActionType.COMPLETE_TASK:
                return {
                    "action_type": "complete_task",
                    "status": "completed",
                    "result": action.payload.get("result", ""),
                }
            case ActionType.SEND_CHANNEL_MESSAGE:
                return await self._handle_send_channel_message(
                    agent_id, action, context=context)
            case ActionType.USE_TOOL:
                # Tool execution happens inside _on_event_inner() via
                # _execute_tools(). USE_TOOL as a final action means the
                # LLM wants to use a tool outside the multi-turn loop.
                logger.warning(
                    "Agent %s returned USE_TOOL as a final action — "
                    "tool calls should happen inside on_event() loop",
                    agent_id,
                )
                return {"action_type": "use_tool", "status": "skipped"}
            case ActionType.DO_NOTHING:
                return {"action_type": "do_nothing", "status": "ok"}
            case ActionType.DELEGATE:
                logger.info(
                    "Agent %s requested delegation to %s (not yet implemented)",
                    agent_id, action.payload.get("agent_id", "unknown"),
                )
                return {"action_type": "delegate", "status": "not_implemented"}
            case ActionType.SPAWN_SUB_AGENT:
                # The agent.subagent.spawn span ships now (RFC 0019 § D) so
                # the span name and attribute keys are pinned before the
                # real spawner lands in RFC 0009.
                _spawn_attrs: dict[str, str] = {
                    "agent.id": agent_id,
                    "subagent.status": "not_implemented",
                }
                _role_raw = action.payload.get("role", "")
                _role = str(_role_raw) if _role_raw else ""
                if _role:
                    _spawn_attrs["subagent.role"] = _role
                with _tracer.start_as_current_span(
                    SUBAGENT_SPAWN_SPAN, attributes=_spawn_attrs,
                ):
                    logger.info(
                        "Agent %s requested sub-agent spawn (not yet implemented)",
                        agent_id,
                    )
                return {"action_type": "spawn_sub_agent", "status": "not_implemented"}
            case ActionType.REQUEST_APPROVAL:
                logger.info("Agent %s requested approval (not yet implemented)", agent_id)
                return {"action_type": "request_approval", "status": "not_implemented"}
            case ActionType.GRANT_APPROVAL:
                logger.info("Agent %s granted approval (not yet implemented)", agent_id)
                return {"action_type": "grant_approval", "status": "not_implemented"}
            case ActionType.DENY_APPROVAL:
                logger.info("Agent %s denied approval (not yet implemented)", agent_id)
                return {"action_type": "deny_approval", "status": "not_implemented"}
            case ActionType.END_INTERACTION_VOTE:
                # RFC 0030 Layer 4 vote producer (producer plan PR 2, IP6) —
                # carved into end_vote_action.py for the 500-line cap.
                result = await publish_end_interaction_vote(
                    self._channel_publisher, agent_id, action, context=context)
                # PR 607 review finding 5: tell the voter how its publish
                # went so the parked local close is discharged — close on
                # "published", drop on any failure status.  (Carved into
                # end_vote_action.py with its publish sibling for the
                # 500-line cap — ISSUE-0118 pushed this module over.)
                await notify_end_vote_outcome(self._dispatcher, agent_id, result)
                return result
            case _:
                # Defensive catch-all: Python match is not exhaustive at the
                # type level. Without this, a new ActionType variant would
                # implicitly return None and break the dict[str, Any] contract.
                logger.warning(
                    "Agent %s: unhandled action type %s",
                    agent_id, action.action_type.value,
                )
                return {"action_type": action.action_type.value, "status": "unhandled"}

    async def _handle_send_channel_message(
        self,
        sender_id: str,
        action: AgentAction,
        *,
        context: DispatchContext,
    ) -> dict[str, Any]:
        """Route SEND_CHANNEL_MESSAGE.

        Two transport branches (RFC 0011 PR 4a-ii-β-1):

        * ``channel_id`` set + :class:`ChannelPublisher` configured →
          ``POST /api/v1/channels/{channel_id}/messages`` (orchestrator
          fans out via the Go ``GRPCMessageDispatcher``).
        * Otherwise → in-process :class:`EventDispatcher` cascade,
          preserved for the chat-reply path until PR 4a-ii-β-2.

        A reply to the ORIGINATING channel echoes its dispatched-under
        interaction id as the wire claim (see :meth:`execute`); cross-channel
        publishes stamp nothing.
        """
        target_channel = action.payload.get("channel_id", "")
        content = action.payload.get("content", "")
        mentions = action.payload.get("mentions", [])

        # PR #250 review (Must-Fix #2): the LLM payload is untyped — if the
        # model emits ``"mentions": "agent-b"`` (str) or ``{...}`` (dict),
        # the original ``len(mentions)`` / ``mentions[:N]`` / ``list(mentions)``
        # chain silently corrupts the wire payload (string → per-char list,
        # dict → list-of-keys, int → TypeError mid-handler). Coerce to []
        # at the boundary and log a WARNING so the prompt regression is
        # visible to operators rather than masquerading as ghost mentions.
        if not isinstance(mentions, list):
            logger.warning(
                "Agent %s SEND_CHANNEL_MESSAGE mentions is not a list "
                "(got %s); coercing to [] — check prompt/persona output schema",
                sender_id, type(mentions).__name__,
            )
            mentions = []

        if len(mentions) > _MAX_MENTIONS_PER_ACTION:
            logger.warning(
                "Agent %s SEND_CHANNEL_MESSAGE mentions list truncated from %d to %d",
                sender_id, len(mentions), _MAX_MENTIONS_PER_ACTION,
            )
            mentions = mentions[:_MAX_MENTIONS_PER_ACTION]

        # RFC 0037 §G (PR 7): the leak tripwire — observability only, runs
        # once per outgoing message over the turn's §D-withheld watch
        # (threaded structurally on the context), never blocks or raises.
        run_channel_message_tripwire(
            watch=context.origin_tripwire_watch,
            agent_id=sender_id,
            channel_id=target_channel,
            content=content,
        )

        # ── REST publish branch (channel-routed) ──
        if target_channel and self._channel_publisher is not None:
            # RFC 0052 no-reopen claim (docstring) via the context's shared
            # rule; None keeps the clean body. The §D synthesis reply-echo —
            # the fanout-head claim's third conjunct — now rides structurally
            # off the context too (PR #718 review), no per-site kwarg to
            # forget (see DispatchContext.same_channel_claim).
            publish_metadata = context.same_channel_claim(target_channel)
            try:
                await asyncio.wait_for(
                    self._channel_publisher.publish(
                        channel_id=target_channel,
                        sender_id=sender_id,
                        content=content,
                        mentions=list(mentions),
                        metadata=publish_metadata,
                        # RFC 0011 amendment "Cascade-depth wire propagation"
                        # (PR 3 of v0.3.0 channel test-findings plan): the
                        # ``+1`` increment lives on the dispatcher side
                        # (see ``EventDispatcher.dispatch`` —
                        # ``cascade_depth=depth + 1`` is what arrives on
                        # this kwarg), so the executor forwards the value
                        # verbatim. Re-incrementing here would fire the
                        # orchestrator's fanout cap one hop early relative
                        # to RFC 0011 §D's depth-5 ceiling.
                        cascade_depth=context.cascade_depth,
                    ),
                    timeout=_DEFAULT_PUBLISH_HTTP_TIMEOUT,
                )
            except ChannelsDisabledError:
                # ISSUE-0026: orchestrator has channels disabled. The
                # publisher already fired a one-shot WARN on the first
                # 503 with the response body for diagnostics; per-action
                # logs stay at DEBUG so operator log volume does not
                # scale linearly with action count. The dedicated
                # status keeps the LLM from interpreting this as a
                # transient ``failed`` it should retry.
                logger.debug(
                    "SEND_CHANNEL_MESSAGE short-circuited (channels disabled "
                    "at orchestrator): agent=%s channel=%s",
                    sender_id, target_channel,
                )
                return {
                    "action_type": "send_channel_message",
                    "status": "channels_disabled",
                    "channel_id": target_channel,
                    "dispatched_to": None,
                }
            except Exception as exc:  # noqa: BLE001 — surfaced via "failed" status
                logger.warning(
                    "Channel publish from %s to %s failed: %s",
                    sender_id, target_channel, exc,
                    exc_info=not isinstance(exc, TimeoutError),
                )
                return {
                    "action_type": "send_channel_message",
                    "status": "failed",
                    "channel_id": target_channel,
                    "dispatched_to": None,
                }
            return {
                "action_type": "send_channel_message",
                "status": "published",
                "channel_id": target_channel,
                "dispatched_to": None,
            }

        # ── Legacy in-process dispatch branch ──
        if self._dispatcher is None:
            logger.warning(
                "Agent %s sent message but no dispatcher configured", sender_id,
            )
            return {
                "action_type": "send_channel_message",
                "status": "no_dispatcher",
                "channel_id": target_channel,
                "dispatched_to": None,
            }

        if not mentions:
            if target_channel:
                logger.warning(
                    "Agent %s SEND_CHANNEL_MESSAGE to channel %s has no mentions and "
                    "no REST publisher configured — message not routed",
                    sender_id, target_channel,
                )
            else:
                logger.debug(
                    "Agent %s SEND_CHANNEL_MESSAGE has no mentions, message not routed",
                    sender_id,
                )
            return {
                "action_type": "send_channel_message",
                "status": "no_targets",
                "channel_id": target_channel,
                "dispatched_to": 0,
            }

        dispatched = 0
        for target_id in mentions:
            try:
                # Synthesize a CHANNEL_MESSAGE the response gate will admit:
                # mentioned recipients in the legacy cascade are by-construction
                # mentioned, so set ``respond_policy=when_mentioned`` and forward
                # the mention list. Without these the gate fails closed under
                # the unknown-policy branch (RFC 0011 PR 4b, #252).
                #
                # ISSUE-0118: forward the origin epoch/session onto the child
                # event so the child's ``on_event`` binds the SAME request
                # world.  The scopes re-entered around this loop
                # (``execute``'s ``request_scopes``) cover only the direct
                # same-task branch; a child routed through the target's
                # queued ``EventLoop`` runs on the supervisor task, where
                # ambient ContextVars cannot follow — the metadata keys are
                # the rail that crosses that hop.  Only non-empty values are
                # seeded (key-absence is the binders' nullcontext contract).
                child_metadata: dict[str, Any] = {
                    "cascade_depth": context.cascade_depth,
                }
                if context.origin_session_id:
                    child_metadata[EVENT_SESSION_METADATA_KEY] = (
                        context.origin_session_id
                    )
                if context.origin_epoch_id:
                    child_metadata[EVENT_EPOCH_METADATA_KEY] = (
                        context.origin_epoch_id
                    )
                if context.origin_principal_id:
                    child_metadata[EVENT_PRINCIPAL_METADATA_KEY] = (
                        context.origin_principal_id
                    )
                event = AgentEvent(
                    event_type=EventType.CHANNEL_MESSAGE,
                    payload={
                        "content": content,
                        "channel_id": target_channel,
                        "mentions": list(mentions),
                        "respond_policy": "when_mentioned",
                    },
                    channel_id=target_channel,
                    sender_id=sender_id,
                    metadata=child_metadata,
                )
                await asyncio.wait_for(
                    self._dispatcher.dispatch(target_id, event),
                    timeout=_DEFAULT_DISPATCH_TIMEOUT,
                )
                dispatched += 1
            except TimeoutError:
                logger.warning(
                    "Dispatch from %s to %s timed out after %.0fs",
                    sender_id, target_id, _DEFAULT_DISPATCH_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 — execute() must not propagate
                logger.warning(
                    "Failed to dispatch message from %s to %s",
                    sender_id, target_id, exc_info=True,
                )

        # Use "failed" when all dispatches errored so callers don't see a
        # successful-looking result with dispatched_to=0.
        # (F-60-6: status "dispatched" with dispatched_to=0 is misleading.)
        status = "dispatched" if dispatched > 0 else "failed"
        return {
            "action_type": "send_channel_message",
            "status": status,
            "channel_id": target_channel,
            "dispatched_to": dispatched,
        }
