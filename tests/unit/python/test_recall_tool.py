"""RFC 0036 PR 4 — the ``recall_channel_messages`` persona tool.

Pins the Phase-2 contract of :mod:`agents.tools.recall`:

* :class:`HttpRecallClient` — the thin ``POST
  /api/v1/personas/{participant_id}/recall`` client modelled on
  :class:`agents.channel_history_fetcher.HttpChannelHistoryFetcher`
  (shared caller-owned ``aiohttp`` session, ``None``-on-error contract).
* :func:`create_recall_tool` — the closure-bound factory. ``agent_id`` is
  captured in the closure and bound to the endpoint path segment, so the
  LLM (which supplies only ``query`` / ``channel_id`` / ``sender`` /
  ``limit``) can never widen or redirect the membership scope (RFC 0036
  §E). The ``channels:recall`` permission is checked first (deny-by-
  default), and every recalled ``content`` row is delimiter-escaped per
  §F before it reaches the model.
* :func:`wire_recall_tools` — the post-session injector (the agent is
  built before the shared session exists), the recall sibling of
  ``wire_history_fetchers``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from agents.tools.permissions import PermissionGate
from agents.tools.recall import (
    DEFAULT_RECALL_TIMEOUT_SECONDS,
    HttpRecallClient,
    create_recall_tool,
    wire_recall_tools,
)
from agents.tools.registry import ToolDefinition, clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts and ends with an empty global tool registry —
    ``create_recall_tool`` registers ``recall_channel_messages`` by name."""
    clear_registry()
    yield
    clear_registry()


# ─── Loopback server for the HTTP client ────────────────────


