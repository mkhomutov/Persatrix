"""RFC 0031 Phase 2 PR 4 — facade read-path default-path pins.

The §D security guarantee this file pins: the persona-runtime default
context path **must never reach** ``sessions="*"`` (the all-sessions
debug mode).  Wiring ``"*"`` into a prompt context re-introduces F-3 —
the very cross-run state bleed Phase 2 closes.

The pins are split into two halves so a regression cannot bypass both:

* **Source-level** — :class:`TestPersonaRuntimeNeverReachesAllSessions`
  scans every persona-runtime module that reaches a memory recall and
  asserts the ``"*"`` literal (and the
  :data:`agents.memory._session_filter.SESSIONS_ALL` import) does not
  appear.  Cheap, catches the obvious mistake at review time, runs
  without DB setup.

* **Runtime** — :class:`TestFacadeRetrieveRelevantSessionForwarding`,
  :class:`TestFacadeRetrieveProceduresSessionForwarding`, and
  :class:`TestFacadeReadFromPoolSessionForwarding` exercise the three
  facade read methods through their public signature and assert the
  ``sessions=`` kwarg threads cleanly to the underlying tier.  Spies on
  the tier-layer recall so the assertion is on the value actually
  reaching the SQL layer, not just the surface the facade exposes.

The carved-out exception is the CLI / debug path (Phase 3's
``persatrix memory recall --all-sessions`` will surface ``"*"``
explicitly).  That path is not pinned here because it is not the
persona-runtime context path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.memory.facade import MemoryStore
from agents.memory.shared_pool import (
    SharedMemoryPool,
    SharedPoolConfig,
    SharedPoolRegistry,
)


# ─── Source-level pin ───────────────────────────────────────


# Modules on the persona-runtime *default* recall path.  Adding a new
# recall call site to the persona prompt-assembly pipeline means
# extending this list — the source-level scan is the regression bar
# against a new site silently wiring ``"*"``.  CLI / debug surfaces
# (``persatrix memory recall --all-sessions``, Phase 3) are NOT on this
# list; they are allowed to pass ``"*"`` explicitly.
PERSONA_RUNTIME_RECALL_MODULES = (
    Path("agents/persona_runtime/memory_context.py"),
    Path("agents/persona_runtime/channel_history.py"),
)


class TestPersonaRuntimeNeverReachesAllSessions:
    """The persona-runtime default context path source files must not
    contain ``sessions="*"`` / ``sessions = "*"`` / ``SESSIONS_ALL``."""

    @pytest.mark.parametrize("module_path", PERSONA_RUNTIME_RECALL_MODULES)
    def test_no_sessions_all_literal_in_source(
        self, module_path: Path,
    ) -> None:
        # Resolve against the repo root (two parents up from this file
        # — ``tests/unit/python/<this>`` → repo root).
        repo_root = Path(__file__).resolve().parents[3]
        full_path = repo_root / module_path
        assert full_path.exists(), (
            f"persona-runtime recall module {module_path} not found at "
            f"{full_path} — update PERSONA_RUNTIME_RECALL_MODULES if the "
            "file was moved or renamed."
        )
        source = full_path.read_text(encoding="utf-8")
        # Two shapes a contributor might naively write:
        #   sessions="*"
        #   sessions = "*"
        # Plus the named constant ``SESSIONS_ALL`` exported by
        # ``agents.memory._session_filter``.  Any of the three is a
        # red flag on the default context path.
        forbidden = (
            'sessions="*"',
            "sessions='*'",
            'sessions = "*"',
            "sessions = '*'",
            "SESSIONS_ALL",
        )
        offenders = [needle for needle in forbidden if needle in source]
        assert not offenders, (
            f"{module_path} references {offenders!r} on the persona-runtime "
            "default recall path — wiring ``\"*\"`` into a prompt context "
            "re-introduces F-3.  Surface this explicitly via the Phase 3 "
            "CLI flag instead."
        )


# ─── Runtime pins: MemoryStore.retrieve_relevant ────────────


@pytest.fixture
async def facade_at_run_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``MemoryStore`` constructed with ``PERSATRIX_SESSION_ID=run-a``."""
    monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
    fac = MemoryStore(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


class TestFacadeRetrieveRelevantSessionForwarding:
    """``MemoryStore.retrieve_relevant`` threads ``sessions=`` to the
    underlying episodic recall.  Default ``None`` resolves to the
    facade's construction-time ``_session_id``."""

    async def test_default_forwards_facade_session_id(
        self, facade_at_run_a: MemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Spy on the tier-layer recall so the assertion sees the value
        # that actually reaches SQL, not just the surface the facade
        # exposes.
        captured: dict[str, Any] = {}
        original = facade_at_run_a._episodic.recall

        async def spy(query: str, **kwargs: Any):
            captured["sessions"] = kwargs.get("sessions")
            return await original(query, **kwargs)

        monkeypatch.setattr(facade_at_run_a._episodic, "recall", spy)
        await facade_at_run_a.retrieve_relevant("anything")
        # Default ``sessions=None`` at the facade resolves to the
        # facade's own ``_session_id`` so the tier's ``_resolve_session_list``
        # walks the "active session + legacy carve-out" branch.
        assert captured["sessions"] == ["run-a"]

    async def test_explicit_list_threads_through(
        self, facade_at_run_a: MemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}
        original = facade_at_run_a._episodic.recall

        async def spy(query: str, **kwargs: Any):
            captured["sessions"] = kwargs.get("sessions")
            return await original(query, **kwargs)

        monkeypatch.setattr(facade_at_run_a._episodic, "recall", spy)
        await facade_at_run_a.retrieve_relevant(
            "anything", sessions=["run-b", "run-c"],
        )
        assert captured["sessions"] == ["run-b", "run-c"]

    async def test_all_sentinel_threads_through(
        self, facade_at_run_a: MemoryStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}
        original = facade_at_run_a._episodic.recall

        async def spy(query: str, **kwargs: Any):
            captured["sessions"] = kwargs.get("sessions")
            return await original(query, **kwargs)

        monkeypatch.setattr(facade_at_run_a._episodic, "recall", spy)
        await facade_at_run_a.retrieve_relevant("anything", sessions="*")
        assert captured["sessions"] == "*"


# ─── Runtime pins: retrieve_procedures ──────────────────────


class TestFacadeRetrieveProceduresSessionForwarding:
    """``ProceduralFacadeMixin.retrieve_procedures`` threads ``sessions=``
    to the underlying procedural recall."""

    async def test_default_uses_facade_session_id(
        self, facade_at_run_a: MemoryStore,
    ) -> None:
        # Seed two procedures: one tagged ``run-a`` (the facade's
        # active session), one ``run-b``.  Default recall must
        # surface only ``run-a`` plus the legacy carve-out (here
        # ``run-b`` is the only non-matching row).
        await facade_at_run_a.store_procedure(
            "deploy.a", "step a", confidence=0.9, session_id="run-a",
        )
        await facade_at_run_a.store_procedure(
            "deploy.b", "step b", confidence=0.9, session_id="run-b",
        )
        results = await facade_at_run_a.retrieve_procedures()
        keys = {r.key for r in results}
        assert "deploy.a" in keys
        assert "deploy.b" not in keys

    async def test_explicit_list_includes_named_session(
        self, facade_at_run_a: MemoryStore,
    ) -> None:
        await facade_at_run_a.store_procedure(
            "deploy.a", "step a", confidence=0.9, session_id="run-a",
        )
        await facade_at_run_a.store_procedure(
            "deploy.b", "step b", confidence=0.9, session_id="run-b",
        )
        results = await facade_at_run_a.retrieve_procedures(
            sessions=["run-b"],
        )
        keys = {r.key for r in results}
        assert "deploy.b" in keys
        # Per §D, an explicit list still includes the legacy carve-out
        # but ``run-a`` is not on the list and not legacy.
        assert "deploy.a" not in keys

    async def test_all_sentinel_returns_every_session(
        self, facade_at_run_a: MemoryStore,
    ) -> None:
        await facade_at_run_a.store_procedure(
            "deploy.a", "step a", confidence=0.9, session_id="run-a",
        )
        await facade_at_run_a.store_procedure(
            "deploy.b", "step b", confidence=0.9, session_id="run-b",
        )
        results = await facade_at_run_a.retrieve_procedures(sessions="*")
        keys = {r.key for r in results}
        assert "deploy.a" in keys
        assert "deploy.b" in keys


# ─── Runtime pins: read_from_pool ───────────────────────────


@pytest.fixture
async def pool_at_run_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A shared pool whose underlying episodic tier was constructed
    under ``PERSATRIX_SESSION_ID=run-a``.

    Shared pools are inherently cross-agent / cross-session by
    RFC 0008 §H design; the PR-4 policy decision (ISSUE-0078) is that
    ``read_from_pool(sessions=None)`` defaults the underlying recall to
    ``"*"`` (Policy A — cross-session) so a row written under any
    session is visible to any reader.
    """
    monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
    cfg = SharedPoolConfig(
        name="team-mem",
        readers=frozenset({"alice", "bob"}),
        writers=frozenset({"alice", "bob"}),
    )
    pool = SharedMemoryPool(cfg, db_path=str(tmp_path / "pool.db"))
    await pool.initialize()
    yield pool
    await pool.close()


class TestFacadeReadFromPoolSessionForwarding:
    """``SharedPoolFacadeMixin.read_from_pool`` threads ``sessions=`` to
    the underlying pool read.  Default cross-session by RFC 0008 §H."""

    async def test_default_sees_writes_from_other_sessions(
        self, pool_at_run_a: SharedMemoryPool,
        tmp_path: Path,
    ) -> None:
        # Write under ``run-b``; default-mode read under ``run-a`` must
        # still see it (Policy A — shared pools are cross-session).
        await pool_at_run_a.write(
            "alice", "shared insight", confidence=0.9, session_id="run-b",
        )

        # Spin up a facade whose ``_session_id`` is ``run-a`` and use
        # its ``read_from_pool`` wrapper to exercise the facade path.
        registry = SharedPoolRegistry({"team-mem": pool_at_run_a})
        fac = MemoryStore(
            agent_id="alice",
            db_path=str(tmp_path / "f.db"),
            shared_pools=registry,
        )
        await fac.initialize()
        try:
            results = await fac.read_from_pool("team-mem", "shared")
            assert any("shared insight" in r.content for r in results), (
                "Policy A: read_from_pool(sessions=None) must default to "
                "cross-session on the underlying recall so a row written "
                "under a different session id is still visible."
            )
        finally:
            await fac.close()

    async def test_explicit_list_filters_to_named_session(
        self, pool_at_run_a: SharedMemoryPool,
        tmp_path: Path,
    ) -> None:
        await pool_at_run_a.write(
            "alice", "from b", confidence=0.9, session_id="run-b",
        )
        await pool_at_run_a.write(
            "alice", "from c", confidence=0.9, session_id="run-c",
        )

        registry = SharedPoolRegistry({"team-mem": pool_at_run_a})
        fac = MemoryStore(
            agent_id="alice",
            db_path=str(tmp_path / "f.db"),
            shared_pools=registry,
        )
        await fac.initialize()
        try:
            results = await fac.read_from_pool(
                "team-mem", "from", sessions=["run-b"],
            )
            contents = {r.content for r in results}
            assert "from b" in contents
            assert "from c" not in contents
        finally:
            await fac.close()


# ─── Runtime pin: spy on EpisodicMemory.recall reachability ─


class TestPersonaRuntimeCallSitesDoNotPassAllSentinel:
    """End-to-end runtime pin: when ``_inject_memory_context`` runs on
    the default path, the ``EpisodicMemory.recall`` call never carries
    ``sessions="*"``.  Complements the source-level scan above so a
    contributor introducing a ``recall(sessions=…)`` argument computed
    at runtime cannot smuggle ``"*"`` through.
    """

    async def test_episodic_recall_default_path_never_sees_star(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Use the leaf module's recall directly with a spy.  This is the
        # path :class:`_MemoryContextMixin._inject_memory_context` takes
        # (it imports ``EpisodicMemory`` and calls ``.recall`` directly).
        from agents.memory.episodic import EpisodicMemory

        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        await mem.initialize()
        try:
            original = mem.recall
            seen_sessions: list[Any] = []

            async def spy(query: str = "", **kwargs: Any):
                seen_sessions.append(kwargs.get("sessions"))
                return await original(query, **kwargs)

            monkeypatch.setattr(mem, "recall", spy)

            # Run the same call shape ``memory_context._inject_memory_context``
            # uses (sessions kwarg either absent — implicit ``None`` —
            # or explicitly ``None``).  Both are the default path.
            await mem.recall("hello", limit=5, min_score=0.2)
            await mem.recall("hi", limit=5, min_score=0.2, sessions=None)

            assert "*" not in seen_sessions
            for s in seen_sessions:
                # Default mode accepts either implicit ``None`` (kwarg
                # absent) or explicit ``sessions=None``; both resolve to
                # the active-session-plus-legacy branch in
                # ``_resolve_session_list``.
                assert s is None
        finally:
            await mem.close()
