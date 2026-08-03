"""ISSUE-0118 (v0.3.13 PR 1) — the recall tool's foreign-epoch wall.

The channel store is single-epoch by the ISSUE-0106 direction-(b) decision
(separate runs never share a store DB; the recall endpoint 400s an
``epoch_id`` override), so a turn delivered under a per-request epoch that
is not the process's world cannot be scoped server-side — the tool must
decline instead of reading the live world's verbatim history.  This is
the tool side door the v0.3.12 fresh-epoch MT leg leaked through: with
injection correctly admitting zero under ``--epoch``, the model's recall
round surfaced the live conversation anyway (the F-3 leak class,
2026-07-30).

Split from :mod:`test_recall_tool` at the 500-line code cap; like
:mod:`test_recall_tool_classification`, this module deliberately declares
its own minimal fakes rather than importing from a sibling test module.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from agents.epoch_id import epoch_scope, resolve_epoch_id_silent, resolve_world_epoch_id
from agents.session_id import session_scope
from agents.tools.permissions import PermissionGate
from agents.tools.recall import create_recall_tool
from agents.tools.registry import clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


class _FakeRecallClient:
    """Duck-typed recall client — records calls, returns a canned result."""

    def __init__(self, result: list[dict[str, Any]] | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result

    async def recall(
        self, *, participant_id: str, acting_classification: str, query: str,
        channel_id: str = "", sender: str = "", limit: int = 10,
    ) -> list[dict[str, Any]] | None:
        self.calls.append({"participant_id": participant_id, "query": query})
        return self._result


def _row(content: str) -> dict[str, Any]:
    return {
        "message_id": "m1", "channel_id": "g:eng", "sender": "alice",
        "timestamp": "2026-06-01T00:00:00Z", "content": content,
    }


def _gate_recall() -> PermissionGate:
    return PermissionGate({"channels": {"recall": True}})


class TestRecallToolForeignEpochWall:
    async def test_foreign_epoch_scope_declines_without_network_call(self):
        client = _FakeRecallClient([_row("Atlas ships Friday")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        assert td.func is not None
        assert resolve_epoch_id_silent() != "mt-crossroom-fresh"
        with epoch_scope("mt-crossroom-fresh"):
            result = await td.func(query="atlas")
        # A fresh epoch must see NOTHING — and not learn that withheld
        # history exists, so the decline is an ordinary empty success.
        assert result.success is True
        assert result.data == []
        assert client.calls == []

    async def test_world_epoch_scope_recalls_normally(self):
        """A scope equal to the process's world (production ``live`` ==
        ``live``; a CI stack under one job epoch) is not foreign — the
        additive contract keeps recall working."""
        client = _FakeRecallClient([_row("Atlas ships Friday")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        assert td.func is not None
        with epoch_scope(resolve_epoch_id_silent()):
            result = await td.func(query="atlas")
        assert result.success is True
        assert len(client.calls) == 1

    async def test_no_scope_recalls_normally(self):
        client = _FakeRecallClient([_row("Atlas ships Friday")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        assert td.func is not None
        result = await td.func(query="atlas")
        assert result.success is True
        assert len(client.calls) == 1

    async def test_foreign_epoch_decline_logs_at_info(
        self, caplog: pytest.LogCaptureFixture,
    ):
        """The decline is the ONLY operator signal when production
        orchestrator and agent server disagree on ``PERSATRIX_EPOCH``
        (every recall silently empties), so it must be visible at default
        log levels — INFO, not DEBUG (PR #809 review finding 3).
        Server-side only: the model still sees an ordinary empty result,
        so quieting this back down must be a conscious trade."""
        client = _FakeRecallClient([_row("x")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        assert td.func is not None
        with caplog.at_level(logging.INFO, logger="agents.tools.recall"):
            with epoch_scope("mt-crossroom-fresh"):
                result = await td.func(query="x")
        assert result.success is True
        assert result.data == []
        assert any(
            record.levelno == logging.INFO and "declined" in record.getMessage()
            for record in caplog.records
        )

    async def test_wiring_under_a_scope_does_not_poison_world_snapshot(self):
        """The world snapshot is env-only (PR #809 review finding 2): a
        tool wired while some ``epoch_scope`` is active — a lazily wired
        persona, a scoped fixture — must still treat the process's env
        world as the world.  With the scope-first resolver this captured
        the wiring-time scope instead, declining the world's own recalls
        and admitting the wiring epoch's."""
        client = _FakeRecallClient([_row("Atlas ships Friday")])
        with epoch_scope("wiring-time-scope"):
            td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        assert td.func is not None
        with epoch_scope(resolve_world_epoch_id()):
            result = await td.func(query="atlas")
        assert result.success is True
        assert len(client.calls) == 1
        # …and the wiring-time scope stays genuinely foreign.
        client.calls.clear()
        with epoch_scope("wiring-time-scope"):
            walled = await td.func(query="atlas")
        assert walled.success is True
        assert walled.data == []
        assert client.calls == []

    async def test_session_scope_does_not_gate(self):
        """Session is room-continuity (carve-out by design, never strict
        isolation) and verbatim recall's access rule is membership — a
        bound session must not wall the tool."""
        client = _FakeRecallClient([_row("Atlas ships Friday")])
        td = create_recall_tool(client, _gate_recall(), agent_id="ember-owl")
        assert td.func is not None
        with session_scope("room-42"):
            result = await td.func(query="atlas")
        assert result.success is True
        assert len(client.calls) == 1

    async def test_permission_denial_still_wins_over_epoch_wall(self):
        """Deny-by-default stays the first gate — a foreign epoch does not
        convert a permission denial into a silent empty success."""
        client = _FakeRecallClient([_row("x")])
        td = create_recall_tool(client, PermissionGate({}), agent_id="ember-owl")
        assert td.func is not None
        with epoch_scope("mt-crossroom-fresh"):
            result = await td.func(query="x")
        assert result.success is False
        assert client.calls == []
