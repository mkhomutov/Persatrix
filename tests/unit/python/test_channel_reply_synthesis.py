"""ISSUE-0048 — synthesise SEND_CHANNEL_MESSAGE for plain-text channel replies.

A persona LLM that has not been prompt-trained on the JSON action schema
returns conversational reply text on a CHANNEL_MESSAGE turn. The action
parser folds that text into a single ``COMPLETE_TASK`` action — which the
``ActionExecutor`` records as ``status=completed`` and never publishes.
The orchestrator-side ``replyWaiter`` therefore never sees a reply on the
inbound channel and the chat-as-DM round-trip 504s on the
``chatDefaultTimeout``.

The runtime closes that gap by synthesising a ``SEND_CHANNEL_MESSAGE``
back to the inbound channel whenever a ``CHANNEL_MESSAGE`` turn produced
only conversational text. The synthesis is conservative:

* It fires only for ``CHANNEL_MESSAGE`` events with a non-empty
  ``channel_id`` — the legacy ``SendChatMessage`` path (no channel_id)
  and non-channel events (``TICK``, ``TASK_ASSIGNED``, …) are untouched.
* It is a no-op when the LLM did emit an explicit ``SEND_CHANNEL_MESSAGE``
  for the same channel — a well-prompted agent is never double-published.
* It is a no-op when the only candidate reply text is empty or
  whitespace-only — empty replies must not turn into ghost publishes.

These tests pin both the pure helper and the action-loop integration.
"""

from __future__ import annotations

from agents.llm_client import LLMResponse
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_runtime.channel_reply import synthesize_channel_reply
from agents.persona_types import ActionType, AgentAction, AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


# ─── Pure helper: synthesize_channel_reply ────────────────


def _channel_message_event(
    *,
    channel_id: str = "dm:alice:ember-owl",
    channel_type: str = "dm",
    sender_id: str = "alice",
    content: str = "say hi",
) -> AgentEvent:
    # ``channel_type`` is threaded explicitly rather than being hard-coded
    # to ``"dm"`` so a test that overrides ``channel_id`` to a group/thread
    # prefix can keep the payload-level ``channel_type`` consistent with
    # the wire shape. The synthesiser itself only reads ``channel_id``,
    # but a reader of the test would be confused by a ``channel_type=dm``
    # row paired with a ``channel_id=group:…``.
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": content,
            "channel_type": channel_type,
            "mentions": ["ember-owl"],
            "respond_policy": "always",
        },
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-1",
        metadata={"sender_participant_type": "user"},
    )


