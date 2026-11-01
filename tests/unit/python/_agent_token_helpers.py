"""A loopback orchestrator for the tests of the agent's token client.

Nothing on the real orchestrator checks a token until RFC 0009 Phases 3–4
plan PR 8, so this one checks it the way PR 8 is told to: a 401 whose ``code``
names the refusal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from agents.base import BaseAgent, TaskInput, TaskOutput, TaskStatus
from agents.orchestrator_session import (
    AGENT_ID_HEADER,
    AGENT_TOKEN_HEADER,
    ENROLLMENT_SECRET_HEADER,
    TOKEN_INVALID,
    TOKEN_REVOKED,
)
from agents.server import AgentServer

REGISTER = "/api/v1/agents/register"
PUBLISH = "/api/v1/channels/group:x/messages"
AGENT = "ember-owl"


class StubAgent(BaseAgent):
    async def handle(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.COMPLETED, result="ok")


@dataclass
class Call:
    method: str
    path: str
    token: str | None
    agent_id: str | None
    secret: str | None
    body: bytes


class FakeOrchestrator:
    """A loopback orchestrator that issues and checks tokens as the real one does.

    A new token retires the one before it, read back as revoked (F-12), and
    ``restart`` makes every token it issued read as invalid, as a new key does.
    """

    def __init__(self) -> None:
        self.url = ""
        self.calls: list[Call] = []
        self.issued: list[str] = []
        self.refusals: dict[str, str] = {}
        self.registration_status = 201
        self.registration_gate: asyncio.Event | None = None
        self.lose_next_answer = False
        self.answer_with_token = True
        self.redirect_to = ""
        self.publish_answer: bytes | None = None

    def to(self, path: str, method: str = "POST") -> list[Call]:
        return [c for c in self.calls if c.path == path and c.method == method]

    @property
    def registrations(self) -> int:
        return len(self.to(REGISTER))

    def restart(self) -> None:
        self.refusals.update(dict.fromkeys(self.issued, TOKEN_INVALID))

    def refuse_current(self, code: str) -> None:
        self.refusals[self.issued[-1]] = code

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.calls.append(Call(
            request.method,
            request.path,
            request.headers.get(AGENT_TOKEN_HEADER),
            request.headers.get(AGENT_ID_HEADER),
            request.headers.get(ENROLLMENT_SECRET_HEADER),
            await request.read(),
        ))
        if self.redirect_to:
            raise web.HTTPTemporaryRedirect(self.redirect_to)
        if request.path == REGISTER:
            return await self._register(request)
        if self.publish_answer is not None:
            return web.Response(body=self.publish_answer, status=401)
        code = self.refusals.get(request.headers.get(AGENT_TOKEN_HEADER, ""))
        if code:
            return web.json_response({"error": "refused", "code": code}, status=401)
        return web.json_response({"ok": True}, status=201)

    async def _register(self, request: web.Request) -> web.StreamResponse:
        if self.registration_gate is not None:
            await self.registration_gate.wait()
        if self.registration_status != 201:
            return web.json_response(
                {"error": "no", "code": "UNAUTHORIZED"}, status=self.registration_status,
            )
        agent_id = (await request.json())["id"]
        answer: dict[str, Any] = {"id": agent_id}
        if self.answer_with_token:
            if agent_id == AGENT:
                self.refusals.update(dict.fromkeys(self.issued, TOKEN_REVOKED))
            token = f"agt1.fake.{len(self.issued) + 1}"
            self.issued.append(token)
            answer["agent_token"] = token
            answer["agent_token_expires_at"] = "2026-11-02T00:00:00Z"
        if self.lose_next_answer:
            self.lose_next_answer = False
            assert request.transport is not None
            request.transport.abort()
            return web.Response()
        return web.json_response(answer, status=201)


async def serve(handler: Any) -> tuple[str, web.AppRunner]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return f"http://127.0.0.1:{port}", runner


@asynccontextmanager
async def running_orchestrator() -> AsyncIterator[FakeOrchestrator]:
    fake = FakeOrchestrator()
    fake.url, runner = await serve(fake.handle)
    try:
        yield fake
    finally:
        await runner.cleanup()


def make_server(url: str) -> AgentServer:
    server = AgentServer(host="127.0.0.1", port=0, shutdown_grace=1, orchestrator_url=url)
    server.register_agent(StubAgent(agent_id=AGENT, config={"capabilities": []}))
    return server


@asynccontextmanager
async def started_server(orchestrator: FakeOrchestrator) -> AsyncIterator[AgentServer]:
    started = make_server(orchestrator.url)
    await started.start()
    try:
        yield started
    finally:
        await started.stop()


async def publish(server: AgentServer, orchestrator: FakeOrchestrator) -> int:
    assert server._session is not None
    async with server._session.post(
        f"{orchestrator.url}{PUBLISH}", json={"content": "hi"},
    ) as resp:
        await resp.read()
        return resp.status
