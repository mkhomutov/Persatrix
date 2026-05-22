"""
Tests for RFC 0031 PR plan PR 4 finding #1: three persona-reachable
write surfaces beyond the direct ``store_episode`` /
``record_interaction`` path must also accept and persist ``session_id``.

The surfaces:

* :meth:`MemoryStore.store_observation` (RFC 0008 §B write path; the
  task-agent + sub-agent surface — ``facade.py:361`` reaches the
  underlying ``store_episode``).
* :meth:`ProceduralFacadeMixin.store_procedure` (RFC 0008 PR 5
  procedural-tier surface — ``facade_procedural.py:155``).
* :meth:`SharedMemoryPool.write` (RFC 0023 shared-pool surface —
  ``shared_pool.py:328``).

Without the kwarg, a persona running under ``PERSATRIX_SESSION_ID=run-a``
that publishes via the facade or shared pool lands its rows tagged
``legacy``, not ``run-a`` — invisible in Phase 1, a silent recall miss
the day Phase 2's filter lands.

Per the PR plan, the facade additionally resolves the active session
from :envvar:`PERSATRIX_SESSION_ID` at construction time so the
task-agent path inherits the carve-out without explicit threading.  An
explicit ``session_id`` kwarg overrides the construction-time default,
which the persona-runtime tier uses when it routes through the facade.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.memory.facade import MemoryStore
from agents.memory.shared_pool import (
    SharedMemoryPool,
    SharedPoolConfig,
)
from agents.memory.shared_pool_facade import publish_via_facade


@pytest.fixture
async def facade(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PERSATRIX_SESSION_ID", raising=False)
    fac = MemoryStore(agent_id="ember-owl", db_path=str(tmp_path / "m.db"))
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


# ─── store_observation ──────────────────────────────────────


class TestStoreObservationSessionID:
    async def test_default_writes_legacy(self, facade: MemoryStore) -> None:
        ep_id = await facade.store_observation("hello")
        db = facade.episodic._ensure_db()  # noqa: SLF001 — test inspection
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy"

    async def test_explicit_kwarg_round_trips(
        self, facade: MemoryStore,
    ) -> None:
        ep_id = await facade.store_observation("hi", session_id="run-a")
        db = facade.episodic._ensure_db()  # noqa: SLF001
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-a"

    async def test_env_var_is_facade_default(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # The task-agent / sub-agent path constructs MemoryStore without
        # threading session_id at every call site; instead the facade
        # resolves PERSATRIX_SESSION_ID once at construction so every
        # subsequent write inherits it.  This mirrors how the persona
        # constructor reads the env var once.
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
        fac = MemoryStore(
            agent_id="task-agent", db_path=str(tmp_path / "tm.db"),
        )
        await fac.initialize()
        try:
            ep_id = await fac.store_observation("a")
            db = fac.episodic._ensure_db()  # noqa: SLF001
            async with db.execute(
                "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "run-a", (
                "facade must inherit PERSATRIX_SESSION_ID at construction "
                f"so task-agent writes are not silently tagged 'legacy'; "
                f"got {row[0]!r}"
            )
        finally:
            await fac.close()

    async def test_explicit_kwarg_overrides_env_default(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # The persona-runtime path can pass session_id explicitly; the
        # caller's kwarg overrides the facade-level construction-time
        # default.  This is the path persona-reachable code uses when
        # it routes through the facade rather than the tiers directly.
        monkeypatch.setenv("PERSATRIX_SESSION_ID", "run-a")
        fac = MemoryStore(
            agent_id="ember-owl", db_path=str(tmp_path / "tm.db"),
        )
        await fac.initialize()
        try:
            ep_id = await fac.store_observation("b", session_id="run-b")
            db = fac.episodic._ensure_db()  # noqa: SLF001
            async with db.execute(
                "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "run-b"
        finally:
            await fac.close()


# ─── store_procedure ────────────────────────────────────────


class TestStoreProcedureSessionID:
    async def test_default_writes_legacy(self, facade: MemoryStore) -> None:
        await facade.store_procedure(
            "deploy.rollback", "run `make rollback`", confidence=0.9,
        )
        db = facade.episodic._ensure_db()  # noqa: SLF001
        async with db.execute(
            "SELECT session_id FROM episodes WHERE agent_id = ? "
            "AND tags_json LIKE ?",
            ("ember-owl", "%procedure:deploy.rollback%"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy"

    async def test_explicit_kwarg_round_trips(
        self, facade: MemoryStore,
    ) -> None:
        await facade.store_procedure(
            "deploy.smoke", "curl health", confidence=0.9,
            session_id="run-a",
        )
        db = facade.episodic._ensure_db()  # noqa: SLF001
        async with db.execute(
            "SELECT session_id FROM episodes WHERE agent_id = ? "
            "AND tags_json LIKE ?",
            ("ember-owl", "%procedure:deploy.smoke%"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-a"


# ─── SharedMemoryPool.write ─────────────────────────────────


class TestSharedMemoryPoolWriteSessionID:
    @pytest.fixture
    async def pool(self, tmp_path: Path):
        cfg = SharedPoolConfig(
            name="team-mem",
            readers=frozenset({"alice", "bob"}),
            writers=frozenset({"alice", "bob"}),
        )
        p = SharedMemoryPool(cfg, db_path=str(tmp_path / "shared.db"))
        await p.initialize()
        yield p
        await p.close()

    async def test_default_writes_legacy(self, pool: SharedMemoryPool) -> None:
        entry_id = await pool.write("alice", "hello", confidence=0.9)
        db = pool._episodic._ensure_db()  # noqa: SLF001 — test inspection
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy"

    async def test_explicit_kwarg_round_trips(
        self, pool: SharedMemoryPool,
    ) -> None:
        entry_id = await pool.write(
            "alice", "hi", confidence=0.9, session_id="run-a",
        )
        db = pool._episodic._ensure_db()  # noqa: SLF001
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-a"


# ─── publish_via_facade ─────────────────────────────────────


class TestPublishViaFacadeSessionID:
    async def test_threads_session_id_to_pool(
        self, tmp_path: Path,
    ) -> None:
        from agents.memory.shared_pool import SharedPoolRegistry
        cfg = SharedPoolConfig(
            name="team-mem",
            readers=frozenset({"alice"}),
            writers=frozenset({"alice"}),
        )
        pool = SharedMemoryPool(cfg, db_path=str(tmp_path / "shared.db"))
        await pool.initialize()
        registry = SharedPoolRegistry({"team-mem": pool})
        try:
            entry_id = await publish_via_facade(
                registry, "alice", "team-mem", "shared note",
                confidence=0.9, session_id="run-a",
            )
            db = pool._episodic._ensure_db()  # noqa: SLF001
            async with db.execute(
                "SELECT session_id FROM episodes WHERE id = ?", (entry_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "run-a"
        finally:
            await pool.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