class TestSynthesizeChannelReply:
    """Pure-function contract — no agent state, no LLM."""

    def test_fallback_complete_task_becomes_send_channel_message(self):
        """The bug ISSUE-0048 documents.

        A LLM that didn't emit JSON actions returns conversational text;
        ``_parse_actions`` wraps it in a ``COMPLETE_TASK``. Without
        synthesis the orchestrator's reply waiter never sees a publish.
        """
        event = _channel_message_event()
        actions = [
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "Hi Alice, what's on your mind?"},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        send_actions = [
            a for a in result if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        synthesized = send_actions[0]
        assert synthesized.payload["channel_id"] == "dm:alice:ember-owl"
        assert synthesized.payload["content"] == "Hi Alice, what's on your mind?"
        # Mention the inbound sender so the priority-1 chat-reply
        # extraction (legacy SendChatMessage) still picks the reply.
        assert synthesized.payload["mentions"] == ["alice"]

    def test_existing_send_channel_message_is_not_duplicated(self):
        """A well-prompted agent that already emits SEND_CHANNEL_MESSAGE
        for the inbound channel must not get a second one stapled on.

        Double-publishing leaks user-visible reply twice through the
        replyWaiter's single-shot semantics — the second publish would
        be silently dropped by the waiter but persisted to the channel
        store, which inflates message counts.
        """
        event = _channel_message_event()
        actions = [
            AgentAction(
                action_type=ActionType.SEND_CHANNEL_MESSAGE,
                payload={
                    "channel_id": "dm:alice:ember-owl",
                    "content": "explicit reply",
                    "mentions": ["alice"],
                },
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        send_actions = [
            a for a in result if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        assert send_actions[0].payload["content"] == "explicit reply"

    def test_send_channel_message_for_other_channel_does_not_count(self):
        """An LLM cross-posting to a different channel still leaves the
        inbound channel without a reply — synthesis must fill that gap.
        """
        event = _channel_message_event(channel_id="dm:alice:ember-owl")
        actions = [
            AgentAction(
                action_type=ActionType.SEND_CHANNEL_MESSAGE,
                payload={
                    "channel_id": "group:planning",
                    "content": "fyi",
                    "mentions": [],
                },
            ),
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "Got it, Alice."},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        dm_replies = [
            a for a in result
            if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
            and a.payload.get("channel_id") == "dm:alice:ember-owl"
        ]
        assert len(dm_replies) == 1
        assert dm_replies[0].payload["content"] == "Got it, Alice."

    def test_empty_result_on_group_channel_is_not_synthesized(self):
        """An empty/whitespace COMPLETE_TASK on a *group* channel is a
        valid silent turn — the reply-discretion prompt snippet
        explicitly tells the persona it may stay silent on groups when
        it has nothing to add. Synthesising a blank publish would
        defeat that affordance.
        """
        event = _channel_message_event(
            channel_id="group:planning", channel_type="group",
        )
        actions = [
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "   \n  "},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        assert result == actions
        assert all(
            a.action_type is not ActionType.SEND_CHANNEL_MESSAGE for a in result
        )

    def test_empty_result_on_dm_falls_back_to_minimal_reply(self):
        """On a DM channel, the response-gate invariant is that a DM
        with no reply is broken by construction
        (``response_gate.py``: ``dm`` branch unconditionally admits).
        When the LLM still produces no usable reply text — typically
        the model failed to follow the reply-discretion guidance — the
        synthesiser falls back to a minimal placeholder so the
        chat-as-DM REST round-trip closes cleanly rather than 504ing
        on ``chatDefaultTimeout``.

        The placeholder is an ellipsis (``…``): unambiguous in audit
        logs, naturalistic as a "I have nothing to add" signal, and
        narrow enough that the model is the right place to fix the
        root cause rather than this fallback.
        """
        event = _channel_message_event(channel_id="dm:alice:ember-owl")
        actions = [
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "   \n  "},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        send_actions = [
            a for a in result if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        synthesized = send_actions[0]
        assert synthesized.payload["channel_id"] == "dm:alice:ember-owl"
        assert synthesized.payload["content"] == "…"
        assert synthesized.payload["mentions"] == ["alice"]

    def test_missing_result_on_dm_also_falls_back(self):
        """The fallback also fires when there is no COMPLETE_TASK
        candidate at all — for example when the LLM returned tool calls
        but no end-turn text. The DM-must-reply invariant does not
        depend on the *shape* of the missing reply.
        """
        event = _channel_message_event(channel_id="dm:alice:ember-owl")
        actions: list[AgentAction] = []

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        send_actions = [
            a for a in result if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        assert send_actions[0].payload["content"] == "…"

    def test_non_channel_event_is_passthrough(self):
        """TICK / TASK_ASSIGNED / etc. never produce channel replies —
        the synthesiser must touch only ``CHANNEL_MESSAGE`` turns.
        """
        event = AgentEvent(event_type=EventType.TICK)
        actions = [
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "tick reflection"},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")
        assert result == actions

    def test_legacy_chat_event_with_no_channel_id_is_passthrough(self):
        """The deprecated ``SendChatMessage`` RPC builds CHANNEL_MESSAGE
        events without a ``channel_id`` (ISSUE-0035). Synthesis would
        emit a ``SEND_CHANNEL_MESSAGE`` with empty channel_id — the
        action validator drops those, and ``ActionExecutor`` would log
        a "no targets" warning. Skip the legacy path.
        """
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={"content": "say hi"},
            sender_id="alice",
            # channel_id intentionally omitted — legacy chat surface.
        )
        actions = [
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "Hi Alice"},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")
        assert result == actions

    def test_synthesized_action_is_prepended(self):
        """The synthesised SEND_CHANNEL_MESSAGE is placed before the
        COMPLETE_TASK so the legacy ``_extract_chat_reply`` priority
        chain (priority 2) returns it ahead of the priority-3
        ``COMPLETE_TASK.result`` fallback.
        """
        event = _channel_message_event()
        actions = [
            AgentAction(
                action_type=ActionType.COMPLETE_TASK,
                payload={"result": "Hi"},
            ),
        ]

        result = synthesize_channel_reply(event, actions, agent_id="ember-owl")

        assert result[0].action_type is ActionType.SEND_CHANNEL_MESSAGE
        assert result[1].action_type is ActionType.COMPLETE_TASK


# ─── Integration: action-loop wiring ───────────────────────


async def _make_agent() -> _LLMPersonaAgent:
    cfg = {**_PERSONA_CONFIG}
    agent = create_persona_agent(
        agent_id=cfg["id"],
        config=cfg,
        llm_client=_make_client(
            responses=[LLMResponse(text="Hi Alice, what's on your mind?")],
        ),
    )
    await agent.initialize_memory()
    return agent


class TestActionLoopSynthesizesChannelReply:
    """End-to-end: a CHANNEL_MESSAGE turn with a plain-text LLM response
    must produce a SEND_CHANNEL_MESSAGE bound to the inbound channel.
    """

    async def test_channel_message_turn_produces_send_channel_message(self):
        agent = await _make_agent()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "say hi",
                "channel_type": "dm",
                "mentions": ["ember-owl"],
                "respond_policy": "always",
            },
            channel_id="dm:alice:ember-owl",
            sender_id="alice",
            message_id="msg-1",
            metadata={"sender_participant_type": "user"},
        )

        actions = await agent.on_event(event)

        send_actions = [
            a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        assert send_actions[0].payload["channel_id"] == "dm:alice:ember-owl"
        assert send_actions[0].payload["content"] == "Hi Alice, what's on your mind?"

    async def test_replay_mode_does_not_synthesize(self):
        """Replay-mode short-circuits before the LLM and returns
        DO_NOTHING — synthesis must not run and must not turn the
        replayed history row into an outbound publish.
        """
        agent = await _make_agent()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "previous chatter",
                "channel_type": "group",
                "mentions": [],
                "respond_policy": "when_mentioned",
            },
            channel_id="group:planning",
            sender_id="iron-fox",
            message_id="msg-replay",
            metadata={"replay_mode": True},
        )

        actions = await agent.on_event(event)

        assert all(
            a.action_type is not ActionType.SEND_CHANNEL_MESSAGE for a in actions
        )

    async def test_gate_suppression_does_not_synthesize(self):
        """When the response gate suppresses (e.g. ``when_mentioned``
        without a mention), the runtime returns DO_NOTHING before the
        LLM runs. Synthesis must not turn that into a chatty publish.
        """
        agent = await _make_agent()
        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "not for me",
                "channel_type": "group",
                "mentions": ["someone-else"],
                "respond_policy": "when_mentioned",
            },
            channel_id="group:planning",
            sender_id="iron-fox",
            message_id="msg-not-mentioned",
        )

        actions = await agent.on_event(event)

        assert all(
            a.action_type is not ActionType.SEND_CHANNEL_MESSAGE for a in actions
        )

    async def test_dm_with_empty_llm_response_falls_back_to_ellipsis(self):
        """End-to-end pin for the DM-empty fallback path.

        The pure-helper ``TestSynthesizeChannelReply`` cases above pin the
        synthesis contract in isolation. The action-loop cases above pin
        the *wiring* (replay short-circuit, gate suppression, no-double-
        publish). This test stitches both together: when the LLM returns
        the empty string on a DM channel, the action loop's parser folds
        it into ``COMPLETE_TASK(result="")``, synthesis sees no usable
        candidate, and the DM-must-reply invariant
        (``response_gate.py`` forces ``always`` on DMs) triggers the
        ``…`` fallback. A regression here would re-introduce the
        chat-as-DM ``chatDefaultTimeout`` 504 from ISSUE-0048 for the
        narrow case where the LLM silently no-ops on an addressed DM.
        """
        cfg = {**_PERSONA_CONFIG}
        agent = create_persona_agent(
            agent_id=cfg["id"],
            config=cfg,
            llm_client=_make_client(responses=[LLMResponse(text="")]),
        )
        await agent.initialize_memory()

        event = _channel_message_event(channel_id="dm:alice:ember-owl")

        actions = await agent.on_event(event)

        send_actions = [
            a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        synthesized = send_actions[0]
        assert synthesized.payload["channel_id"] == "dm:alice:ember-owl"
        assert synthesized.payload["content"] == "…"
        # Inbound sender mentioned so the priority-1 chat-reply extraction
        # (legacy SendChatMessage path, ``agents.chat_reply``) still picks
        # the synthesised reply ahead of the priority-3
        # ``COMPLETE_TASK.result`` fallback — same mention contract as the
        # non-fallback synthesis path.
        assert synthesized.payload["mentions"] == ["alice"]

    async def test_explicit_send_channel_message_is_not_doubled(self):
        """When the LLM returns proper JSON actions including a
        SEND_CHANNEL_MESSAGE for the inbound channel, no synthesis runs.
        """
        cfg = {**_PERSONA_CONFIG}
        explicit_json = (
            '[{"action_type": "send_channel_message", '
            '"payload": {"channel_id": "dm:alice:ember-owl", '
            '"content": "explicit reply", "mentions": ["alice"]}}]'
        )
        agent = create_persona_agent(
            agent_id=cfg["id"],
            config=cfg,
            llm_client=_make_client(
                responses=[LLMResponse(text=explicit_json)],
            ),
        )
        await agent.initialize_memory()

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "say hi",
                "channel_type": "dm",
                "mentions": ["ember-owl"],
                "respond_policy": "always",
            },
            channel_id="dm:alice:ember-owl",
            sender_id="alice",
            message_id="msg-2",
            metadata={"sender_participant_type": "user"},
        )

        actions = await agent.on_event(event)

        send_actions = [
            a for a in actions if a.action_type is ActionType.SEND_CHANNEL_MESSAGE
        ]
        assert len(send_actions) == 1
        assert send_actions[0].payload["content"] == "explicit reply"


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
