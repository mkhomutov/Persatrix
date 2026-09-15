"""ISSUE-0158 — the ``recall_channel_messages`` tool carries the ACTING channel.

The RFC 0037 §F recall filter had the acting classification and nothing
about who is in the acting room, so on a group-room turn it returned a DM's
transcript the §D audience gate had just withheld from the same turn (found
live at the v0.3.16 release-prep arc). The tool now binds the acting channel
id from the turn — the same trusted seam as the acting classification, never
an LLM argument — and sends it only when the persona's
``memory.egress.audience`` resolves ``live``, so ``shadow`` and ``off`` stay
byte-identical to v0.3.15; the orchestrator applies the member-subset
condition (pinned Go-side in ``sqlite_search_audience_test.go``).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.acting_channel import acting_channel_scope, current_acting_channel_id
from agents.tools.permissions import PermissionGate
from agents.tools.recall import HttpRecallClient, create_recall_tool, wire_recall_tools
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _gate() -> PermissionGate:
    return PermissionGate({"channels": {"recall": True}})


class _FakeRecallClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def recall(
        self, *, participant_id: str, acting_classification: str, query: str,
        channel_id: str = "", sender: str = "", limit: int = 10,
        acting_channel_id: str = "",
    ) -> list[dict[str, Any]] | None:
        self.calls.append({
            "participant_id": participant_id, "acting_channel_id": acting_channel_id,
            "query": query,
        })
        return []


# ── the contextvar ──────────────────────────────────────────────────────────

def test_acting_channel_scope_binds_and_restores() -> None:
    assert current_acting_channel_id() is None
    with acting_channel_scope("group:planning"):
        assert current_acting_channel_id() == "group:planning"
        with acting_channel_scope("dm:alice:ember-owl"):
            assert current_acting_channel_id() == "dm:alice:ember-owl"
        assert current_acting_channel_id() == "group:planning"
    assert current_acting_channel_id() is None


def test_acting_channel_scope_ignores_a_blank_id() -> None:
    """A tick or a channel-less turn binds nothing, so the read stays None."""
    with acting_channel_scope(""):
        assert current_acting_channel_id() is None
    with acting_channel_scope(None):
        assert current_acting_channel_id() is None


def test_request_scope_binds_the_acting_channel_beside_the_other_axes() -> None:
    """The persona runtime enters one scope per event; the channel rides it."""
    from agents.request_scope import request_scope_from_metadata

    with request_scope_from_metadata({}, channel_id="group:planning"):
        assert current_acting_channel_id() == "group:planning"
    with request_scope_from_metadata({}):
        assert current_acting_channel_id() is None


# ── the tool ────────────────────────────────────────────────────────────────

async def test_tool_sends_the_acting_channel_when_live_and_bound() -> None:
    client = _FakeRecallClient()
    td = create_recall_tool(client, _gate(), agent_id="ember-owl", audience_live=True)
    func = td.func
    assert func is not None
    with acting_channel_scope("group:planning"):
        await func(query="helix")
    assert client.calls[0]["acting_channel_id"] == "group:planning"


async def test_tool_sends_nothing_when_the_knob_is_not_live() -> None:
    """``shadow`` is the documented rollback lever: byte-identical to v0.3.15,
    which means the recall read is unscoped exactly as it was."""
    client = _FakeRecallClient()
    td = create_recall_tool(client, _gate(), agent_id="ember-owl", audience_live=False)
    func = td.func
    assert func is not None
    with acting_channel_scope("group:planning"):
        await func(query="helix")
    assert client.calls[0]["acting_channel_id"] == ""


async def test_tool_sends_nothing_when_no_channel_is_bound() -> None:
    client = _FakeRecallClient()
    td = create_recall_tool(client, _gate(), agent_id="ember-owl", audience_live=True)
    func = td.func
    assert func is not None
    await func(query="helix")
    assert client.calls[0]["acting_channel_id"] == ""


async def test_tool_default_is_compatible_with_older_fakes() -> None:
    """A caller that never opted in gets the pre-0158 call shape — the
    existing test fakes accept no ``acting_channel_id`` keyword."""
    calls: list[dict[str, Any]] = []

    class _Old:
        async def recall(self, *, participant_id, acting_classification, query,
                         channel_id="", sender="", limit=10):
            calls.append({"query": query})
            return []

    td = create_recall_tool(cast(Any, _Old()), _gate(), agent_id="ember-owl")
    func = td.func
    assert func is not None
    with acting_channel_scope("group:planning"):
        await func(query="helix")
    assert calls == [{"query": "helix"}]


def test_acting_channel_is_not_an_llm_parameter() -> None:
    td = create_recall_tool(_FakeRecallClient(), _gate(), agent_id="ember-owl", audience_live=True)
    assert "acting_channel_id" not in td.parameters.get("properties", {})


# ── the client body ─────────────────────────────────────────────────────────

class _FakeResponse:
    status = 200

    async def json(self) -> dict[str, Any]:
        return {"messages": []}

    async def text(self) -> str:
        return ""

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: Any) -> _FakeResponse:
        self.bodies.append(json)
        return _FakeResponse()


async def test_client_body_carries_the_acting_channel_only_when_set() -> None:
    session = _FakeSession()
    client = HttpRecallClient(session=session, orchestrator_url="http://o")  # type: ignore[arg-type]
    await client.recall(participant_id="ember-owl", acting_classification="internal",
                        query="helix", acting_channel_id="group:planning")
    await client.recall(participant_id="ember-owl", acting_classification="internal",
                        query="helix")
    assert session.bodies[0]["acting_channel_id"] == "group:planning"
    assert "acting_channel_id" not in session.bodies[1]


# ── the wiring ──────────────────────────────────────────────────────────────

class _FakeAgent:
    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.config = config
        self.tools: list[Any] = []

    def add_recall_tool(self, td: Any) -> None:
        self.tools.append(td)


async def test_wiring_resolves_the_audience_knob_per_persona(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default is ``live`` → the acting channel rides; a persona
    pinned to ``shadow`` → it does not."""
    sent: dict[str, str] = {}

    async def fake_recall(self, *, participant_id, acting_classification, query,
                          channel_id="", sender="", limit=10, acting_channel_id=""):
        sent[participant_id] = acting_channel_id
        return []

    monkeypatch.setattr(HttpRecallClient, "recall", fake_recall)
    perms = {"permissions": {"channels": {"recall": True}}}
    live = _FakeAgent("ember-owl", {**perms})
    shadow = _FakeAgent("iron-fox", {**perms, "memory": {"egress": {"audience": "shadow"}}})
    fleet = cast(Any, {"ember-owl": live, "iron-fox": shadow})
    wire_recall_tools(fleet, session=cast(Any, object()), orchestrator_url="http://o")
    with acting_channel_scope("group:planning"):
        await live.tools[0].func(query="helix")
        await shadow.tools[0].func(query="helix")
    assert sent == {"ember-owl": "group:planning", "iron-fox": ""}
