"""Tick-path cascade-depth default — terminate-by-default safety pin.

When the tick scheduler executes an agent's ``SEND_CHANNEL_MESSAGE``
actions it has no inbound event to derive ``cascade_depth`` from
(unlike :meth:`agents.dispatch.EventDispatcher.dispatch`, which forwards
``depth + 1`` from the event it just received). Before this safety
default landed, ``tick.py`` called ``executor.execute(actions)`` with no
``cascade_depth`` kwarg, the executor defaulted the kwarg to ``0``, the
publisher's ``if cascade_depth:`` guard skipped the metadata write, and
the orchestrator stored every tick-originated publish with no
``cascade_depth`` at all — silently resetting any cascade-in-flight to
chain-origin on every hop.

A real-world manifestation of this regression was the v0.3.0 demo
walkthrough (``docs/guides/v0.3.0-demo.md``): each inbound channel
message wakes the tick scheduler via :meth:`EventDispatcher.dispatch`'s
``scheduler.wake()`` call, which fires an immediate tick whose on_tick
actions then publish at depth 0. The agents trade replies indefinitely
instead of capping at five hops.

The contract pinned here:

* ``agents.dispatch.DEFAULT_MAX_CASCADE_DEPTH`` is a module-level
  constant — callers that need the "no inbound depth known, terminate"
  value can import it without reflecting on a default kwarg.
* ``ActionExecutor.execute`` (and the executor's nested helpers) and
  ``ChannelPublisher.publish`` default ``cascade_depth`` to
  ``DEFAULT_MAX_CASCADE_DEPTH`` so callers that omit the kwarg get the
  safe "terminate at the orchestrator clamp" behaviour rather than
  the cascade-resetting "chain origin" behaviour.
* ``TickScheduler`` invokes ``executor.execute`` without forwarding a
  per-event depth (it has none); the safe default therefore reaches the
  publisher and the orchestrator's
  ``cascade_depth >= max_cascade_depth`` clamp drops fanout on the
  tick-originated publish.

A regression that returns the default to ``0`` (or that adds a tick
path that publishes channel messages without explicitly threading the
inbound depth through) lands as a red test here rather than as a paid
runaway cascade in production.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.dispatch import ActionExecutor, EventDispatcher
from agents.llm_client import LLMClient, LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentAction
from agents.tick import TickScheduler
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _lower_min_interval():
    """Production ``TickScheduler._MIN_INTERVAL`` is 1.0s to prevent cost
    bursts; tests need fast intervals to avoid multi-second waits.
    """
    original = TickScheduler._MIN_INTERVAL
    TickScheduler._MIN_INTERVAL = 0.01
    yield
    TickScheduler._MIN_INTERVAL = original


_PERSONA_CONFIG: dict = {
    "id": "iron-fox",
    "type": "persona",
    "name": "Iron Fox",
    "role": "Cascade-default tick pin",
    "model": "test-model",
    "temperature": 0.7,
    "max_llm_calls": 5,
    "max_tokens": 1024,
    "persona": {
        "title": "Cascade-default tick pin",
        "background": "Tick-path cascade-depth default test stand-in.",
        "behavior": {
            "directness": "direct",
            "detail_focus": "big-picture",
            "formality": "professional",
            "risk_tolerance": "moderate",
            "expressiveness": "reserved",
        },
    },
    "permissions": {
        "memory": {"read": True, "write": True},
    },
    "memory": {
        "db_path": ":memory:",
        "notes": {"max_notes": 100, "auto_reflect_after": 5},
    },
}


def _stub_llm() -> LLMClient:
    """Minimal LLM client — never called in these tests (on_tick is
    overridden). Present only because _LLMPersonaAgent construction
    requires one.
    """
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(return_value=LLMResponse(text=""))
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(side_effect=lambda m, r, t: m)
    return LLMClient(mock_provider)


async def _make_agent() -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=_PERSONA_CONFIG["id"],
        config=_PERSONA_CONFIG,
        llm_client=_stub_llm(),
    )
    assert isinstance(agent, _LLMPersonaAgent)
    await agent.initialize_memory()
    return agent


class TestDefaultMaxCascadeDepthConstant:
    """The default value must be importable as a named constant so
    callers (tick scheduler, future code paths that need the "no
    inbound depth known" sentinel) reference one source of truth
    rather than re-hardcoding ``5``.
    """

    def test_constant_exported_from_dispatch_module(self) -> None:
        from agents.dispatch import DEFAULT_MAX_CASCADE_DEPTH

        assert isinstance(DEFAULT_MAX_CASCADE_DEPTH, int)
        assert DEFAULT_MAX_CASCADE_DEPTH >= 1

    def test_constant_matches_event_dispatcher_default(self) -> None:
        """The module constant and the dispatcher's keyword default
        MUST agree — they are the same value, sourced once.
        """
        import inspect

        from agents.dispatch import DEFAULT_MAX_CASCADE_DEPTH

        sig = inspect.signature(EventDispatcher.__init__)
        assert sig.parameters["max_cascade_depth"].default == DEFAULT_MAX_CASCADE_DEPTH


class TestActionExecutorDefaultCascadeDepth:
    """``ActionExecutor.execute`` callers that omit the kwarg must hit
    the "terminate at the orchestrator clamp" default, not the
    cascade-origin ``0`` that previously masked the regression.
    """

    async def test_execute_default_cascade_depth_forwards_to_publisher(self) -> None:
        """``executor.execute(...)`` without an explicit ``cascade_depth``
        forwards :data:`DEFAULT_MAX_CASCADE_DEPTH` to the channel
        publisher.

        Pins the contract for the tick-scheduler call site at
        ``agents/tick.py`` — the scheduler does not thread a per-event
        depth (it has none), so the executor's default is what reaches
        the wire. ``DEFAULT_MAX_CASCADE_DEPTH`` is the only safe choice:
        the orchestrator's
        :func:`internal/channels.ChannelRouter.Publish` clamps incoming
        depth to ``[0, max]`` and drops fanout on
        ``clamped >= max_cascade_depth``, so a tick-originated publish
        at the cap is stored once and then chain-terminated.
        """
        from agents.dispatch import DEFAULT_MAX_CASCADE_DEPTH

        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute(
            "iron-fox",
            [
                AgentAction(
                    ActionType.SEND_CHANNEL_MESSAGE,
                    {
                        "channel_id": "group:planning",
                        "content": "tick-originated",
                        "mentions": [],
                    },
                ),
            ],
        )

        publisher.publish.assert_awaited_once()
        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["cascade_depth"] == DEFAULT_MAX_CASCADE_DEPTH, (
            "executor.execute() without an explicit cascade_depth must "
            "default to DEFAULT_MAX_CASCADE_DEPTH so tick-originated "
            "channel publishes are clamp-and-dropped at the orchestrator "
            f"rather than resetting cascade-in-flight; got {kwargs.get('cascade_depth')!r}"
        )

    async def test_execute_explicit_zero_still_honored(self) -> None:
        """An explicit ``cascade_depth=0`` is still respected — the safe
        default only kicks in when the caller omits the kwarg.

        Without this, the chat surface and the orchestrator-driven
        dispatcher path (both of which legitimately mark a publish as
        chain-origin) would silently get clamped to cap. The kwarg
        contract is "default to safe; explicit overrides".
        """
        publisher = AsyncMock()
        publisher.publish = AsyncMock(return_value=None)
        executor = ActionExecutor(channel_publisher=publisher)

        await executor.execute(
            "iron-fox",
            [
                AgentAction(
                    ActionType.SEND_CHANNEL_MESSAGE,
                    {
                        "channel_id": "group:planning",
                        "content": "origin",
                        "mentions": [],
                    },
                ),
            ],
            cascade_depth=0,
        )

        kwargs = publisher.publish.await_args.kwargs
        assert kwargs["cascade_depth"] == 0


class TestTickSchedulerCascadeDepth:
    """End-to-end pin: ``TickScheduler`` → ``ActionExecutor`` →
    ``ChannelPublisher`` delivers the safe-default depth on
    tick-originated channel publishes.
    """

    async def test_tick_emitted_send_channel_message_uses_default_cap_depth(
        self,
    ) -> None:
        """A tick-originated ``SEND_CHANNEL_MESSAGE`` must publish with
        ``cascade_depth == DEFAULT_MAX_CASCADE_DEPTH``.

        Regression guard for the v0.3.0 demo runaway cascade: every
        inbound channel message wakes the tick scheduler via
        ``scheduler.wake()`` in :meth:`EventDispatcher.dispatch`; if the
        woken tick produces its own ``SEND_CHANNEL_MESSAGE``, that
        publish has no inbound-event depth to inherit. Without the
        safe default, the tick publish lands at depth 0 and the
        cascade in-flight gets reset — producing the unbounded
        iron-fox/nova-sparrow loop observed in production.
        """
        from agents.dispatch import DEFAULT_MAX_CASCADE_DEPTH

        agent = await _make_agent()
        try:
            async def _channel_publish_tick() -> list[AgentAction]:
                return [
                    AgentAction(
                        ActionType.SEND_CHANNEL_MESSAGE,
                        {
                            "channel_id": "group:planning",
                            "content": "autonomous status",
                            "mentions": [],
                        },
                    ),
                ]

            agent.on_tick = _channel_publish_tick  # type: ignore[assignment]

            publisher = AsyncMock()
            publisher.publish = AsyncMock(return_value=None)
            executor = ActionExecutor(channel_publisher=publisher)

            scheduler = TickScheduler(
                agent, interval=0.05, idle_after_ticks=100, executor=executor,
            )
            scheduler.start()
            # Loose deadline: tick fires after one interval; one publish
            # is enough to pin the contract. Bound on real time bumped
            # to tolerate slow CI without false-negative.
            for _ in range(40):
                await asyncio.sleep(0.05)
                if publisher.publish.await_count >= 1:
                    break
            await scheduler.stop()

            assert publisher.publish.await_count >= 1, (
                "tick scheduler did not invoke the channel publisher — "
                "test setup regressed before reaching the cascade-depth check"
            )
            kwargs = publisher.publish.await_args.kwargs
            assert kwargs["cascade_depth"] == DEFAULT_MAX_CASCADE_DEPTH, (
                "tick-originated SEND_CHANNEL_MESSAGE must publish at "
                f"cascade_depth={DEFAULT_MAX_CASCADE_DEPTH} so the orchestrator's "
                "clamp drops fanout instead of treating the publish as a fresh "
                f"chain origin; got cascade_depth={kwargs.get('cascade_depth')!r}. "
                "If you added a new way to invoke executor.execute from the tick "
                "loop, thread cascade_depth=DEFAULT_MAX_CASCADE_DEPTH (or the "
                "inbound depth, if the call site knows it) explicitly."
            )
        finally:
            await agent.close_memory()
