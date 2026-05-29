"""ISSUE-0081 PR 1 — context-local session id (contextvars enabler).

RFC 0031 closed F-3 (cross-**run** state bleed) on the assumption that
*one process == one session*: ``PERSATRIX_SESSION_ID`` is read once at
boot and cached at tier construction.  That assumption breaks the moment
one persona process fields more than one conversation concurrently — the
shared ``(agent_id, session_id)`` namespace lets conversation A's writes
recall into conversation B's prompt.  This is the intra-process sibling
of F-3.

The fix moves the active session id from *process-global, cached* to
*task-local, resolved at call time* via a
:class:`contextvars.ContextVar` (auto-copied per :class:`asyncio.Task`),
keeping the construction-time env snapshot as the fallback seed so every
single-session CLI / test / boot path is unchanged.

Four groups:

* :class:`TestSessionScopePrimitive` — the module-level ContextVar
  contract on :mod:`agents.session_id`: ``current_session_id`` is the
  override-only reader, ``session_scope`` sets/normalises/resets, and
  ``resolve_session_id_silent`` resolves ContextVar → env → legacy.
* :class:`TestDefaultRecallHonoursScope` — one shared tier instance;
  the default (``sessions=None``) recall narrows to the scoped session,
  the ``legacy`` carve-out stays visible, an explicit ``sessions=`` arg
  still wins, and the no-scope path falls back to the construction
  snapshot (regression — Phase 2 behaviour unchanged).
* :class:`TestConcurrentTasksIsolated` — two ``asyncio`` tasks under
  different ``session_scope`` reading **one** shared tier across an
  await barrier get isolated recall.  This is the property a
  process-global env var cannot provide.
* :class:`TestFacadeWriteDefaultHonoursScope` — the facade
  ``store_observation`` write path resolves the default ``session_id``
  at call time too, so a scoped write is tagged with the scoped session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.memory.facade import MemoryStore
from agents.session_id import (
    LEGACY_SESSION_ID,
    SESSION_ID_ENV_VAR,
    current_session_id,
    resolve_session_id_silent,
    session_scope,
)

_SEED = "seed-session"


# ─── Group A — the ContextVar primitive ─────────────────────


class TestSessionScopePrimitive:
    def test_current_session_id_none_when_unset(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``current_session_id`` is the *override* reader: it consults
        # only the ContextVar, never the env var, so an unset scope is
        # ``None`` even when PERSATRIX_SESSION_ID is exported.  The env
        # value is folded in by ``resolve_session_id_silent`` /
        # construction snapshots, not here.
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "env-run")
        assert current_session_id() is None

    def test_session_scope_sets_and_resets(self) -> None:
        assert current_session_id() is None
        with session_scope("conv-a") as resolved:
            assert resolved == "conv-a"
            assert current_session_id() == "conv-a"
        assert current_session_id() is None

    def test_session_scope_normalises_blank_to_legacy(self) -> None:
        # Mirrors ``normalize_session_id``: a blank scope can never
        # silently fall through to the construction snapshot — it
        # collapses to the ``legacy`` carve-out.
        with session_scope("   ") as resolved:
            assert resolved == LEGACY_SESSION_ID
            assert current_session_id() == LEGACY_SESSION_ID
        assert current_session_id() is None

    def test_session_scope_normalises_none_to_legacy(self) -> None:
        with session_scope(None) as resolved:
            assert resolved == LEGACY_SESSION_ID
            assert current_session_id() == LEGACY_SESSION_ID
        assert current_session_id() is None

    def test_resolve_silent_prefers_scope_over_env(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "env-run")
        assert resolve_session_id_silent() == "env-run"
        with session_scope("ctx-run"):
            assert resolve_session_id_silent() == "ctx-run"
        assert resolve_session_id_silent() == "env-run"

    def test_resolve_silent_scope_over_legacy_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(SESSION_ID_ENV_VAR, raising=False)
        assert resolve_session_id_silent() == LEGACY_SESSION_ID
        with session_scope("ctx-run"):
            assert resolve_session_id_silent() == "ctx-run"
        assert resolve_session_id_silent() == LEGACY_SESSION_ID

    def test_nested_scopes_restore_outer(self) -> None:
        with session_scope("outer"):
            assert current_session_id() == "outer"
            with session_scope("inner"):
                assert current_session_id() == "inner"
            assert current_session_id() == "outer"
        assert current_session_id() is None

    def test_scope_resets_on_exception(self) -> None:
        with pytest.raises(RuntimeError):
            with session_scope("boom"):
                assert current_session_id() == "boom"
                raise RuntimeError("body raised")
        assert current_session_id() is None


# ─── shared fixtures ────────────────────────────────────────


@pytest.fixture
async def episodic(monkeypatch: pytest.MonkeyPatch):
    """One shared ``EpisodicMemory`` seeded with ``PERSATRIX_SESSION_ID``.

    The construction snapshot resolves to ``_SEED``; the scope tests
    override it per call, the regression test relies on it as the
    no-scope fallback.
    """
    monkeypatch.setenv(SESSION_ID_ENV_VAR, _SEED)
    mem = EpisodicMemory(agent_id="issue-0081", db_path=":memory:")
    await mem.initialize()
    try:
        yield mem
    finally:
        await mem.close()


@pytest.fixture
async def facade(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SESSION_ID_ENV_VAR, _SEED)
    fac = MemoryStore(agent_id="issue-0081", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


# ─── Group B — default recall honours the active scope ──────


class TestDefaultRecallHonoursScope:
    async def test_scope_narrows_default_recall(
        self, episodic: EpisodicMemory,
    ) -> None:
        mem = episodic
        id_a = await mem.store_episode(
            "alpha secret on the lake", {}, session_id="conv-a", importance=0.5,
        )
        id_b = await mem.store_episode(
            "beta secret in the river", {}, session_id="conv-b", importance=0.5,
        )
        with session_scope("conv-a"):
            ids = {r.id for r in await mem.recall("secret", limit=10)}
        assert id_a in ids
        assert id_b not in ids
        with session_scope("conv-b"):
            ids = {r.id for r in await mem.recall("secret", limit=10)}
        assert id_b in ids
        assert id_a not in ids

    async def test_legacy_carveout_visible_under_scope(
        self, episodic: EpisodicMemory,
    ) -> None:
        mem = episodic
        id_a = await mem.store_episode(
            "alpha secret on the lake", {}, session_id="conv-a", importance=0.5,
        )
        id_legacy = await mem.store_episode(
            "legacy secret at the coast", {}, session_id="legacy", importance=0.5,
        )
        with session_scope("conv-a"):
            ids = {r.id for r in await mem.recall("secret", limit=10)}
        assert id_a in ids
        assert id_legacy in ids  # always-visible carve-out

    async def test_no_scope_uses_construction_snapshot(
        self, episodic: EpisodicMemory,
    ) -> None:
        # Regression: with no scope active the default recall path must
        # behave exactly as it did before — narrowed to the construction
        # snapshot (``_SEED``), not to ``legacy`` and not to a sibling
        # conversation.
        mem = episodic
        id_seed = await mem.store_episode(
            "seed secret here", {}, session_id=_SEED, importance=0.5,
        )
        id_other = await mem.store_episode(
            "other secret there", {}, session_id="conv-z", importance=0.5,
        )
        ids = {r.id for r in await mem.recall("secret", limit=10)}
        assert id_seed in ids
        assert id_other not in ids

    async def test_explicit_sessions_arg_overrides_scope(
        self, episodic: EpisodicMemory,
    ) -> None:
        # The ContextVar only fills the ``sessions=None`` default; an
        # explicit list still wins (CLI / cross-session recall paths).
        mem = episodic
        id_a = await mem.store_episode(
            "alpha secret lake", {}, session_id="conv-a", importance=0.5,
        )
        id_b = await mem.store_episode(
            "beta secret river", {}, session_id="conv-b", importance=0.5,
        )
        with session_scope("conv-a"):
            ids = {
                r.id
                for r in await mem.recall("secret", sessions=["conv-b"], limit=10)
            }
        assert id_b in ids
        assert id_a not in ids


# ─── Group C — concurrency: one tier, two scoped tasks ──────


class _AsyncBarrier:
    """Minimal N-party await barrier for single-threaded asyncio.

    Avoids depending on :class:`asyncio.Barrier` (3.11+).  Each waiter
    increments the count; the last one releases everyone.  Sufficient to
    force both ``session_scope`` blocks to be active simultaneously
    before either task recalls — the interleaving that a process-global
    session id cannot survive.
    """

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._count = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._count += 1
        if self._count >= self._parties:
            self._event.set()
        await self._event.wait()


async def _scoped_recall(
    mem: EpisodicMemory,
    barrier: _AsyncBarrier,
    scope: str,
    expect_id: str,
    forbid_id: str,
) -> set[str]:
    with session_scope(scope):
        # Suspend with the scope active so the sibling task's scope is
        # also live before either of us reads — proves task-local
        # isolation across an await point.
        await barrier.wait()
        ids = {r.id for r in await mem.recall("secret", limit=10)}
    assert expect_id in ids
    assert forbid_id not in ids
    return ids


class TestConcurrentTasksIsolated:
    async def test_two_tasks_one_tier_isolated_recall(
        self, episodic: EpisodicMemory,
    ) -> None:
        mem = episodic
        id_a = await mem.store_episode(
            "alpha secret on the lake", {}, session_id="conv-a", importance=0.5,
        )
        id_b = await mem.store_episode(
            "beta secret in the river", {}, session_id="conv-b", importance=0.5,
        )
        barrier = _AsyncBarrier(2)
        ids_a, ids_b = await asyncio.gather(
            _scoped_recall(mem, barrier, "conv-a", id_a, id_b),
            _scoped_recall(mem, barrier, "conv-b", id_b, id_a),
        )
        assert id_a in ids_a and id_b not in ids_a
        assert id_b in ids_b and id_a not in ids_b


# ─── Group D — facade write default resolves at call time ───


class TestFacadeWriteDefaultHonoursScope:
    async def test_observation_default_session_is_scope(
        self, facade: MemoryStore,
    ) -> None:
        with session_scope("conv-a"):
            ep_id = await facade.store_observation("scoped observation")
        db = facade.episodic._ensure_db()  # noqa: SLF001 — test inspection
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "conv-a"

    async def test_observation_without_scope_uses_construction_default(
        self, facade: MemoryStore,
    ) -> None:
        # No scope active → the construction-time snapshot (``_SEED``),
        # preserving the PR #337 facade contract.
        ep_id = await facade.store_observation("unscoped observation")
        db = facade.episodic._ensure_db()  # noqa: SLF001
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == _SEED

    async def test_explicit_kwarg_overrides_scope(
        self, facade: MemoryStore,
    ) -> None:
        with session_scope("conv-a"):
            ep_id = await facade.store_observation(
                "explicit wins", session_id="run-x",
            )
        db = facade.episodic._ensure_db()  # noqa: SLF001
        async with db.execute(
            "SELECT session_id FROM episodes WHERE id = ?", (ep_id,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "run-x"


# ─── Group E — recall span names the active session ─────────


@pytest.fixture
def span_exporter() -> Iterator[object]:
    """Capture finished spans (same shape as ``test_episodic_session_scope``)."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exp = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exp)
    provider.add_span_processor(processor)
    yield exp
    processor.shutdown()


