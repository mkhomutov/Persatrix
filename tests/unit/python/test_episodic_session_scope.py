"""
Tests for RFC 0031 Phase 2 PR 2 — session-scoped recall on the episodic +
notes tiers.

PR 1 (migration v9) brought ``notes`` to ``session_id`` write-path parity
with ``episodes`` / ``relationships`` / ``facts``; every row is tagged
but every read still surfaces every session's rows.  PR 2 closes that:
``EpisodicMemory.recall`` and ``NoteStore.recall_notes`` gain a
``sessions`` keyword-only parameter shaped by `RFC 0031 §D
<../../docs/rfcs/0031-per-session-namespacing-channels.md#d-recall-semantics>`_:

* ``sessions=None`` (default) → active session only, plus the always-
  visible ``legacy`` carve-out (the §D shape the persona-runtime gets
  with no explicit kwarg — this is the F-3 closer).
* ``sessions=["a", "b"]`` → named list, plus the ``legacy`` carve-out.
* ``sessions="*"`` → no filter (CLI / debug sentinel; the
  persona-runtime context path is pinned in PR 4 not to reach this).
* ``sessions=[]`` → ``ValueError`` (§D guard against the silent
  legacy-only collapse — an empty list is never "no constraint").

The active session is resolved once at tier construction time via
:func:`agents.session_id.resolve_session_id_silent` so the persona
prompt-assembly path (which reads ``EpisodicMemory.recall`` directly,
bypassing the :class:`agents.memory.MemoryStore` facade — see the
:file:`episodic.py` comment block) gets the same ``sessions=None``
contract as the facade path.

These tests assert the recall contract end-to-end through the tier
public API; the SQL fragment shape is pinned separately in
:mod:`tests.unit.python.test_session_id_session_filter`.

The OTEL span attribute test pins OQ #7 — ``session_id`` on the
``EPISODIC_RECALL_SPAN`` recall span (acceptable as a span attribute
because session cardinality is bounded per trace; it would not be
acceptable as a metric label, which is why ``sessions.writes`` carries
``session_id`` as an attribute on a counter rather than dimensioning a
gauge by it).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from agents.memory.episodic import EpisodicMemory
from agents.session_id import LEGACY_SESSION_ID, SESSION_ID_ENV_VAR

if TYPE_CHECKING:
    pass


# ─── Helpers ────────────────────────────────────────────────


async def _seed_three_session_episodes(
    mem: EpisodicMemory, query_token: str = "kayak",
) -> dict[str, str]:
    """Store one episode in each of ``run-a`` / ``run-b`` / ``legacy``.

    Each summary embeds ``query_token`` so the FTS5 / LIKE recall paths
    find all three on a single search; recency-path callers pass
    ``query=""`` and ignore the token.  Returns ``{session_id: ep_id}``.
    """
    a = await mem.store_episode(
        f"{query_token} on the lake", {}, session_id="run-a", importance=0.5,
    )
    b = await mem.store_episode(
        f"{query_token} in the river", {}, session_id="run-b", importance=0.5,
    )
    legacy = await mem.store_episode(
        f"{query_token} at the coast", {}, session_id="legacy", importance=0.5,
    )
    return {"run-a": a, "run-b": b, "legacy": legacy}


async def _seed_three_session_notes(
    mem: EpisodicMemory, query_token: str = "kayak",
) -> dict[str, str]:
    """Same shape as :func:`_seed_three_session_episodes` for notes."""
    a = await mem.store_note(f"{query_token}-topic-a", "content-a", session_id="run-a")
    b = await mem.store_note(f"{query_token}-topic-b", "content-b", session_id="run-b")
    legacy = await mem.store_note(
        f"{query_token}-topic-l", "content-legacy", session_id="legacy",
    )
    return {"run-a": a, "run-b": b, "legacy": legacy}


@pytest.fixture
async def memory_at_run_a(monkeypatch: pytest.MonkeyPatch):
    """``EpisodicMemory`` constructed with ``PERSATRIX_SESSION_ID=run-a``.

    The default conftest fixture removes the env var; here we set it
    before construction so ``_active_session_id`` resolves to ``"run-a"``
    and the ``sessions=None`` default exercises the active-session path
    rather than the ``legacy`` carve-out alone.
    """
    monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
    mem = EpisodicMemory(agent_id="test-agent", db_path=":memory:")
    await mem.initialize()
    yield mem
    await mem.close()


@pytest.fixture
def span_exporter() -> Iterator[object]:
    """Capture finished spans for the OTEL attribute test (OQ #7).

    Same shape as :file:`tests/unit/python/test_llm_client_model_alias_span.py`
    — installs a fresh ``InMemorySpanExporter`` on the global provider
    without calling ``init_tracing`` (which would build a competing
    provider and break tests sharing the global).
    """
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


# ─── Active-session resolution at tier construction ─────────


class TestActiveSessionResolution:
    """``EpisodicMemory`` resolves the active session once at __init__.

    Why on the tier and not only on the facade: the persona-runtime
    memory-context path reads ``EpisodicMemory.recall`` directly
    (the explicit comment at :file:`agents/memory/episodic.py:335`),
    bypassing :class:`agents.memory.MemoryStore`.  If the active session
    were resolved only on the facade, the ``sessions=None`` default
    on the persona-direct path would collapse to legacy-only and F-3
    would stay open on the persona-prompt surface.  The tier must own
    its own active session.
    """

    async def test_default_active_session_is_legacy_when_env_unset(
        self,
    ) -> None:
        # Autouse ``_isolate_session_env`` in conftest already deletes
        # the env var; constructing here gives a clean baseline.
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        try:
            await mem.initialize()
            assert mem._active_session_id == LEGACY_SESSION_ID
        finally:
            await mem.close()

    async def test_env_var_resolved_at_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        try:
            await mem.initialize()
            assert mem._active_session_id == "run-a"
        finally:
            await mem.close()

    async def test_active_session_id_immutable_after_construction(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A late ``setenv`` after construction must not retroactively
        re-tag recall; the tier captured the value at __init__.
        """
        monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
        mem = EpisodicMemory(agent_id="t", db_path=":memory:")
        try:
            await mem.initialize()
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            assert mem._active_session_id == "run-a"
        finally:
            await mem.close()


# ─── EpisodicMemory.recall — sessions parameter ─────────────


class TestEpisodicRecallSessionFilter:
    """The four RFC 0031 §D modes on ``EpisodicMemory.recall``."""

    async def test_default_returns_active_plus_legacy_only(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_episodes(memory_at_run_a)
        eps = await memory_at_run_a.recall("kayak", limit=10)
        ep_ids = {ep.id for ep in eps}
        # ``run-a`` (active) + ``legacy`` carve-out present;
        # ``run-b`` absent — this is the F-3 closer assertion.
        assert ids["run-a"] in ep_ids
        assert ids["legacy"] in ep_ids
        assert ids["run-b"] not in ep_ids

    async def test_default_excludes_other_session_even_when_only_match(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        """If the only matching row is in a non-active non-legacy
        session, the default recall returns an empty list — *not*
        a fallback that surfaces it.  Tightens the "legacy carve-out
        could mask a no-op filter" risk per the PR plan's risk table.
        """
        await memory_at_run_a.store_episode(
            "trampoline routine", {}, session_id="run-b",
        )
        eps = await memory_at_run_a.recall("trampoline", limit=10)
        assert eps == []

    async def test_explicit_list_returns_named_plus_legacy(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_episodes(memory_at_run_a)
        eps = await memory_at_run_a.recall(
            "kayak", limit=10, sessions=["run-b"],
        )
        ep_ids = {ep.id for ep in eps}
        assert ids["run-b"] in ep_ids
        assert ids["legacy"] in ep_ids
        # ``run-a`` (active) is NOT in the explicit list and not legacy;
        # explicit mode honours the list verbatim plus the carve-out.
        assert ids["run-a"] not in ep_ids

    async def test_star_returns_all_sessions(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_episodes(memory_at_run_a)
        eps = await memory_at_run_a.recall("kayak", limit=10, sessions="*")
        ep_ids = {ep.id for ep in eps}
        assert ep_ids == set(ids.values())

    async def test_empty_list_raises_value_error(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await memory_at_run_a.recall("kayak", sessions=[])

    async def test_recency_path_filters_by_session(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        """Empty query → ``recall_recency`` path.  The persona-runtime
        channel-history tier hits this path (see
        :file:`agents/persona_runtime/channel_history.py`); it must
        filter by session or F-3 stays open on that surface.
        """
        ids = await _seed_three_session_episodes(memory_at_run_a)
        eps = await memory_at_run_a.recall("", limit=10)
        ep_ids = {ep.id for ep in eps}
        assert ids["run-a"] in ep_ids
        assert ids["legacy"] in ep_ids
        assert ids["run-b"] not in ep_ids


# ─── EPISODIC_RECALL_SPAN session_id attribute (OQ #7) ──────


class TestEpisodicRecallSpanSessionAttribute:
    async def test_span_carries_session_id_attribute(
        self, memory_at_run_a: EpisodicMemory, span_exporter,
    ) -> None:
        await memory_at_run_a.recall("kayak", limit=5)
        spans = [
            s for s in span_exporter.get_finished_spans()
            if s.name == "agent.memory.episodic.recall"
        ]
        assert spans, "no episodic recall spans captured"
        # Capture the most recent — autouse fixtures elsewhere may have
        # emitted earlier spans.
        attrs = spans[-1].attributes or {}
        # OQ #7: ``session_id`` on a span (cardinality OK), never a metric
        # label.  The attribute names the *active* session, not the
        # filter mode — operators tracing latency by session want the
        # row that produced the query, not the filter shape.
        assert attrs.get("session_id") == "run-a"


# ─── NoteStore.recall_notes — sessions parameter ────────────


class TestNotesRecallSessionFilter:
    """Mirrors :class:`TestEpisodicRecallSessionFilter` on the notes tier.

    Notes are recalled into the persona prompt at
    :file:`agents/persona_runtime/memory_context.py:331`; with no
    session filter on the notes tier, F-3 stays open on the notes
    surface even after the episodic tier is fixed.
    """

    async def test_default_returns_active_plus_legacy_only(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_notes(memory_at_run_a)
        notes = await memory_at_run_a.recall_notes("kayak", limit=10)
        note_ids = {n.id for n in notes}
        assert ids["run-a"] in note_ids
        assert ids["legacy"] in note_ids
        assert ids["run-b"] not in note_ids

    async def test_explicit_list_returns_named_plus_legacy(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_notes(memory_at_run_a)
        notes = await memory_at_run_a.recall_notes(
            "kayak", limit=10, sessions=["run-b"],
        )
        note_ids = {n.id for n in notes}
        assert ids["run-b"] in note_ids
        assert ids["legacy"] in note_ids
        assert ids["run-a"] not in note_ids

    async def test_star_returns_all_sessions(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_notes(memory_at_run_a)
        notes = await memory_at_run_a.recall_notes(
            "kayak", limit=10, sessions="*",
        )
        note_ids = {n.id for n in notes}
        assert note_ids == set(ids.values())

    async def test_empty_list_raises_value_error(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            await memory_at_run_a.recall_notes("kayak", sessions=[])

    async def test_recency_path_filters_by_session(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        ids = await _seed_three_session_notes(memory_at_run_a)
        notes = await memory_at_run_a.recall_notes("", limit=10)
        note_ids = {n.id for n in notes}
        assert ids["run-a"] in note_ids
        assert ids["legacy"] in note_ids
        assert ids["run-b"] not in note_ids


# ─── Notes prune — session-scoped (PR 1 review F1 carry-forward) ──


class TestNotesPruneIsSessionScoped:
    """``NoteStore._prune_notes`` filters by ``(agent_id, session_id)``.

    PR 1 left ``_prune_notes`` agent-scoped only — a write tagged
    ``session_id="run-b"`` that trips ``max_notes`` could delete the
    oldest ``session_id="run-a"`` row on the same agent.  Write-side
    isolation was one-way; the lifecycle path still bled.  PR 2
    scopes the prune to ``(agent_id, session_id)`` so session B can
    no longer evict session A's notes (accepting that session B also
    cannot reclaim space session A is holding — a per-session
    capacity is the operator-visible cost of clean isolation).
    """

    async def test_prune_does_not_evict_other_session_notes(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        # Seed two run-a notes (the protected session in this test).
        a1 = await memory_at_run_a.store_note(
            "topic-a1", "content-a1", session_id="run-a", max_notes=10,
        )
        a2 = await memory_at_run_a.store_note(
            "topic-a2", "content-a2", session_id="run-a", max_notes=10,
        )
        # Now hammer the same agent with run-b writes at a low
        # ``max_notes`` — pre-PR-2, the prune subquery sorted all
        # agent rows together and would evict a1 (oldest).  Post-PR-2,
        # the prune is scoped to (agent_id, session_id="run-b") so
        # a1 / a2 are untouched.
        for i in range(5):
            await memory_at_run_a.store_note(
                f"topic-b{i}", f"content-b{i}",
                session_id="run-b", max_notes=2,
            )

        # Both run-a notes still present.
        ids = await memory_at_run_a.recall_notes(
            "", limit=20, sessions=["run-a"],
        )
        ids_set = {n.id for n in ids}
        assert a1 in ids_set, "run-b prune evicted run-a's oldest note"
        assert a2 in ids_set

        # And run-b is at capacity 2 (its own cap, scoped to its session).
        run_b = await memory_at_run_a.recall_notes(
            "", limit=20, sessions=["run-b"],
        )
        assert len(run_b) == 2


# ─── scope_recall passthrough (RFC 0020 §G orthogonal) ─────


class TestScopeRecallSessionPassthrough:
    """:func:`agents.memory.scope_recall.recall_with_scope_filter` forwards
    ``sessions`` to ``EpisodicMemory.recall`` orthogonally to the §G
    scope predicate (`RFC 0031 §F
    <../../docs/rfcs/0031-per-session-namespacing-channels.md#f-interaction-with-rfc-0020-g-scope>`_:
    separate column, separate index, separate WHERE clause — the two
    predicates AND together but never widen each other).
    """

    async def test_passthrough_default_filters_by_session(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        from agents.memory.scope_recall import recall_with_scope_filter

        ids = await _seed_three_session_episodes(memory_at_run_a)
        eps = await recall_with_scope_filter(
            memory_at_run_a, "kayak", limit=10,
        )
        ep_ids = {ep.id for ep in eps}
        assert ids["run-a"] in ep_ids
        assert ids["run-b"] not in ep_ids
        assert ids["legacy"] in ep_ids

    async def test_passthrough_explicit_star_returns_all(
        self, memory_at_run_a: EpisodicMemory,
    ) -> None:
        from agents.memory.scope_recall import recall_with_scope_filter

        ids = await _seed_three_session_episodes(memory_at_run_a)
        eps = await recall_with_scope_filter(
            memory_at_run_a, "kayak", limit=10, sessions="*",
        )
        ep_ids = {ep.id for ep in eps}
        assert ep_ids == set(ids.values())


# ─── Cross-tier file-share regression (memory_pair-style) ────


class TestCrossEpisodicMemoryInstanceIsolation:
    """Two ``EpisodicMemory`` instances pointing at the same DB but
    constructed with different ``PERSATRIX_SESSION_ID`` env values each
    see only their own session + legacy by default.

    Mirrors the ``memory_pair`` fixture pattern in :file:`conftest.py`
    but with distinct active sessions instead of distinct agents — the
    canonical reproduction of F-3 (same channel, same user, different
    operator session).
    """

    async def test_two_instances_isolated_by_active_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``TemporaryDirectory`` (vs. ``mkstemp`` + ``os.unlink``) cleans
        # the whole sibling set on exit — WAL mode creates ``-shm`` /
        # ``-wal`` companion files alongside the main ``.db``, and an
        # ``os.unlink(path)`` of only the main file leaves the
        # companions behind on Windows where the OS does not garbage-
        # collect the temp dir.  (PR 449 deep-review carry-forward.)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "memory.db")

            # Construct memory_a under run-a; write a run-a row.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-a")
            mem_a = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_a.initialize()
            try:
                await mem_a.store_episode(
                    "fingerprint-a", {}, session_id="run-a",
                )
            finally:
                await mem_a.close()

            # Construct memory_b under run-b on the same agent + same DB.
            monkeypatch.setenv(SESSION_ID_ENV_VAR, "run-b")
            mem_b = EpisodicMemory(agent_id="shared-agent", db_path=path)
            await mem_b.initialize()
            try:
                eps = await mem_b.recall("fingerprint-a", limit=10)
                # Pre-PR-2: this returned the run-a row → F-3 reproduction.
                # Post-PR-2: empty — the run-a row is invisible to run-b.
                assert eps == []
            finally:
                await mem_b.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