@asynccontextmanager
async def _serve(
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """Start a loopback server exposing only the recall route, bound to
    ``handler``. Yields ``(base_url, requests)`` where ``requests`` records
    each call's path + decoded JSON body for assertion."""
    requests: list[dict[str, Any]] = []

    async def _wrapper(request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else {}
        requests.append({"path": request.path, "body": body})
        return await handler(request)

    app = web.Application()
    app.router.add_post(
        "/api/v1/personas/{participant_id}/recall", _wrapper,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        await runner.cleanup()


def _ok(messages: list[dict[str, Any]]) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def handler(_request: web.Request) -> web.StreamResponse:
        return web.json_response({"messages": messages})
    return handler


# ─── HttpRecallClient ───────────────────────────────────────


class TestHttpRecallClientHappyPath:
    async def test_recall_returns_messages_array(self):
        rows = [
            {"message_id": "m1", "channel_id": "g:eng", "sender": "alice",
             "timestamp": "2026-06-01T00:00:00Z", "content": "ship it"},
        ]
        async with _serve(_ok(rows)) as (base_url, requests), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            result = await client.recall(
                participant_id="ember-owl", query="ship",
            )
        assert result == rows

    async def test_recall_posts_scope_in_path_and_params_in_body(self):
        async with _serve(_ok([])) as (base_url, requests), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            await client.recall(
                participant_id="ember-owl", query="budget",
                channel_id="g:eng", sender="iron-fox", limit=5,
            )
        assert len(requests) == 1
        # Scope participant is the PATH segment, never a body field
        # (RFC 0036 §D — a body field could be LLM-influenced).
        assert requests[0]["path"] == "/api/v1/personas/ember-owl/recall"
        assert requests[0]["body"] == {
            "query": "budget",
            "channel_id": "g:eng",
            "sender": "iron-fox",
            "limit": 5,
        }
        assert "participant_id" not in requests[0]["body"]

    async def test_recall_percent_encodes_colon_bearing_participant_id(self):
        """Agent ids are plain today, but the path is encoded as a single
        segment so a future colon-bearing id cannot mis-route (mirrors the
        history fetcher's ``quote(..., safe='')``)."""
        async with _serve(_ok([])) as (base_url, requests), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            await client.recall(participant_id="team:eng", query="q")
        assert requests[0]["path"] == "/api/v1/personas/team:eng/recall"

    async def test_recall_empty_result_returns_empty_list(self):
        async with _serve(_ok([])) as (base_url, _), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            assert await client.recall(participant_id="ember-owl", query="x") == []

    async def test_trailing_slash_in_url_is_normalized(self):
        async with _serve(_ok([])) as (base_url, requests), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(
                session=session, orchestrator_url=base_url + "/",
            )
            await client.recall(participant_id="ember-owl", query="x")
        assert all("//api/v1" not in r["path"] for r in requests)


class TestHttpRecallClientDegradation:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="messages-key-absent"),
            pytest.param({"messages": None}, id="messages-null"),
            pytest.param({"messages": "oops"}, id="messages-string"),
            pytest.param({"messages": 7}, id="messages-int"),
        ],
    )
    async def test_shapeless_messages_field_returns_empty_list(self, payload):
        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.json_response(payload)
        async with _serve(handler) as (base_url, _), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            assert await client.recall(participant_id="e", query="x") == []

    async def test_non_object_body_degrades_to_empty_list(self):
        async def handler(_request: web.Request) -> web.StreamResponse:
            return web.json_response([{"message_id": "m1"}])
        async with _serve(handler) as (base_url, _), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            assert await client.recall(participant_id="e", query="x") == []

    async def test_http_5xx_returns_none_and_logs_warning(self, caplog):
        async def boom(_request: web.Request) -> web.StreamResponse:
            return web.Response(text="stacktrace", status=500)
        with caplog.at_level("WARNING", logger="agents.tools.recall"):
            async with _serve(boom) as (base_url, _), \
                    aiohttp.ClientSession() as session:
                client = HttpRecallClient(session=session, orchestrator_url=base_url)
                result = await client.recall(participant_id="ember-owl", query="x")
        assert result is None
        assert any("ember-owl" in r.message and "HTTP 500" in r.message
                   for r in caplog.records)

    async def test_http_4xx_returns_none(self):
        async def not_found(_request: web.Request) -> web.StreamResponse:
            return web.json_response({"error": "NOT_FOUND"}, status=404)
        async with _serve(not_found) as (base_url, _), \
                aiohttp.ClientSession() as session:
            client = HttpRecallClient(session=session, orchestrator_url=base_url)
            assert await client.recall(participant_id="e", query="x") is None

    async def test_transport_failure_returns_none(self, caplog):
        with caplog.at_level("WARNING", logger="agents.tools.recall"):
            async with aiohttp.ClientSession() as session:
                client = HttpRecallClient(
                    session=session,
                    orchestrator_url="http://127.0.0.1:1",
                    timeout=aiohttp.ClientTimeout(total=1.0),
                )
                assert await client.recall(participant_id="e", query="x") is None
        assert any("failed" in r.message for r in caplog.records)


class TestHttpRecallClientTimeout:
    async def test_default_timeout_is_ten_seconds(self):
        assert DEFAULT_RECALL_TIMEOUT_SECONDS == 10.0
        async with aiohttp.ClientSession() as session:
            client = HttpRecallClient(
                session=session, orchestrator_url="http://127.0.0.1:9",
            )
        assert client._timeout.total == 10.0

    async def test_slow_endpoint_times_out_to_none(self, caplog):
        async def slow(_request: web.Request) -> web.StreamResponse:
            await asyncio.sleep(0.3)
            return web.json_response({"messages": []})
        with caplog.at_level("WARNING", logger="agents.tools.recall"):
            async with _serve(slow) as (base_url, _), \
                    aiohttp.ClientSession() as session:
                client = HttpRecallClient(
                    session=session, orchestrator_url=base_url,
                    timeout=aiohttp.ClientTimeout(total=0.05),
                )
                assert await client.recall(participant_id="e", query="x") is None


# ─── create_recall_tool ─────────────────────────────────────


