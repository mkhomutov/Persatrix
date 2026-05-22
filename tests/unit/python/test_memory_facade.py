"""Unit tests for ``agents.memory.facade.MemoryStore`` (RFC 0008 PR plan PR 2).

Covers the pinned API surface that downstream RFCs rely on:

- ``retrieve_relevant`` AND-tag semantics (RFC 0011 PR plan PR 5 contract).
- ``store_observation`` round-tripping into ``retrieve_relevant``.
- ``compress`` extractive token budget enforcement (RFC 0020 PR plan PR 4 hook).
- Lifecycle: ``initialize()`` is idempotent, ``close()`` is safe to call twice,
  use-before-init raises a clear error.
- ``budget_to_limit`` translation from the orchestrator's
  ``_context_package.budget_memory_tokens`` to a recall ``limit``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from agents.memory.facade import (
    DEFAULT_AVG_ENTRY_TOKENS,
    CompressedView,
    MemoryDisabledError,
    MemoryEntry,
    MemoryStore,
    budget_to_limit,
)


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryStore, None]:
    """An initialised in-memory ``MemoryStore`` for a synthetic agent."""
    fac = MemoryStore(agent_id="facade-test", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


# ─── Lifecycle ────────────────────────────────────────────────


async def test_initialize_is_idempotent() -> None:
    fac = MemoryStore(agent_id="idem", db_path=":memory:")
    await fac.initialize()
    # A second call must not raise; it logs a warning and no-ops.
    await fac.initialize()
    await fac.close()


async def test_close_before_initialize_is_noop() -> None:
    fac = MemoryStore(agent_id="noop", db_path=":memory:")
    # Calling close() without initialize() must be safe.
    await fac.close()


async def test_use_before_initialize_raises() -> None:
    fac = MemoryStore(agent_id="cold", db_path=":memory:")
    # PR 2a follow-up L1/L2: facade raises the memory-specific error type
    # (still a RuntimeError subclass for backward compat).
    with pytest.raises(MemoryDisabledError, match="not initialised"):
        await fac.retrieve_relevant("anything")


async def test_close_then_reuse_raises() -> None:
    fac = MemoryStore(agent_id="reuse", db_path=":memory:")
    await fac.initialize()
    await fac.close()
    with pytest.raises(MemoryDisabledError, match="not initialised"):
        await fac.retrieve_relevant("anything")


async def test_episodic_property_raises_memory_disabled() -> None:
    """L1: ``episodic`` property uses the same error contract as writes."""
    fac = MemoryStore(agent_id="ep", db_path=":memory:")
    with pytest.raises(MemoryDisabledError, match="not initialised"):
        _ = fac.episodic


# ─── store_observation + retrieve_relevant ───────────────────


async def test_store_observation_returns_id(facade: MemoryStore) -> None:
    entry_id = await facade.store_observation(
        "the user prefers tabs over spaces",
        importance=0.8,
        tags=("preferences",),
    )
    assert isinstance(entry_id, str)
    assert entry_id  # non-empty


async def test_store_observation_rejects_empty_content(facade: MemoryStore) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        await facade.store_observation("   ")


async def test_store_observation_rejects_non_positive_ttl(facade: MemoryStore) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        await facade.store_observation("hello", ttl_seconds=0)


async def test_retrieve_relevant_finds_stored_observation(facade: MemoryStore) -> None:
    await facade.store_observation(
        "the user uses Python type hints in 3.12 syntax",
        importance=0.9,
        tags=("python", "syntax"),
    )
    results = await facade.retrieve_relevant("python type hints", limit=5)
    assert len(results) >= 1
    assert any("type hints" in entry.content for entry in results)


async def test_retrieve_relevant_tags_and_semantics(facade: MemoryStore) -> None:
    """RFC 0011 PR plan PR 5 contract: tags filter is AND, not OR."""
    await facade.store_observation(
        "alpha entry",
        importance=0.9,
        tags=("a", "b"),
    )
    await facade.store_observation(
        "beta entry",
        importance=0.9,
        tags=("a",),
    )
    await facade.store_observation(
        "gamma entry",
        importance=0.9,
        tags=("b",),
    )

    results_ab = await facade.retrieve_relevant(
        "entry", limit=10, tags=("a", "b"),
    )
    contents_ab = {entry.content for entry in results_ab}
    assert "alpha entry" in contents_ab
    # AND semantics: beta has only "a", gamma has only "b" — both excluded.
    assert "beta entry" not in contents_ab
    assert "gamma entry" not in contents_ab

    # Empty tags filter is a no-op — all matches pass.
    results_none = await facade.retrieve_relevant("entry", limit=10, tags=())
    assert {"alpha entry", "beta entry", "gamma entry"}.issubset(
        {entry.content for entry in results_none},
    )


async def test_retrieve_relevant_scope_filter(facade: MemoryStore) -> None:
    await facade.store_observation(
        "observation in slack-dev",
        importance=0.9,
        scope="channel:slack-#dev",
    )
    await facade.store_observation(
        "observation in slack-general",
        importance=0.9,
        scope="channel:slack-#general",
    )
    results = await facade.retrieve_relevant(
        "observation", limit=10, scope="channel:slack-#dev",
    )
    contents = {entry.content for entry in results}
    assert "observation in slack-dev" in contents
    assert "observation in slack-general" not in contents


async def test_retrieve_relevant_min_score_validation(facade: MemoryStore) -> None:
    with pytest.raises(ValueError, match="min_score"):
        await facade.retrieve_relevant("q", min_score=1.5)


async def test_retrieve_relevant_drops_pending_summary_rows(
    facade: MemoryStore,
) -> None:
    """RFC 0020 PR 5 — defense-in-depth: ``[summary pending]`` rows are hidden.

    The two-phase close path (RFC 0020 PR 4) writes a row with
    :data:`SUMMARY_PENDING_TEXT` before the LLM summariser fires. If the
    summariser is in flight when ``retrieve_relevant`` runs, the pending
    sentinel must not surface to recall consumers — they should see the
    finalised neighbour rows but not the placeholder text.
    """
    from agents.memory.interactions import (
        SUMMARY_PENDING_TEXT,
        SUMMARY_UNAVAILABLE_TEXT,
    )

    # Finalised closed-interaction row.
    await facade.episodic.store_episode(
        summary="alpha closed",
        context={"scope": "thread:t-1"},
        importance=0.8,
        interaction_id="alpha",
        started_at=100.0,
        closed_at=110.0,
        turn_count=3,
        scope="thread:t-1",
    )
    # In-flight closing row — the LLM ``UPDATE`` has not landed yet.
    await facade.episodic.store_episode(
        summary=SUMMARY_PENDING_TEXT,
        context={"scope": "thread:t-2"},
        importance=0.8,
        interaction_id="beta",
        started_at=200.0,
        closed_at=210.0,
        turn_count=4,
        scope="thread:t-2",
    )
    # Janitor-finalised row (fallback summary). Must remain visible —
    # it is the surface the operator sees when summarisation failed and
    # hiding it would silently swallow real conversational data.
    await facade.episodic.store_episode(
        summary=SUMMARY_UNAVAILABLE_TEXT,
        context={"scope": "thread:t-3"},
        importance=0.8,
        interaction_id="gamma",
        started_at=300.0,
        closed_at=310.0,
        turn_count=5,
        scope="thread:t-3",
    )

    # Empty query → recency ranking returns every row. The defense-
    # in-depth filter must drop the pending sentinel even when the
    # caller did not ask for it explicitly.
    results = await facade.retrieve_relevant("", limit=10)
    summaries = {entry.content for entry in results}
    assert "alpha closed" in summaries
    assert SUMMARY_PENDING_TEXT not in summaries
    # The fallback marker still surfaces — the janitor explicitly chose
    # to publish a row whose summary is unrecoverable rather than drop it.
    assert SUMMARY_UNAVAILABLE_TEXT in summaries


async def test_retrieve_relevant_returns_memory_entry_shape(
    facade: MemoryStore,
) -> None:
    await facade.store_observation(
        "shape-check entry",
        importance=0.7,
        tags=("shape",),
        scope="task:shape",
    )
    results = await facade.retrieve_relevant("shape-check", limit=1)
    assert results, "expected at least one result"
    entry = results[0]
    assert isinstance(entry, MemoryEntry)
    assert entry.content == "shape-check entry"
    assert entry.importance == pytest.approx(0.7)
    assert "shape" in entry.tags
    assert entry.scope == "task:shape"
    assert 0.0 <= entry.score <= 1.0


# ─── store_procedure ─────────────────────────────────────────


async def test_store_procedure_validates_inputs(facade: MemoryStore) -> None:
    with pytest.raises(ValueError, match="key"):
        await facade.store_procedure("", "body", confidence=0.9)
    with pytest.raises(ValueError, match="confidence"):
        await facade.store_procedure("k", "body", confidence=1.5)


async def test_store_procedure_round_trip(facade: MemoryStore) -> None:
    await facade.store_procedure(
        "deploy-checklist",
        "Run tests, then bump version, then tag.",
        confidence=0.85,
    )
    results = await facade.retrieve_relevant(
        "deploy", limit=5, tags=("procedure:deploy-checklist",),
    )
    assert any("Run tests" in entry.content for entry in results)


# ─── compress ────────────────────────────────────────────────


def _entry(content: str, importance: float) -> MemoryEntry:
    return MemoryEntry(
        id=f"id-{content[:8]}",
        content=content,
        importance=importance,
        tags=(),
        created_at=0.0,
        score=0.0,
    )


def test_compress_under_budget_admits_all() -> None:
    fac = MemoryStore(agent_id="c", db_path=":memory:")
    entries = [_entry("short one", 0.9), _entry("short two", 0.5)]
    view = fac.compress(entries, target_tokens=1000)
    assert isinstance(view, CompressedView)
    assert view.entries_dropped == 0
    assert view.tokens_after <= 1000
    assert "short one" in view.summary
    assert "short two" in view.summary


def test_compress_drops_lowest_importance_when_over_budget() -> None:
    fac = MemoryStore(agent_id="c", db_path=":memory:")
    # Each ~25 chars → ~6 tokens via the chars/4 estimator.
    high = _entry("x" * 100, importance=0.9)  # ~25 tokens
    low = _entry("y" * 100, importance=0.1)   # ~25 tokens
    view = fac.compress([low, high], target_tokens=30)
    # High-importance entry admitted; low-importance dropped.
    assert "x" * 100 in view.summary
    assert "y" * 100 not in view.summary
    assert view.entries_dropped == 1
    assert view.tokens_after <= 30
    assert view.tokens_before >= view.tokens_after


def test_compress_zero_budget_drops_everything() -> None:
    fac = MemoryStore(agent_id="c", db_path=":memory:")
    view = fac.compress([_entry("anything", 0.9)], target_tokens=0)
    assert view.summary == ""
    assert view.entries_dropped == 1
    assert view.tokens_after == 0


def test_compress_negative_budget_raises() -> None:
    fac = MemoryStore(agent_id="c", db_path=":memory:")
    with pytest.raises(ValueError, match="target_tokens"):
        fac.compress([], target_tokens=-1)


def test_compress_idempotent_on_already_fitting_view() -> None:
    fac = MemoryStore(agent_id="c", db_path=":memory:")
    entries = [_entry("alpha", 0.9), _entry("beta", 0.5)]
    first = fac.compress(entries, target_tokens=100)
    # Re-compressing the admitted entries (synthesised from the summary)
    # is a no-op shape-wise.
    second = fac.compress(entries, target_tokens=100)
    assert first.summary == second.summary
    assert first.entries_dropped == second.entries_dropped


# ─── list_candidates (Phase 2 stub) ──────────────────────────


async def test_list_candidates_returns_empty_in_phase_2(facade: MemoryStore) -> None:
    candidates = await facade.list_candidates({"step_id": "anything"})
    assert candidates == []


# ─── budget_to_limit ─────────────────────────────────────────


def test_budget_to_limit_zero_budget_falls_back() -> None:
    # PR 1 emits 0 — fallback ensures retrieval still admits a small set.
    assert budget_to_limit(0) >= 1


def test_budget_to_limit_uses_default_avg() -> None:
    # 500 tokens / 100 tokens-per-entry == 5 (RFC 0008 PR plan integration spec).
    assert budget_to_limit(500) == 500 // DEFAULT_AVG_ENTRY_TOKENS


def test_budget_to_limit_clamps_to_one() -> None:
    # 50 tokens / 100 tokens-per-entry == 0; must clamp to 1, not collapse.
    assert budget_to_limit(50) == 1


def test_budget_to_limit_rejects_zero_avg() -> None:
    with pytest.raises(ValueError, match="avg_entry_tokens"):
        budget_to_limit(500, avg_entry_tokens=0)
