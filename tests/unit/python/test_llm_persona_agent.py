"""Tests for _LLMPersonaAgent core behavior."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import TaskStatus
from agents.llm_client import LLMClient, LLMResponse, StopReason, ToolCall, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.memory_context import MemoryInjectionResult
from agents.persona_types import ActionType, AgentEvent, EventType, Mood

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client, _task

# ─── _LLMPersonaAgent Tests ───────────────────────────────


class TestLLMPersonaAgent:
    """Tests for the concrete LLM-powered persona agent."""

    async def _make_agent(
        self,
        config: dict | None = None,
        llm_client: LLMClient | None = None,
    ) -> _LLMPersonaAgent:
        """Helper to create an initialized _LLMPersonaAgent with mocked memory."""
        cfg = config or {**_PERSONA_CONFIG}
        client = llm_client or _make_client()

        agent = create_persona_agent(
            agent_id=cfg["id"],
            config=cfg,
            llm_client=client,
        )
        await agent.initialize_memory()
        return agent

    async def test_on_event_returns_actions(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "How's the sprint going?"},
            sender_id="iron-fox",
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1
        # Default LLM response text → COMPLETE_TASK fallback
        assert actions[0].action_type == ActionType.COMPLETE_TASK

    async def test_on_event_with_task_assigned(self):
        agent = await self._make_agent()
        task = _task("Review the architecture document")
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": task},
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1

    async def test_handle_backward_compatibility(self):
        agent = await self._make_agent()
        output = await agent.handle(_task("Write a design doc"))
        assert output.status == TaskStatus.COMPLETED
        assert "handle this task" in output.result

    async def test_system_prompt_contains_persona(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "Ember Owl" in prompt
        assert "VP of Engineering" in prompt
        assert "Engineering leadership" in prompt
        assert "15 years in software engineering" in prompt

    async def test_system_prompt_contains_behavior(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "Communication style:" in prompt
        assert "Says exactly what they think" in prompt  # directness: direct

    async def test_system_prompt_contains_goals(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "Ship v2.0 on time" in prompt
        assert "Reduce tech debt by 20%" in prompt
        assert "Prove the team can self-organize" in prompt

    async def test_system_prompt_contains_quirks(self):
        agent = await self._make_agent()
        prompt = agent._build_system_prompt()
        assert "What's on fire?" in prompt

    async def test_system_prompt_contains_dynamic_state(self):
        agent = await self._make_agent()
        agent._state.mood = Mood.FRUSTRATED
        agent._state.stress_level = 0.8
        prompt = agent._build_system_prompt()
        assert "frustrated" in prompt
        assert "Stress level: 0.8/1.0" in prompt

    async def test_format_event_task_assigned(self):
        agent = await self._make_agent()
        task = _task("Review code")
        event = AgentEvent(
            event_type=EventType.TASK_ASSIGNED,
            payload={"task": task},
        )
        msg = agent._format_event(event)
        assert "assigned a task" in msg
        assert "Review code" in msg

    async def test_format_event_message_received(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Hello Sarah"},
            sender_id="mike",
        )
        msg = agent._format_event(event)
        assert "Message from mike" in msg
        assert "Hello Sarah" in msg

    async def test_format_event_mention(self):
        agent = await self._make_agent()
        event = AgentEvent(
            event_type=EventType.MENTION,
            payload={"content": "@sarah check this"},
            sender_id="devops-bot",
        )
        msg = agent._format_event(event)
        assert "mentioned by devops-bot" in msg

    async def test_format_event_tick(self):
        agent = await self._make_agent()
        event = AgentEvent(event_type=EventType.TICK)
        msg = agent._format_event(event)
        assert "Autonomous tick" in msg

    async def test_multi_turn_tool_use(self):
        """LLM calls a tool, tool result fed back, final response parsed."""
        responses = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="tc1", name="recall_notes", input={"query": "sprint"})],
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(100, 50),
            ),
            LLMResponse(
                text="The sprint is on track based on my notes.",
                stop_reason=StopReason.END_TURN,
                usage=Usage(200, 100),
            ),
        ]
        client = _make_client(responses)
        agent = await self._make_agent(llm_client=client)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "Sprint status?"},
            sender_id="mike",
        )
        actions = await agent.on_event(event)
        assert len(actions) >= 1
        # LLM was called twice (tool use + final response)
        assert client._provider.create_message.call_count == 2

    async def test_energy_drains_on_actions(self):
        agent = await self._make_agent()
        assert agent._state.energy == 1.0
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
            sender_id="test",
        )
        await agent.on_event(event)
        # Should drain for the COMPLETE_TASK action
        assert agent._state.energy < 1.0

    async def test_on_tick_recovers_energy(self):
        agent = await self._make_agent()
        agent._state.energy = 0.5
        # Patch _inject_memory_context to return non-zero tokens so the
        # RFC 0017 §F empty-context TICK short-circuit does not fire.
        # Without this, the TICK returns DO_NOTHING early without calling
        # the LLM, preventing energy drain and resulting in 0.6 instead of
        # the expected 0.55 (0.5 + 0.1 recovery - 0.05 drain).
        with patch.object(
            agent,
            "_inject_memory_context",
            return_value=MemoryInjectionResult(memory_admitted_tokens=200),
        ):
            await agent.on_tick()
        # Energy recovered by 0.1, then drained by 0.05 for the action
        assert agent._state.energy == pytest.approx(0.55)

    async def test_persona_state_property(self):
        agent = await self._make_agent()
        agent._state.mood = Mood.FOCUSED
        state_dict = agent.persona_state
        assert state_dict["mood"] == "focused"

    async def test_persona_state_persistence(self):
        """Serialize → persist to DB → load from DB → values match."""
        cfg = {**_PERSONA_CONFIG}
        client = _make_client()

        agent1 = create_persona_agent(
            agent_id="ember-owl", config=cfg, llm_client=client,
        )
        await agent1.initialize_memory()
        agent1._state.mood = Mood.SATISFIED
        agent1._state.energy = 0.6
        agent1._state.stress_level = 0.4
        agent1._state.goal_progress = {"v2": 0.8}
        await agent1._persist_persona_state()

        # Load back from the actual DB via _load_persona_state()
        # (same agent / same DB connection — exercises the full DB path)
        restored = await agent1._load_persona_state()
        assert restored.mood is Mood.SATISFIED
        assert restored.energy == 0.6
        assert restored.stress_level == 0.4
        assert restored.goal_progress == {"v2": 0.8}

        await agent1.close_memory()

    async def test_parse_actions_json_array(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text=json.dumps([
                {
                    "action_type": "send_channel_message",
                    "payload": {"channel_id": "general", "content": "hi"},
                },
                {"action_type": "complete_task", "payload": {"result": "done"}},
            ]),
        )
        actions = agent._parse_actions(response)
        assert len(actions) == 2
        assert actions[0].action_type == ActionType.SEND_CHANNEL_MESSAGE
        assert actions[1].action_type == ActionType.COMPLETE_TASK

    async def test_parse_actions_json_code_block(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text=(
                'Here are my actions:\n```json\n'
                '[{"action_type": "do_nothing", "payload": {}}]\n```'
            ),
        )
        actions = agent._parse_actions(response)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.DO_NOTHING

    async def test_parse_actions_plain_text_fallback(self):
        agent = await self._make_agent()
        response = LLMResponse(text="I'll work on the documentation.")
        actions = agent._parse_actions(response)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "documentation" in actions[0].payload["result"]

    async def test_parse_actions_unknown_action_type_skipped(self):
        agent = await self._make_agent()
        response = LLMResponse(
            text=json.dumps([
                {"action_type": "fly_to_moon", "payload": {}},
                {"action_type": "complete_task", "payload": {"result": "ok"}},
            ]),
        )
        actions = agent._parse_actions(response)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK

    async def test_llm_error_returns_error_action(self):
        mock_provider = AsyncMock()
        mock_provider.create_message = AsyncMock(
            side_effect=RuntimeError("API timeout")
        )
        mock_provider.format_tool_definitions = MagicMock(return_value=[])
        client = LLMClient(mock_provider)
        agent = await self._make_agent(llm_client=client)

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "test"},
        )
        actions = await agent.on_event(event)
        assert len(actions) == 1
        assert actions[0].action_type == ActionType.COMPLETE_TASK
        assert "LLM provider error" in actions[0].payload["result"]

    def test_persona_runtime_mixins_require_non_none_llm_client(self):
        """RFC 0020 PR 6 slice 7 — ``_llm_client`` annotation tightened
        to :class:`LLMClient` (no ``| None``).

        Pins the construction-time invariant: ``_LLMPersonaAgent.__init__``
        already takes a required ``llm_client: LLMClient`` kwarg, but
        until slice 7 the persona-runtime mixins
        (:class:`_ActionLoopMixin`, :class:`_EpisodeRoutingMixin`)
        declared ``_llm_client: LLMClient | None`` to match
        :class:`BaseAgent`'s loose annotation.  That kept two dead
        silent-drop branches alive in production code:

        * ``_on_event_inner`` returned ``"LLM client not configured"``
          on the ``None`` path.
        * ``_persist_closed_interaction`` early-returned on the ``None``
          path, skipping the close-path persistence entirely.

        Both branches were reachable only via the ``agent._llm_client = None``
        test seam in the prior ``test_no_llm_client``.  Slice 7 removes
        the branches and tightens the annotation so a future refactor
        that re-widens to ``| None`` is caught immediately rather than
        re-opening the silent-drop surface (PR-4 review #25 deferred
        from slice 1).

        Accepts either annotation form against the :class:`LLMClient`
        class object so the contract survives a PEP 563 flip:

        * Today, both mixin modules carry ``from __future__ import
          annotations``, so ``cls.__annotations__["_llm_client"]`` is
          the source-text string ``"LLMClient"``.
        * If a future cleanup drops the future-import (Python 3.13 PEP
          649 makes it optional), the same entry becomes the evaluated
          ``LLMClient`` class object.

        A bare ``ann == "LLMClient"`` check would silently start
        green-passing the wrong thing under the second form (the
        equality returns ``False`` against a class object even when
        the annotation is correct, but the failure message would point
        at PEP 563 mechanics rather than the intended invariant).
        Handling both forms here keeps the assertion focused on the
        invariant — *the annotation resolves to* :class:`LLMClient` —
        rather than on its source-level encoding (PR-6 review #2
        robustness).

        We deliberately read ``cls.__annotations__`` directly rather
        than using :func:`typing.get_type_hints`: the mixins also
        annotate attributes whose types are imported only under
        ``if TYPE_CHECKING:`` (e.g. ``MemoryNamespace`` on
        :class:`_EpisodeRoutingMixin`), and ``get_type_hints``
        evaluates *all* annotations on the class, raising
        :class:`NameError` for any TYPE_CHECKING-only name absent from
        the runtime namespace.  The per-attribute check sidesteps that
        without forcing the production modules to drop their
        TYPE_CHECKING-gated imports.
        """
        from agents.persona_runtime.action_loop import _ActionLoopMixin
        from agents.persona_runtime.episode_routing import _EpisodeRoutingMixin

        for cls in (_ActionLoopMixin, _EpisodeRoutingMixin):
            ann = cls.__annotations__["_llm_client"]
            ok = ann == "LLMClient" or ann is LLMClient
            assert ok, (
                f"{cls.__name__}._llm_client annotation is {ann!r}; "
                "RFC 0020 PR 6 slice 7 pins it to bare ``LLMClient`` "
                "(no ``| None``) so the silent-drop branches in "
                "``_on_event_inner`` / ``_persist_closed_interaction`` "
                "stay dead in production."
            )

    async def test_lock_serializes_concurrent_events(self):
        """Verify the per-agent lock actually serializes concurrent on_event calls.

        Uses asyncio.gather to run two events concurrently. A shared list
        records enter/exit markers — if the lock works, one event fully
        completes before the other starts (no interleaving).

        Review finding: previous test only checked isinstance() which is
        a no-op verification.
        """
        agent = await self._make_agent()
        order: list[str] = []
        original_inner = agent._on_event_inner

        async def _tracking_inner(event: AgentEvent) -> list:
            label = event.payload.get("label", "?")
            order.append(f"enter-{label}")
            result = await original_inner(event)
            order.append(f"exit-{label}")
            return result

        agent._on_event_inner = _tracking_inner  # type: ignore[assignment]

        e1 = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "first", "label": "1"},
            sender_id="a",
        )
        e2 = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "second", "label": "2"},
            sender_id="b",
        )
        await asyncio.gather(agent.on_event(e1), agent.on_event(e2))

        # With proper lock serialization, events must not interleave:
        # either [enter-1, exit-1, enter-2, exit-2] or [enter-2, exit-2, enter-1, exit-1]
        assert order[0].startswith("enter-")
        assert order[1].startswith("exit-")
        assert order[0][-1] == order[1][-1]  # same label = same event
        assert order[2].startswith("enter-")
        assert order[3].startswith("exit-")
        assert order[2][-1] == order[3][-1]
        await agent.close_memory()

    async def test_close_memory_persists_state(self):
        agent = await self._make_agent()
        agent._state.mood = Mood.ENERGIZED
        await agent.close_memory()
        # After close, the state should have been persisted (no error)
