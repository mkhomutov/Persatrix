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

from .cascade_depth_defaults import DEFAULT_MAX_CASCADE_DEPTH
from .channel_publisher import (
    DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ChannelPublisher,
    ChannelsDisabledError,
)
from .observability.spans import SUBAGENT_SPAWN_SPAN
from .persona_types import (
    ActionType,
    AgentAction,
    AgentEvent,
    EventType,
)

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

# Per-publish HTTP timeout (seconds) for the REST channels publish path.
#
# Defense-in-depth ceiling that wraps any :class:`ChannelPublisher` impl
# (the HTTP one already self-times via :class:`aiohttp.ClientTimeout`,
# but Protocol allows non-HTTP implementations that may not).
#
# PR #250 review (Should-Fix #1): aliased to the publisher's
# :data:`DEFAULT_PUBLISH_TIMEOUT_SECONDS` so both timers always agree —
# raising the ceiling for RFC 0009 Phase 4 mTLS cold starts is a
# one-line change in :mod:`agents.channel_publisher`.
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

    async def execute(
        self,
        agent_id: str,
        actions: list[AgentAction],
        *,
        cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH,
    ) -> list[dict[str, Any]]:
        """Execute actions and return per-action status dicts.

        Non-fatal failures are logged but do not propagate. ``cascade_depth``
        is propagated to child dispatches so the cascade depth limit is
        enforced across the full event chain.

        ``cascade_depth`` defaults to :data:`DEFAULT_MAX_CASCADE_DEPTH`
        rather than ``0``: callers that omit the kwarg (notably the tick
        scheduler, which has no inbound event to derive depth from) get
        the orchestrator's terminate-at-clamp behaviour on any
        ``SEND_CHANNEL_MESSAGE`` they produce, instead of silently
        publishing at depth 0 and resetting any cascade in flight. The
        v0.3.0 demo runaway cascade was a direct consequence of the
        previous default — every channel message woke the tick scheduler,
        the woken tick published at depth 0, and the orchestrator's
        per-hop cap never fired. Callers that legitimately mark a publish
        as chain-origin (chat surface, dispatcher's first hop) pass
        ``cascade_depth=0`` explicitly; the safe default only fires for
        omitting callers.
        """
        results: list[dict[str, Any]] = []
        for action in actions:
            result = await self._execute_one(agent_id, action, cascade_depth=cascade_depth)
            results.append(result)
        return results

    async def _execute_one(
        self,
        agent_id: str,
        action: AgentAction,
        *,
        cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH,
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
                    agent_id, action, cascade_depth=cascade_depth,
                )
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
        cascade_depth: int = DEFAULT_MAX_CASCADE_DEPTH,
    ) -> dict[str, Any]:
        """Route SEND_CHANNEL_MESSAGE.

        Two transport branches (RFC 0011 PR 4a-ii-β-1):

        * ``channel_id`` set + :class:`ChannelPublisher` configured →
          ``POST /api/v1/channels/{channel_id}/messages`` (orchestrator
          fans out via the Go ``GRPCMessageDispatcher``).
        * Otherwise → in-process :class:`EventDispatcher` cascade,
          preserved for the chat-reply path until PR 4a-ii-β-2.
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

        # ── REST publish branch (channel-routed) ──
        if target_channel and self._channel_publisher is not None:
            try:
                await asyncio.wait_for(
                    self._channel_publisher.publish(
                        channel_id=target_channel,
                        sender_id=sender_id,
                        content=content,
                        mentions=list(mentions),
                        # RFC 0011 amendment "Cascade-depth wire propagation"
                        # (PR 3 of v0.3.0 channel test-findings plan): the
                        # ``+1`` increment lives on the dispatcher side
                        # (see ``EventDispatcher.dispatch`` —
                        # ``cascade_depth=depth + 1`` is what arrives on
                        # this kwarg), so the executor forwards the value
                        # verbatim. Re-incrementing here would fire the
                        # orchestrator's fanout cap one hop early relative
                        # to RFC 0011 §D's depth-5 ceiling.
                        cascade_depth=cascade_depth,
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
                    metadata={"cascade_depth": cascade_depth},
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
