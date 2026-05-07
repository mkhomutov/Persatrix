"""RFC 0011 PR 5 follow-up — on-startup catch-up replay-mode flag.

The catch-up fetcher (see :mod:`agents.channel_catchup`) builds
``CHANNEL_MESSAGE`` events with ``metadata["replay_mode"] = True`` and
hands them to ``agent.on_event`` so the persona runtime ingests channel
history into memory **without** invoking the LLM and without producing
an outbound ``SEND_CHANNEL_MESSAGE``. This file pins the action-loop
side of that contract.

Contract:

* ``metadata["replay_mode"] is True`` short-circuits ``_on_event_inner``
  *after* :func:`agents.persona_runtime.channel_ingest.sanitize_inbound_event`
  runs and *before* the response gate / LLM path. The seam matches the
  RFC 0011 PR 5 ingest pipeline (sanitize → tracker → store) so the
  channel-history tier sees the row.
* The agent never calls the LLM and returns
  ``[AgentAction(action_type=DO_NOTHING, payload={})]``.
* ``_store_event_episode`` is called so the replayed turn lands in
  ``InteractionTracker`` (and the channel-history tier becomes
  recall-eligible). The agent's own outbound messages — ``sender_id ==
  agent_id`` — are skipped (defense-in-depth: the orchestrator's
  cleartext gRPC port could echo our outbound back, and double-counting
  would inflate ``turn_count`` and write a turn whose ``payload.sender
  == agent_id`` for a peer-keyed scope).
* The ``channel.messages.replayed`` counter increments once per replayed
  event. The metric is **separate** from ``channel.messages.gated`` so a
  noisy startup catch-up does not mask a real gate-suppression spike on
  dashboards.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.persona_types import ActionType, AgentEvent, EventType

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client


async def _make_agent() -> _LLMPersonaAgent:
    cfg = {**_PERSONA_CONFIG}
    agent = create_persona_agent(
        agent_id=cfg["id"],
        config=cfg,
        llm_client=_make_client(),
    )
    await agent.initialize_memory()
    return agent


def _replay_event(
    *,
    sender_id: str = "iron-fox",
    channel_id: str = "group:planning",
    content: str = "old message",
    mentions: list[str] | None = None,
) -> AgentEvent:
    return AgentEvent(
        event_type=EventType.CHANNEL_MESSAGE,
        payload={
            "content": content,
            "channel_type": "group",
            "mentions": list(mentions or []),
            "respond_policy": "when_mentioned",
        },
        channel_id=channel_id,
        sender_id=sender_id,
        message_id="msg-replay-1",
        metadata={"replay_mode": True},
    )


class TestReplayModeShortCircuit:
    async def test_replay_event_returns_do_nothing(self):
        """A replay-mode event must produce exactly one DO_NOTHING action.

        The contract is "ingest only, never respond" — even if the
        underlying message would have triggered a reply (mentions the
        agent, ``always`` policy, etc.), replay must not send.
        """
        agent = await _make_agent()
        # Mention the agent so the gate would have admitted normally.
        evt = _replay_event(mentions=["ember-owl"])
        actions = await agent.on_event(evt)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING

    async def test_replay_event_skips_llm(self):
        """Replay-mode must short-circuit before the LLM call.

        Asserts the LLM client's ``create_message`` is never invoked.
        Replay traffic costs zero LLM tokens by construction.
        """
        agent = await _make_agent()
        evt = _replay_event(mentions=["ember-owl"])
        await agent.on_event(evt)

        agent._llm_client._provider.create_message.assert_not_called()  # type: ignore[attr-defined]

    async def test_replay_event_calls_store_event_episode(self):
        """Replay must drive ``_store_event_episode`` so the row lands in
        ``InteractionTracker`` and feeds the channel-history tier.

        Without this, on-startup catch-up would fail its primary purpose:
        ingesting the messages the agent missed while offline.
        """
        agent = await _make_agent()
        evt = _replay_event(mentions=["ember-owl"])

        with patch.object(
            agent, "_store_event_episode", new=AsyncMock(),
        ) as store_mock:
            await agent.on_event(evt)

        store_mock.assert_awaited_once()
        # Empty actions list — replay never produced any actions.
        called_event, called_actions = store_mock.await_args.args
        assert called_event is evt
        assert called_actions == []

    async def test_replay_event_with_self_sender_skips_ingest(self):
        """Defense-in-depth: a replayed message whose sender is this
        agent must NOT be re-ingested.

        The orchestrator's history endpoint returns the agent's own
        outbound messages alongside other members'. Re-ingesting them
        would inflate ``turn_count`` and (for peer-keyed DM scopes)
        write a turn whose ``payload.sender == agent_id``. The same
        rationale that drives the live-path
        ``POLICY_DEFENSE_IN_DEPTH`` skip applies here.
        """
        agent = await _make_agent()
        evt = _replay_event(sender_id="ember-owl")  # sender == agent_id

        with patch.object(
            agent, "_store_event_episode", new=AsyncMock(),
        ) as store_mock:
            actions = await agent.on_event(evt)

        store_mock.assert_not_called()
        # Still returns DO_NOTHING — no outbound, no exception.
        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING

    async def test_replay_event_runs_sanitization(self):
        """Replay-mode must not bypass inbound sanitization.

        The sanitizer is the canonical defense for prompt-injection at
        the LLM-content boundary; skipping it for catch-up traffic
        would create a hole an attacker could drive through by waiting
        for a target agent to restart. The pipeline order is
        sanitize → replay-short-circuit, NOT the reverse.
        """
        agent = await _make_agent()
        evt = _replay_event()

        with patch.object(
            agent, "_sanitize_inbound_event", wraps=agent._sanitize_inbound_event,
        ) as san_mock:
            await agent.on_event(evt)

        san_mock.assert_called_once_with(evt)

    async def test_non_replay_event_falls_through_to_gate(self):
        """Negative case: an event without ``replay_mode`` (or with
        ``replay_mode=False``) must NOT short-circuit. The gate runs and
        the LLM is invoked when the gate admits the event.
        """
        agent = await _make_agent()
        # Non-replay event mentioning the agent — gate admits, LLM fires.
        evt = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            payload={
                "content": "hi ember-owl",
                "channel_type": "group",
                "mentions": ["ember-owl"],
                "respond_policy": "when_mentioned",
            },
            channel_id="group:planning",
            sender_id="iron-fox",
            message_id="msg-live-1",
            # metadata is empty — no replay_mode.
        )
        await agent.on_event(evt)

        # LLM was called for live traffic.
        agent._llm_client._provider.create_message.assert_called()  # type: ignore[attr-defined]


class TestReplayModeMetric:
    """Pin the dedicated ``channel.messages.replayed`` counter.

    Separating replay from the gate counter keeps dashboards honest:
    a startup-catch-up burst does not look like a wave of policy-driven
    suppressions.
    """

    async def test_replayed_counter_increments_on_replay(self, monkeypatch):
        agent = await _make_agent()

        captured: list[tuple[int, dict[str, str]]] = []

        class _StubCounter:
            def add(self, n: int, attributes: dict[str, str]) -> None:
                captured.append((n, attributes))

        class _StubInst:
            channel_messages_replayed = _StubCounter()

        monkeypatch.setattr(
            "agents.persona_runtime.action_loop.try_get_instruments",
            lambda: _StubInst(),
        )

        evt = _replay_event()
        await agent.on_event(evt)

        assert len(captured) == 1
        n, attrs = captured[0]
        assert n == 1
        assert attrs.get("channel_id") == "group:planning"

    async def test_replayed_counter_silent_for_self_sender(
        self, monkeypatch,
    ):
        """The ``channel.messages.replayed`` counter measures *ingestions
        into memory*, not events received by the short-circuit. A
        replayed event whose sender is this agent is skipped from
        ``_store_event_episode`` (see
        ``test_replay_event_with_self_sender_skips_ingest``) so the
        counter MUST stay silent for that path.

        Why this matters: the metric description pins
        "messages replayed through the on-startup catch-up fetch", and
        operator dashboards interpret the value as "rows written to
        InteractionTracker". Counting self-sender events that were
        intentionally dropped would inflate the gauge by however many
        of the agent's own outbound messages happen to sit in the
        last-N history window — masking real ingestion regressions.
        PR-265 review L5.
        """
        agent = await _make_agent()

        captured: list[tuple[int, dict[str, str]]] = []

        class _StubCounter:
            def add(self, n: int, attributes: dict[str, str]) -> None:
                captured.append((n, attributes))

        class _StubInst:
            channel_messages_replayed = _StubCounter()

        monkeypatch.setattr(
            "agents.persona_runtime.action_loop.try_get_instruments",
            lambda: _StubInst(),
        )

        # Self-sender replay: ingest is skipped, so counter must stay 0.
        evt = _replay_event(sender_id="ember-owl")
        await agent.on_event(evt)

        assert captured == []

    async def test_replayed_counter_silent_when_instruments_absent(
        self, monkeypatch,
    ):
        """When ``try_get_instruments`` returns ``None`` (test mode, no
        OTLP exporter wired) the short-circuit must still complete
        without raising.
        """
        agent = await _make_agent()

        monkeypatch.setattr(
            "agents.persona_runtime.action_loop.try_get_instruments",
            lambda: None,
        )

        evt = _replay_event()
        actions = await agent.on_event(evt)

        assert len(actions) == 1
        assert actions[0].action_type is ActionType.DO_NOTHING


# pytest-asyncio plugin auto-detects ``async def`` tests via
# ``asyncio_mode = "auto"`` in ``pyproject.toml``; no marker needed.
