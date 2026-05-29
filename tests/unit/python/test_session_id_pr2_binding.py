"""ISSUE-0081 PR 2 — gRPC session propagation + ``on_event`` scope binding.

PR 1 (``test_session_id_contextvars.py``) landed the task-local
:class:`contextvars.ContextVar` primitive and made the default recall +
facade-write paths resolve the active session at call time.  But nothing
yet *enters* a ``session_scope`` on the inbound path, so a persona process
fielding two concurrent conversations still resolves both to the same
construction-time snapshot — the bleed PR 1 only armed the fix for.

PR 2 closes the loop on the Python side:

* :class:`TestSessionFromMetadata` — the pure
  ``_session_from_metadata`` helper that lifts the ``persatrix-session``
  gRPC metadata header (case-insensitive, empty-skipping) off
  ``context.invocation_metadata()``.
* :class:`TestOnEventBindsScope` — ``_LLMPersonaAgent.on_event`` enters a
  ``session_scope`` for the duration of ``_on_event_inner`` when the event
  carries the session metadata key, so recall + writes inside the handler
  see the per-request scope.  ``on_event`` is the universal funnel both
  inbound paths (sync ``SendChatMessage`` dispatch and fire-and-forget
  ``ReceiveChannelMessage`` EventLoop drain) flow through, so binding here
  covers both without touching the gRPC interceptor.
* :class:`TestWriteSeamHonoursScope` — the episode write seam
  (``_store_event_episode``) tags the row with the *active* scope, not the
  construction snapshot, so conversation A's writes land under A.
* :class:`TestClosedInteractionUsesCapturedSession` — the close path uses
  the session captured **when the interaction opened** (``interaction
  .session_id``), not whatever scope happens to be bound when the janitor
  flushes it.  This is the sibling-mislabel guard: ``idle_check`` can flush
  conversation B's stale interaction while conversation A's event holds the
  scope, and B's row must still be tagged B.
* :class:`TestInteractionTrackerCapturesSession` — the tracker freezes the
  session at interaction-open time and ignores it on subsequent turns.
* :class:`TestNoteToolHonoursScope` — the ``store_note`` builtin resolves
  the active scope at call time, overriding its tool-construction snapshot.

The whole file is RED until the PR 2 implementation lands (the
``EVENT_SESSION_METADATA_KEY`` / ``SESSION_METADATA_GRPC_KEY`` constants and
``_session_from_metadata`` do not exist yet, so import fails at collection).
"""

from __future__ import annotations

import pytest

from agents.memory.boundary_detectors import REASON_STRUCTURAL
from agents.memory.episodic import EpisodicMemory
from agents.memory.interactions import InteractionTracker
from agents.persona import create_persona_agent
from agents.persona_types import AgentEvent, EventType
from agents.session_id import (
    EVENT_SESSION_METADATA_KEY,
    LEGACY_SESSION_ID,
    SESSION_ID_ENV_VAR,
    SESSION_METADATA_GRPC_KEY,
    current_session_id,
    session_scope,
)
from agents.session_metadata import _session_from_metadata
from agents.tools.builtin import create_memory_tools
from agents.tools.permissions import PermissionGate

from ._persona_test_helpers import _PERSONA_CONFIG, _make_client

# ─── Group A — the pure metadata-lifting helper ─────────────


class TestSessionFromMetadata:
    def test_wire_key_constant_is_stable(self) -> None:
        # The orchestrator (Go) emits this exact header; pin the spelling
        # so a rename here is a conscious cross-language break, not a
        # silent drift that makes every inbound request fall back to the
        # construction snapshot.
        assert SESSION_METADATA_GRPC_KEY == "persatrix-session"

    def test_returns_value_when_present(self) -> None:
        meta = [("persatrix-session", "conv-x")]
        assert _session_from_metadata(meta) == "conv-x"

    def test_returns_none_when_absent(self) -> None:
        meta = [("user-agent", "grpc-go"), ("content-type", "application/grpc")]
        assert _session_from_metadata(meta) is None

    def test_empty_iterable_returns_none(self) -> None:
        assert _session_from_metadata([]) is None

    def test_none_metadata_returns_none(self) -> None:
        # ``context.invocation_metadata()`` is always iterable in practice,
        # but a defensive ``None`` guard keeps the helper total.
        assert _session_from_metadata(None) is None

    def test_empty_value_is_skipped(self) -> None:
        # A blank header must not bind a blank scope (which would collapse
        # to ``legacy`` and re-merge conversations); treat it as absent.
        assert _session_from_metadata([("persatrix-session", "")]) is None

    def test_key_match_is_case_insensitive(self) -> None:
        # HTTP/2 lower-cases header names, but a proxy or test harness may
        # present mixed case; match defensively.
        meta = [("Persatrix-Session", "conv-c")]
        assert _session_from_metadata(meta) == "conv-c"

    def test_first_matching_value_wins(self) -> None:
        meta = [("persatrix-session", "conv-first"), ("persatrix-session", "conv-second")]
        assert _session_from_metadata(meta) == "conv-first"

    def test_empty_value_then_nonempty_match_wins(self) -> None:
        # A blank first value must not short-circuit to ``None``: scanning
        # continues so a later non-empty header for the same key still binds.
        meta = [("persatrix-session", ""), ("persatrix-session", "conv-late")]
        assert _session_from_metadata(meta) == "conv-late"

    def test_bytes_value_is_skipped(self) -> None:
        # ``persatrix-session`` is a non-binary header (no ``-bin`` suffix), so
        # gRPC always delivers a ``str``.  A ``bytes`` value is anomalous and is
        # treated as absent (→ fall back to the construction snapshot) rather
        # than guessing an encoding.
        assert _session_from_metadata([("persatrix-session", b"conv-x")]) is None

    def test_event_envelope_key_is_namespaced(self) -> None:
        # The in-process envelope key rides the shared ``event.metadata`` dict
        # alongside generic keys (``chat_session_id`` is a *different* concept —
        # the CLI chat session, not the RFC 0031 operator namespace).  A
        # namespaced value avoids colliding with a future bare ``session_id``
        # and stays distinct from the wire header so the two evolve apart.
        assert EVENT_SESSION_METADATA_KEY == "persatrix_session"
        assert EVENT_SESSION_METADATA_KEY != SESSION_METADATA_GRPC_KEY