class TestRecallSpanNamesActiveSession:
    """The ``EPISODIC_RECALL_SPAN`` ``session_id`` attribute names the
    call-time *active* session — never the ``sessions=`` filter shape.

    Pins the OQ #7 contract (``test_episodic_session_scope`` documents it
    as "the active session, not the filter mode") against the ISSUE-0081
    scope axis: the span must follow ``current_session_id() or snapshot``,
    so an explicit filter list cannot move it and a per-request
    ``session_scope`` wins over the construction snapshot.
    """

    @staticmethod
    def _latest_session_id(span_exporter: object) -> object:
        spans = [
            s for s in span_exporter.get_finished_spans()  # type: ignore[attr-defined]
            if s.name == "agent.memory.episodic.recall"
        ]
        assert spans, "no episodic recall spans captured"
        return (spans[-1].attributes or {}).get("session_id")

    async def test_explicit_filter_does_not_move_span_session(
        self, episodic: EpisodicMemory, span_exporter: object,
    ) -> None:
        # No scope: the span reports the construction snapshot (``_SEED``),
        # never the first filtered id ("conv-b") — that would be the filter
        # shape, the exact failure the OQ #7 contract forbids.
        await episodic.recall("secret", limit=5, sessions=["conv-b"])
        assert self._latest_session_id(span_exporter) == _SEED

    async def test_scope_wins_over_filter_and_snapshot(
        self, episodic: EpisodicMemory, span_exporter: object,
    ) -> None:
        # A per-request ``session_scope`` is the call-time active session;
        # the span reports it over both the filter shape ("conv-b") and the
        # construction snapshot (``_SEED``).
        with session_scope("conv-a"):
            await episodic.recall("secret", limit=5, sessions=["conv-b"])
        assert self._latest_session_id(span_exporter) == "conv-a"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
