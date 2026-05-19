"""RFC 0011 cascade-depth wire-round-trip backstop pin (PR 4).

PR 4 of the [v0.3.0 channel test-findings PR plan]
(../../docs/v0.3.0-test-findings-pr-plan.md). PRs 2 and 3 each pin one
side of the wire boundary in isolation:

* PR 2 (Go orchestrator) — REST decode of ``metadata.cascade_depth``,
  clamp, fanout cap, outbound proto ``cascade_depth``. Pinned by
  ``internal/channels/router_cascade_depth_test.go`` and
  ``internal/server/channel_handlers_cascade_depth_test.go``.
* PR 3 (Python publisher + servicer) — emit ``metadata.cascade_depth``
  in the POST body, read ``request.cascade_depth`` from the proto,
  defense-in-depth dispatcher cap. Pinned by
  ``tests/unit/python/test_channel_publisher_cascade_depth.py`` and
  ``tests/unit/python/test_response_gate_cascade_backstop.py``.

Each suite is correct in isolation. The regression PR 4 is here to
catch is: someone restores the old "publish doesn't carry depth"
behavior on any **one** of the four wire hops (publisher emit,
orchestrator REST decode, dispatcher outbound proto, servicer
proto-read). PRs 2 and 3's unit tests stay green in that case because
the regression is on the *seam* between them — only a wire round-trip
fails.

The test wires:

* Two real ``AgentServiceServicer`` instances (one persona agent each)
  on real ephemeral 127.0.0.1 gRPC ports — the production receiver
  path including ``request.cascade_depth`` → ``AgentEvent.metadata``.
* Two real ``HTTPChannelPublisher`` instances pointed at a Python
  aiohttp "orchestrator stub" — the production emitter path including
  ``cascade_depth`` → ``body.metadata.cascade_depth``.
* An aiohttp stub that implements the Go orchestrator's depth contract
  (read ``metadata.cascade_depth``, clamp to
  ``[0, max_cascade_depth]``, store, drop fanout at
  ``clamped >= max_cascade_depth``, otherwise dispatch via real gRPC
  with the unchanged depth on the typed proto field).

The stub stands in for the Go orchestrator because spinning the real
binary up from pytest plus injecting a no-LLM persona is heavier
scaffolding than the cross-process pin justifies — the Go-side wire
hops are independently covered. The stub's job is faithful contract
mirroring, NOT regression coverage for Go.

Bound on reply count derivation (cap=5, ``depth >= 5 → drop fanout``):

1. User publish at depth 0 — stored, fanout at depth 0 dispatches to
   both members; each replies once at depth 1 → 2 publishes stored.
2. Depths 1, 2, 3, 4 — each level produces 2 publishes (one per
   member), each fans out to the OTHER member; the receiver replies
   once at the next depth → 2 publishes stored at each level.
3. Depth 5 — 2 publishes stored, but fanout is suppressed by the cap
   so the chain terminates.

Stored reply messages = 2 × 5 = **10**. Total channel size including
the initial user publish = 11. The PR plan's derivation of "8" missed
that the depth-5 publishes are stored before fanout-cap fires; the
upper bound asserted here is the actual cap-terminated count.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import grpc
import grpc.aio
import pytest
from aiohttp import web

from agents.channel_publisher import HTTPChannelPublisher
from agents.dispatch import EventDispatcher
from agents.generated import task_pb2, task_pb2_grpc
from agents.llm_client import LLMClient, LLMResponse, StopReason, Usage
from agents.persona import create_persona_agent
from agents.persona_runtime import _LLMPersonaAgent
from agents.server_servicers import AgentServiceServicer
from agents.tools.registry import clear_registry

# Mirrors ``internal/defaults/defaults.go::DefaultMaxCascadeDepth`` and
# ``agents/dispatch.py::EventDispatcher.max_cascade_depth``. Inlined here
# (rather than imported from one side) so a regression that desyncs the
# two enforcement points surfaces against this test's expected upper
# bound rather than against a value that silently drifts with the cap.
_MAX_CASCADE_DEPTH = 5

# Upper bound on reply messages stored at the orchestrator stub. See
# the module docstring derivation. Loose enough to tolerate per-recipient
# timing variation in async fanout (the spec calls for ``<= 8`` but the
# actual cap-terminated count is 10 — the spec missed that depth-5
# publishes are stored before fanout-cap fires).
_REPLY_UPPER_BOUND = 10

# Bound on how long the cascade can stay "in motion" before the test
# concludes it has stabilized. Generous so loaded CI runners do not
# false-fail; tight enough that an unbounded cascade regression
# surfaces as a test timeout rather than wasting the whole 2-min
# pytest deadline.
_CASCADE_QUIESCE_SECONDS = 1.5
_CASCADE_TOTAL_TIMEOUT_SECONDS = 30.0

_CHANNEL_ID = "group:planning"
_CHANNEL_TYPE = "group"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _persona_config(agent_id: str) -> dict[str, Any]:
    return {
        "id": agent_id,
        "model": "test-model",
        "role": "Cascade-backstop cross-process pin",
        "type": "persona",
        "max_llm_calls": 5,
        "max_tokens": 1024,
        "tools": [],
        "persona": {
            "name": agent_id,
            "background": "Always-reply stub for PR 4 cross-process cascade pin.",
            "behavior": {
                "directness": "balanced",
                "formality": "professional",
                "risk_tolerance": "moderate",
            },
        },
        "autonomy": {
            "level": "semi-autonomous",
            "tick_interval_seconds": 1,
            "max_actions_per_tick": 3,
            "idle_after_ticks": 5,
        },
        "memory": {
            "db_path": ":memory:",
            "working": {"max_tokens": 50000},
        },
        "relationships": [],
    }


def _always_reply_llm() -> LLMClient:
    """Mock LLM that always emits a SEND_CHANNEL_MESSAGE to the test channel.

    The reply carries no mentions — the channel has two ``always``-respond
    members and the receiver-side response gate admits ``always`` traffic
    without consulting ``mentions``. Mentions would also work but would
    couple the cascade shape to mention-routing, which is a separate
    contract.
    """
    mock_provider = AsyncMock()
    mock_provider.create_message = AsyncMock(
        return_value=LLMResponse(
            text=(
                '```json\n[{"action_type": "send_channel_message", '
                '"payload": {"content": "auto-reply", '
                f'"channel_id": "{_CHANNEL_ID}"'
                "}}]\n```"
            ),
            stop_reason=StopReason.END_TURN,
            usage=Usage(10, 10),
        ),
    )
    mock_provider.format_tool_definitions = MagicMock(return_value=[])
    mock_provider.append_tool_round = MagicMock(
        side_effect=lambda msgs, resp, results: [
            *msgs,
            {"role": "assistant", "content": "tool round"},
            {"role": "user", "content": "tool results"},
        ],
    )
    return LLMClient(mock_provider)


async def _create_agent(agent_id: str) -> _LLMPersonaAgent:
    agent = create_persona_agent(
        agent_id=agent_id,
        config=_persona_config(agent_id),
        llm_client=_always_reply_llm(),
    )
    assert isinstance(agent, _LLMPersonaAgent)
    await agent.initialize_memory()
    return agent


class _OrchestratorStub:
    """Python aiohttp stub mirroring the Go orchestrator's publish contract.

    Faithfully implements the depth-handling pinned in PR 2:

    * Read ``metadata.cascade_depth`` from the POST body (default 0).
    * Clamp to ``[0, _MAX_CASCADE_DEPTH]``.
    * Store the message (depth-5 publishes are stored before the
      fanout cap fires — see ``internal/channels/router.go:232-247``).
    * Drop fanout when ``clamped >= _MAX_CASCADE_DEPTH``.
    * Otherwise dispatch via real gRPC ``ReceiveChannelMessage`` to every
      ``always``-respond member except the sender, with the unchanged
      depth on the typed ``cascade_depth`` proto field.

    The stub does NOT implement Go's full router (no thread-parent
    lookup, no metrics, no chat waiter) — only the depth contract this
    test pins.
    """

    def __init__(
        self,
        *,
        members: dict[str, str],
        max_cascade_depth: int = _MAX_CASCADE_DEPTH,
    ) -> None:
        # members maps agent_id -> "host:port" of the recipient gRPC
        # server. RespondPolicy is implicit "always" — the channel has
        # two always-respond members per the PR 4 spec.
        self._members = dict(members)
        self._max_cascade_depth = max_cascade_depth
        self._messages: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        # Track in-flight fanout dispatches so the test can wait for
        # the cascade to drain without polling on message count. Set
        # rather than counter so we can reason about exact lifecycle.
        self._inflight: set[asyncio.Task[Any]] = set()

    @property
    def messages(self) -> list[dict[str, Any]]:
        return list(self._messages)

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    async def handle_publish(self, request: web.Request) -> web.Response:
        body = await request.json()
        sender_id = body["sender_id"]
        content = body["content"]
        mentions = list(body.get("mentions", []))
        inbound_depth = int(body.get("metadata", {}).get("cascade_depth", 0))
        clamped = max(0, min(self._max_cascade_depth, inbound_depth))

        msg_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        stored = {
            "id": msg_id,
            "channel_id": _CHANNEL_ID,
            "channel_type": _CHANNEL_TYPE,
            "sender_id": sender_id,
            "content": content,
            "mentions": mentions,
            "timestamp": timestamp,
            "cascade_depth": clamped,
        }
        async with self._lock:
            self._messages.append(stored)

        if clamped < self._max_cascade_depth:
            # Schedule fanout as a tracked background task so the POST
            # response (and the agent's publish call site) does not
            # block on the rest of the cascade. Mirrors the Python
            # servicer's fire-and-forget dispatch shape (the orchestrator
            # acks the publish before the cascade is fully drained).
            task = asyncio.create_task(self._fanout(stored))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

        return web.json_response({"id": msg_id}, status=201)

    async def _fanout(self, msg: dict[str, Any]) -> None:
        for recipient_id, address in self._members.items():
            if recipient_id == msg["sender_id"]:
                continue
            try:
                await self._dispatch_grpc(recipient_id, address, msg)
            except Exception:  # noqa: BLE001 — best-effort fanout
                pass

    async def _dispatch_grpc(
        self, recipient_id: str, address: str, msg: dict[str, Any],
    ) -> None:
        event = task_pb2.ChannelMessageEvent(
            message_id=msg["id"],
            channel_id=msg["channel_id"],
            channel_type=msg["channel_type"],
            sender_id=msg["sender_id"],
            content=msg["content"],
            mentions=msg["mentions"],
            timestamp=msg["timestamp"],
            respond_policy="always",
            cascade_depth=int(msg["cascade_depth"]),
        )
        async with grpc.aio.insecure_channel(address) as channel:
            stub = task_pb2_grpc.AgentServiceStub(channel)
            await stub.ReceiveChannelMessage(event, timeout=5.0)

    async def wait_for_cascade_to_drain(
        self,
        *,
        quiesce: float = _CASCADE_QUIESCE_SECONDS,
        total_timeout: float = _CASCADE_TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        """Wait until both the fanout queue is empty AND no new message
        has been stored for ``quiesce`` seconds — a two-signal stability
        check that catches both "still dispatching" and "agent reply
        in-flight at the dispatcher".
        """
        deadline = asyncio.get_event_loop().time() + total_timeout
        last_count = -1
        last_change_ts = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)
            now = asyncio.get_event_loop().time()
            current = len(self._messages)
            if current != last_count:
                last_count = current
                last_change_ts = now
                continue
            if self.inflight_count == 0 and (now - last_change_ts) >= quiesce:
                return
        raise TimeoutError(
            f"cascade did not drain within {total_timeout}s "
            f"(messages={len(self._messages)}, inflight={self.inflight_count})",
        )


async def _start_orchestrator_stub(
    stub: _OrchestratorStub,
) -> tuple[str, web.AppRunner]:
    app = web.Application()
    app.router.add_post(
        "/api/v1/channels/{id}/messages", stub.handle_publish,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return f"http://127.0.0.1:{port}", runner


async def _start_agent_servicer(
    agent: _LLMPersonaAgent,
    dispatcher: EventDispatcher,
) -> tuple[str, grpc.aio.Server]:
    servicer = AgentServiceServicer({agent.agent_id: agent}, dispatcher=dispatcher)
    server = grpc.aio.server()
    task_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return f"127.0.0.1:{port}", server


@pytest.fixture
async def cascade_world() -> AsyncIterator[
    tuple[_OrchestratorStub, str]
]:
    """Stand up two agents + servicers + orchestrator stub fully wired.

    Yields the stub (for assertions) and the orchestrator base URL (so
    the test can fire the initial user publish via the same wire path
    a real chat client would use).
    """
    agent_a = await _create_agent("agent-a")
    agent_b = await _create_agent("agent-b")

    dispatcher_a = EventDispatcher(agents={"agent-a": agent_a})
    dispatcher_b = EventDispatcher(agents={"agent-b": agent_b})

    addr_a, server_a = await _start_agent_servicer(agent_a, dispatcher_a)
    addr_b, server_b = await _start_agent_servicer(agent_b, dispatcher_b)

    stub = _OrchestratorStub(
        members={"agent-a": addr_a, "agent-b": addr_b},
    )
    base_url, http_runner = await _start_orchestrator_stub(stub)

    # Real HTTPChannelPublisher per agent so the wire path under test
    # is the production emitter, not a stand-in. One shared aiohttp
    # session per agent process (we run both in one pytest process; in
    # production each agent has its own).
    session_a = aiohttp.ClientSession()
    session_b = aiohttp.ClientSession()
    dispatcher_a.set_channel_publisher(HTTPChannelPublisher(
        orchestrator_url=base_url, session=session_a,
    ))
    dispatcher_b.set_channel_publisher(HTTPChannelPublisher(
        orchestrator_url=base_url, session=session_b,
    ))

    try:
        yield stub, base_url
    finally:
        # Tear down in reverse-dependency order: gRPC servers stop FIRST so
        # no inbound dispatch can land while agents are closing memory; the
        # agents' own ``close_memory`` therefore runs LAST. Publisher sessions
        # and the stub server fall in between (publish side then receive side).
        await server_a.stop(grace=0)
        await server_b.stop(grace=0)
        await session_a.close()
        await session_b.close()
        await http_runner.cleanup()
        await agent_a.close_memory()
        await agent_b.close_memory()


async def test_cascade_terminates_at_max_cascade_depth(
    cascade_world: tuple[_OrchestratorStub, str],
) -> None:
    """One user publish → cap-terminated cascade with bounded reply count.

    Pins the wire round-trip end-to-end through real
    ``HTTPChannelPublisher`` (POST body ``metadata.cascade_depth``) and
    real ``AgentServiceServicer`` (proto ``cascade_depth`` →
    ``AgentEvent.metadata``). A regression that silently drops
    ``cascade_depth`` on ANY of the four Python-side wire hops surfaces
    as either an unbounded cascade (test timeout) or a violated upper
    bound — the unit tests stay green either way.
    """
    stub, base_url = cascade_world

    # Fire one user publish via the same REST path a chat client would
    # use. ``user-1`` is not a channel member so the sender filter does
    # not fold the cascade back into the user's inbox.
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/api/v1/channels/{_CHANNEL_ID}/messages",
            json={"sender_id": "user-1", "content": "kick off"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            assert resp.status == 201, await resp.text()

    await stub.wait_for_cascade_to_drain()

    messages = stub.messages
    initial = [m for m in messages if m["sender_id"] == "user-1"]
    replies = [m for m in messages if m["sender_id"] != "user-1"]

    assert len(initial) == 1, "the initial user publish must be stored exactly once"
    assert len(replies) <= _REPLY_UPPER_BOUND, (
        f"cascade must terminate within {_REPLY_UPPER_BOUND} replies "
        f"(max_cascade_depth={_MAX_CASCADE_DEPTH}); got {len(replies)} replies "
        f"— a regression on any Python-side wire hop for cascade_depth "
        f"would manifest as an unbounded cascade or an off-by-one here."
    )

    # Sanity: at least one reply landed. Without this guard, a
    # regression that turned every recipient gate into "always drop"
    # would pass the upper-bound assertion (0 <= 10) while silently
    # breaking the very flow the test is meant to exercise.
    assert len(replies) >= 1, (
        "cascade produced no replies — either the gate is dropping every "
        "message or the gRPC dispatch never reaches the agents"
    )

    # Every stored message MUST carry a clamped depth in
    # ``[0, max_cascade_depth]``. A regression that propagated
    # ``cascade_depth=999`` past the clamp would surface here even if
    # the cascade itself happened to terminate by some other means.
    for m in messages:
        depth = m["cascade_depth"]
        assert 0 <= depth <= _MAX_CASCADE_DEPTH, (
            f"stored message carries out-of-band cascade_depth={depth} "
            f"(must be clamped to [0, {_MAX_CASCADE_DEPTH}])"
        )

    # The cascade MUST actually reach the cap. Without this, a regression
    # that capped earlier (e.g. ``max_cascade_depth`` lowered from 5 to 3,
    # or an extra ``+1`` increment somewhere on the wire) would produce
    # fewer replies, still under ``_REPLY_UPPER_BOUND``, still ``>= 1``,
    # still in-range — and the test would silently pass on what is
    # effectively a tightened cap.
    #
    # Strict ``==`` (not ``>=``) is safe because the cascade shape is
    # deterministic in this fixture: AsyncMock LLM (no I/O variability),
    # in-memory sqlite, the stub mirrors the Go router's
    # store-before-fanout-cap ordering, both agents are ``always``-respond,
    # and ``wait_for_cascade_to_drain`` blocks until the message count is
    # quiescent. A spot-check probe over the same setup observed
    # ``{0:1, 1:2, 2:2, 3:2, 4:2, 5:2}`` deterministically across runs.
    max_observed = max(m["cascade_depth"] for m in messages)
    assert max_observed == _MAX_CASCADE_DEPTH, (
        f"cascade terminated at depth {max_observed} instead of "
        f"{_MAX_CASCADE_DEPTH} — the cap fired one or more hops early "
        f"(e.g. a lowered max_cascade_depth or an extra increment site)"
    )