# ─── Group B — on_event binds the scope for the handler ─────


def _agent(monkeypatch: pytest.MonkeyPatch, *, session_env: str | None = None):
    if session_env is None:
        monkeypatch.delenv(SESSION_ID_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, session_env)
    return create_persona_agent(
        agent_id="ember-owl",
        config=_PERSONA_CONFIG,
        llm_client=_make_client(),
    )


class TestOnEventBindsScope:
    async def test_event_metadata_binds_session_scope(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``_on_event_inner`` runs inside the ``asyncio.wait_for`` child
        # task; the scope set around ``wait_for`` is copied into that task
        # at creation, so the handler sees the per-request session.
        agent = _agent(monkeypatch, session_env="run-a")
        captured: dict[str, str | None] = {}

        async def fake_inner(event: AgentEvent):
            captured["sid"] = current_session_id()
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            metadata={EVENT_SESSION_METADATA_KEY: "conv-x"},
        )
        await agent.on_event(event)

        assert captured.get("sid") == "conv-x"

    async def test_no_metadata_leaves_scope_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Without the session metadata key the handler runs with no active
        # scope, so call-time resolution falls back to the construction
        # snapshot exactly as before PR 2 (single-session / CLI path).
        agent = _agent(monkeypatch, session_env="run-a")
        captured: dict[str, str | None] = {}

        async def fake_inner(event: AgentEvent):
            captured["sid"] = current_session_id()
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        await agent.on_event(AgentEvent(event_type=EventType.TICK))

        assert captured.get("sid") is None

    async def test_scope_does_not_leak_after_on_event(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The scope is restored on block exit, so it never bleeds into a
        # sibling task that resumes on the same event loop afterwards.
        agent = _agent(monkeypatch, session_env="run-a")

        async def fake_inner(event: AgentEvent):
            return []

        agent._on_event_inner = fake_inner  # type: ignore[method-assign]

        event = AgentEvent(
            event_type=EventType.CHANNEL_MESSAGE,
            metadata={EVENT_SESSION_METADATA_KEY: "conv-x"},
        )
        await agent.on_event(event)
        assert current_session_id() is None


# ─── Group C — the episode write seam honours the scope ─────


async def _spy_store_episode(agent) -> list[dict]:
    """Replace ``store_episode`` with a kwargs-capturing pass-through."""
    captured: list[dict] = []
    original = agent._episodic_memory.store_episode

    async def spy(*args, **kwargs):
        captured.append(kwargs.copy())
        return await original(*args, **kwargs)

    agent._episodic_memory.store_episode = spy  # type: ignore[method-assign]
    return captured


class TestWriteSeamHonoursScope:
    async def test_single_turn_write_uses_active_scope(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Construction snapshot is ``run-a``; under an active scope the
        # row must be tagged with the scope, not the snapshot — otherwise
        # the always-on ``legacy``/snapshot carve-out re-merges every
        # conversation that shares this process.
        agent = _agent(monkeypatch, session_env="run-a")
        await agent.initialize_memory()
        try:
            captured = await _spy_store_episode(agent)
            with session_scope("conv-live"):
                await agent._store_event_episode(
                    event=AgentEvent(event_type=EventType.TICK), actions=[],
                )
            assert captured, "expected store_episode to be called"
            assert all(c.get("session_id") == "conv-live" for c in captured), (
                f"writes under an active scope must be tagged with it; "
                f"got: {captured!r}"
            )
        finally:
            await agent.close_memory()

    async def test_no_scope_falls_back_to_snapshot(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Regression: with no scope active the write seam behaves exactly
        # as PR 1 left it — the construction snapshot.
        agent = _agent(monkeypatch, session_env="run-a")
        await agent.initialize_memory()
        try:
            captured = await _spy_store_episode(agent)
            await agent._store_event_episode(
                event=AgentEvent(event_type=EventType.TICK), actions=[],
            )
            assert captured
            assert all(c.get("session_id") == "run-a" for c in captured)
        finally:
            await agent.close_memory()


# ─── Group D — the close path uses the captured session ─────


class TestClosedInteractionUsesCapturedSession:
    async def test_persist_uses_interaction_session_not_active_scope(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The sibling-mislabel guard.  An interaction opened under
        # conversation B is flushed while conversation A's event holds the
        # scope (``idle_check`` runs cross-scope at the top of every
        # event).  The persisted row must carry B's session — the one
        # frozen when the interaction opened — not A's bound scope.
        agent = _agent(monkeypatch, session_env="run-a")
        await agent.initialize_memory()
        try:
            tracker = agent._interaction_tracker
            tracker.add_turn(
                "dm:user-b", payload={"summary": "from conv-b"},
                session_id="conv-b",
            )
            closed = tracker.close("dm:user-b", reason=REASON_STRUCTURAL)
            assert closed is not None
            assert closed.session_id == "conv-b"

            captured = await _spy_store_episode(agent)
            with session_scope("conv-a"):
                await agent._persist_closed_interaction(closed)
            await agent.drain_pending_summaries()

            assert captured, "expected the closing-row insert"
            assert all(c.get("session_id") == "conv-b" for c in captured), (
                f"closed-interaction rows must use the session captured at "
                f"open (conv-b), not the bound scope (conv-a); got: {captured!r}"
            )
        finally:
            await agent.close_memory()


# ─── Group E — the tracker freezes session at open ──────────


class TestInteractionTrackerCapturesSession:
    def test_add_turn_captures_session_at_open(self) -> None:
        tracker = InteractionTracker()
        inter = tracker.add_turn("scope-x", session_id="conv-b")
        assert inter.session_id == "conv-b"

    def test_session_frozen_after_open(self) -> None:
        # A second turn in the same open scope must not overwrite the
        # session — the interaction's identity is fixed at open so a later
        # turn arriving under a different scope cannot relabel it.
        tracker = InteractionTracker()
        tracker.add_turn("scope-x", session_id="conv-b")
        inter = tracker.add_turn("scope-x", session_id="conv-c")
        assert inter.session_id == "conv-b"

    def test_default_session_is_legacy(self) -> None:
        tracker = InteractionTracker()
        inter = tracker.add_turn("scope-y")
        assert inter.session_id == LEGACY_SESSION_ID

    def test_start_captures_session(self) -> None:
        tracker = InteractionTracker()
        inter = tracker.start("scope-z", session_id="conv-d")
        assert inter.session_id == "conv-d"


# ─── Group F — the note tool resolves the scope at call time ─


async def _memory(monkeypatch: pytest.MonkeyPatch, env: str) -> EpisodicMemory:
    monkeypatch.setenv(SESSION_ID_ENV_VAR, env)
    mem = EpisodicMemory(agent_id="issue-0081", db_path=":memory:")
    await mem.initialize()
    return mem


def _store_note_tool(memory: EpisodicMemory):
    gate = PermissionGate({"memory": {"read": True, "write": True}})
    defs = create_memory_tools(memory, gate)
    return next(d for d in defs if d.name == "store_note")


class TestNoteToolHonoursScope:
    async def test_store_note_uses_active_scope(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The tool snapshots the session at construction (``run-a``); a
        # call made under an active scope must override that snapshot, the
        # same call-time precedence the episode/facade write paths use.
        mem = await _memory(monkeypatch, "run-a")
        try:
            store_note = _store_note_tool(mem)  # constructed with no scope
            captured: list[dict] = []
            original = mem.store_note

            async def spy(*args, **kwargs):
                captured.append(kwargs.copy())
                return await original(*args, **kwargs)

            mem.store_note = spy  # type: ignore[method-assign]

            with session_scope("conv-live"):
                await store_note.func(topic="t", content="c")

            assert captured, "expected store_note to be called"
            assert captured[0].get("session_id") == "conv-live"
        finally:
            await mem.close()

    async def test_store_note_without_scope_uses_snapshot(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Regression: no scope → the construction snapshot, preserving the
        # RFC 0031 Phase 2 PR 1 tool contract.
        mem = await _memory(monkeypatch, "run-a")
        try:
            store_note = _store_note_tool(mem)
            captured: list[dict] = []
            original = mem.store_note

            async def spy(*args, **kwargs):
                captured.append(kwargs.copy())
                return await original(*args, **kwargs)

            mem.store_note = spy  # type: ignore[method-assign]

            await store_note.func(topic="t", content="c")

            assert captured
            assert captured[0].get("session_id") == "run-a"
        finally:
            await mem.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