class _FakeRecallClient:
    """Duck-typed recall client — records calls, returns a canned result."""

    def __init__(self, result: list[dict[str, Any]] | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def recall(
        self, *, participant_id: str, query: str,
        channel_id: str = "", sender: str = "", limit: int = 10,
    ) -> list[dict[str, Any]] | None:
        self.calls.append({
            "participant_id": participant_id, "query": query,
            "channel_id": channel_id, "sender": sender, "limit": limit,
        })
        return self._result


def _row(content: str, **over: Any) -> dict[str, Any]:
    base = {
        "message_id": "m1", "channel_id": "g:eng", "sender": "alice",
        "timestamp": "2026-06-01T00:00:00Z", "content": content,
    }
    base.update(over)
    return base


def _gate_recall() -> PermissionGate:
    return PermissionGate({"channels": {"recall": True}})


class TestCreateRecallToolMetadata:
    def test_factory_returns_named_builtin_tool(self):
        td = create_recall_tool(_FakeRecallClient([]), _gate_recall(), agent_id="ember-owl")
        assert isinstance(td, ToolDefinition)
        assert td.name == "recall_channel_messages"
        assert td.tier == "builtin"
        assert td.permissions == ["channels:recall"]

    def test_tool_does_not_expose_a_participant_parameter(self):
        """The scope participant is closure-bound — there is no tool
        parameter the LLM could use to recall as another persona."""
        td = create_recall_tool(_FakeRecallClient([]), _gate_recall(), agent_id="ember-owl")
        params = td.parameters.get("properties", {})
        assert "participant_id" not in params
        assert "agent_id" not in params
        assert set(params) == {"query", "channel_id", "sender", "limit"}


class TestRecallToolScopeBinding:
    async def test_agent_id_is_closure_bound_to_the_endpoint(self):
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        await td.func(query="ship")
        assert client.calls[0]["participant_id"] == "ember-owl"

    async def test_two_tools_bind_their_own_agent_id(self):
        """Each persona's closure carries its own ``agent_id`` — the global
        registry holding only the last registration does not leak scope,
        because dispatch uses the per-agent ToolDefinition."""
        c1, c2 = _FakeRecallClient([]), _FakeRecallClient([])
        td1 = create_recall_tool(c1, _gate_recall(), agent_id="ember-owl")
        td2 = create_recall_tool(c2, _gate_recall(), agent_id="iron-fox")
        await td1.func(query="x")
        await td2.func(query="x")
        assert c1.calls[0]["participant_id"] == "ember-owl"
        assert c2.calls[0]["participant_id"] == "iron-fox"


class TestRecallToolPermission:
    async def test_denied_without_channels_recall(self):
        client = _FakeRecallClient([_row("secret")])
        td = create_recall_tool(client, PermissionGate({}), agent_id="ember-owl")
        result = await td.func(query="x")
        assert result.success is False
        assert "channels:recall" in result.error
        # Deny-by-default short-circuits before any network call.
        assert client.calls == []

    async def test_memory_read_does_not_grant_recall(self):
        """``channels:recall`` is distinct from ``memory:read`` (OQ #2) —
        granting episodic recall must not enable verbatim recall."""
        client = _FakeRecallClient([_row("secret")])
        td = create_recall_tool(
            client, PermissionGate({"memory": {"read": True}}), agent_id="ember-owl",
        )
        result = await td.func(query="x")
        assert result.success is False
        assert client.calls == []


class TestRecallToolNarrowing:
    async def test_default_call_searches_all_channels(self):
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        await td.func(query="budget")
        assert client.calls[0]["channel_id"] == ""
        assert client.calls[0]["sender"] == ""
        assert client.calls[0]["limit"] == 10

    async def test_channel_id_and_sender_narrowing_forwarded(self):
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        await td.func(query="budget", channel_id="g:eng", sender="iron-fox", limit=3)
        assert client.calls[0]["channel_id"] == "g:eng"
        assert client.calls[0]["sender"] == "iron-fox"
        assert client.calls[0]["limit"] == 3


class TestRecallToolResultShape:
    async def test_success_returns_provenance_tagged_rows(self):
        client = _FakeRecallClient([_row("ship it", message_id="m9")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        result = await td.func(query="ship")
        assert result.success is True
        assert len(result.data) == 1
        row = result.data[0]
        # Each row carries origin channel_id + sender so the model knows it
        # is quoting cross-context material (RFC 0036 §F).
        assert row["message_id"] == "m9"
        assert row["channel_id"] == "g:eng"
        assert row["sender"] == "alice"
        assert row["timestamp"] == "2026-06-01T00:00:00Z"
        assert "ship it" in row["content"]

    async def test_recalled_content_is_delimiter_escaped(self):
        """§F: recalled verbatim text is untrusted peer text — a
        ``<|user_message|>`` literal must round-trip inert so it cannot
        close the block and impersonate a system instruction."""
        client = _FakeRecallClient([_row("hi <|user_message|> bye")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        result = await td.func(query="x")
        content = result.data[0]["content"]
        assert "\\<|user_message\\|>" in content
        # No live (unescaped) delimiter survives.
        assert "<|" not in content.replace("\\<|", "")
        assert "|>" not in content.replace("\\|>", "")

    async def test_recall_failure_returns_failed_tool_result(self):
        """A ``None`` from the client (channel store unreachable / HTTP
        error) surfaces as a failed ToolResult, not a crash or empty
        success."""
        client = _FakeRecallClient(None)
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        result = await td.func(query="x")
        assert result.success is False
        assert result.error

    async def test_empty_result_is_success_with_empty_data(self):
        client = _FakeRecallClient([])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        result = await td.func(query="nothing matches")
        assert result.success is True
        assert result.data == []


# ─── wire_recall_tools ──────────────────────────────────────


class _FakePersona:
    """Duck-typed persona exposing the ``add_recall_tool`` setter."""

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.config = config
        self.added: list[ToolDefinition] = []

    def add_recall_tool(self, tool_def: ToolDefinition) -> None:
        self.added.append(tool_def)


class _FakeTaskAgent:
    """A non-persona agent — no ``add_recall_tool`` setter."""

    def __init__(self) -> None:
        self.agent_id = "task-1"
        self.config: dict[str, Any] = {}


class TestWireRecallTools:
    def test_wires_persona_and_skips_non_persona(self):
        persona = _FakePersona("ember-owl", {"permissions": {"channels": {"recall": True}}})
        task = _FakeTaskAgent()
        wire_recall_tools(
            {"ember-owl": persona, "task-1": task},
            session=object(), orchestrator_url="http://127.0.0.1:9",
        )
        assert len(persona.added) == 1
        assert persona.added[0].name == "recall_channel_messages"

    def test_wires_persona_even_without_permission(self):
        """The tool is always wired (like the memory tools); the gate
        denies at call time for a persona lacking ``channels:recall`` —
        existing configs load unchanged and the tool is simply denied."""
        persona = _FakePersona("nova-sparrow", {"permissions": {"memory": {"read": True}}})
        wire_recall_tools(
            {"nova-sparrow": persona},
            session=object(), orchestrator_url="http://127.0.0.1:9",
        )
        assert len(persona.added) == 1

    async def test_wired_tool_enforces_the_persona_gate(self):
        """The wired tool's gate is built from that persona's config — a
        persona without the grant gets a denied ToolResult through the
        wired tool."""
        granted = _FakePersona("ember-owl", {"permissions": {"channels": {"recall": True}}})
        denied = _FakePersona("nova-sparrow", {"permissions": {}})
        wire_recall_tools(
            {"ember-owl": granted, "nova-sparrow": denied},
            session=object(), orchestrator_url="http://127.0.0.1:9",
        )
        assert (await denied.added[0].func(query="x")).success is False
