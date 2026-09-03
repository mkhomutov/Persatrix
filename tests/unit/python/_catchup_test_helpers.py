"""Shared fixtures and helpers for the on-startup catch-up fetch
test suites.

Extracted so the parent ``test_channel_catchup.py`` and the
PR-265-review follow-up file ``test_channel_catchup_followups.py``
can both ride the same loopback ``aiohttp`` orchestrator without
either file blowing past the 500-line review cap. The pattern mirrors
``_persona_test_helpers.py`` — a private module sibling to the test
files, imported by name. Pytest discovers the fixtures because the
test files re-export them (``from ._catchup_test_helpers import
orchestrator``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiohttp import web

from agents.persona_types import AgentEvent

__all__ = ["_msg", "_channel", "_SpyAgent", "orchestrator"]


def _msg(
    *,
    msg_id: str,
    channel_id: str,
    sender_id: str,
    content: str,
    ts: datetime | None = None,
    mentions: list[str] | None = None,
) -> dict:
    """Build a JSON message body in the wire shape from
    ``internal/server/channel_types.go::channelMessageResponse``."""
    return {
        "id": msg_id,
        "channel_id": channel_id,
        "sender_id": sender_id,
        "content": content,
        "timestamp": (ts or datetime(2026, 5, 7, 10, 0, tzinfo=UTC)).isoformat(),
        "mentions": list(mentions or []),
    }


def _channel(*, channel_id: str, name: str = "", channel_type: str = "group") -> dict:
    return {
        "id": channel_id,
        "name": name or channel_id.split(":", 1)[-1],
        "channel_type": channel_type,
        "description": "",
        "created_at": "2026-05-01T00:00:00+00:00",
    }


class _SpyAgent:
    """Records ``on_event`` invocations so the test can assert the
    AgentEvent shape (event_type, channel_id, sender_id, replay_mode)
    without booting the full persona runtime.

    Mirrors the surface ``replay_channel_history`` consumes: ``agent_id``,
    ``async on_event(event)`` and ``note_replay_gap`` (the ISSUE-0130 (b)
    hook the ingest-time derivation door reads — the real persona routes
    it into its ``InteractionTracker``). The integration with the real
    ``_LLMPersonaAgent`` is covered by
    ``tests/unit/python/test_replay_mode_action_loop.py``.
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.events: list[AgentEvent] = []
        self.replay_gaps: list[tuple[str, str]] = []

    async def on_event(self, event: AgentEvent):
        self.events.append(event)
        return []

    def note_replay_gap(self, channel_id: str, speaker_id: str) -> None:
        self.replay_gaps.append((channel_id, speaker_id))


@pytest.fixture
async def orchestrator():
    """Loopback orchestrator with the four endpoints the fetcher needs.

    Returns a dict the test can mutate to set up channels, members per
    channel, and history per channel; plus a ``log`` list capturing the
    URL of every request the fetcher issued (used to pin the call
    shape). The list endpoint logs ``path_qs`` (not ``path``) so tests
    can pin the explicit ``?limit=`` query string the fetcher MUST
    send (PR-265 review L1 first-pass: silent 50-channel cap
    regression).

    Asymmetry note (PR-265 review L8 second-pass): ``log`` records
    ``path_qs`` (path + query string) but ``fail_paths`` is matched
    against ``request.path`` (no query string). This is intentional,
    not a bug: ``log`` exists to *pin* fine-grained request shape
    (which ``?limit=`` value, which ``?cursor=``), while
    ``fail_paths`` is a coarse-grained "this endpoint is broken"
    knob that should fail every variant of the path without forcing
    a test to enumerate the query-string permutations. A future test
    that wants to fail only one query-string variant should add a
    second ``fail_query_strings: set[str]`` knob rather than
    flattening this asymmetry.
    """
    state: dict = {
        "channels": [],            # list of channel JSON
        "members": {},              # channel_id -> [member JSON]
        "history": {},              # channel_id -> [message JSON]
        "log": [],                  # request paths
        "fail_paths": set(),        # paths that return 500
    }

    async def list_channels(request: web.Request) -> web.Response:
        state["log"].append(request.path_qs)
        if request.path in state["fail_paths"]:
            return web.json_response({"error": "boom"}, status=500)
        return web.json_response({"channels": state["channels"]})

    async def get_channel(request: web.Request) -> web.Response:
        state["log"].append(request.path)
        if request.path in state["fail_paths"]:
            return web.json_response({"error": "boom"}, status=500)
        cid = request.match_info["id"]
        ch = next((c for c in state["channels"] if c["id"] == cid), None)
        if ch is None:
            return web.json_response({"error": "NOT_FOUND"}, status=404)
        out = dict(ch)
        out["members"] = state["members"].get(cid, [])
        return web.json_response(out)

    async def history(request: web.Request) -> web.Response:
        state["log"].append(request.path_qs)
        if request.path in state["fail_paths"]:
            return web.json_response({"error": "boom"}, status=500)
        cid = request.match_info["id"]
        return web.json_response({"messages": state["history"].get(cid, [])})

    app = web.Application()
    app.router.add_get("/api/v1/channels", list_channels)
    app.router.add_get("/api/v1/channels/{id}", get_channel)
    app.router.add_get("/api/v1/channels/{id}/messages", history)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        await runner.cleanup()
