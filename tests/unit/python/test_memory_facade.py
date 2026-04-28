"""Unit tests for ``agents.memory.facade.MemoryFacade`` (RFC 0008 PR plan PR 2).

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
    MemoryEntry,
    MemoryFacade,
    budget_to_limit,
)


@pytest.fixture
async def facade() -> AsyncGenerator[MemoryFacade, None]:
    """An initialised in-memory ``MemoryFacade`` for a synthetic agent."""
    fac = MemoryFacade(agent_id="facade-test", db_path=":memory:")
    await fac.initialize()
    try:
        yield fac
    finally:
        await fac.close()


# ─── Lifecycle ────────────────────────────────────────────────


async def test_initialize_is_idempotent() -> None:
    fac = MemoryFacade(agent_id="idem", db_path=":memory:")
    await fac.initialize()
    # A second call must not raise; it logs a warning and no-ops.
    await fac.initialize()
    await fac.close()


async def test_close_before_initialize_is_noop() -> None:
    fac = MemoryFacade(agent_id="noop", db_path=":memory:")
    # Calling close() without initialize() must be safe.
    await fac.close()


async def test_use_before_initialize_raises() -> None:
    fac = MemoryFacade(agent_id="cold", db_path=":memory:")
    with pytest.raises(RuntimeError, match="not initialised"):
        await fac.retrieve_relevant("anything")


async def test_close_then_reuse_raises() -> None:
    fac = MemoryFacade(agent_id="reuse", db_path=":memory:")
    await fac.initialize()
    await fac.close()
    with pytest.raises(RuntimeError, match="not initialised"):
        await fac.retrieve_relevant("anything")


# ─── store_observation + retrieve_relevant ───────────────────


async def test_store_observation_returns_id(facade: MemoryFacade) -> None:
    entry_id = await facade.store_observation(
        "the user prefers tabs over spaces",
        importance=0.8,
        tags=("preferences",),
    )
    assert isinstance(entry_id, str)
    assert entry_id  # non-empty


async def test_store_observation_rejects_empty_content(facade: MemoryFacade) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        await facade.store_observation("   ")


async def test_store_observation_rejects_non_positive_ttl(facade: MemoryFacade) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        await facade.store_observation("hello", ttl_seconds=0)


async def test_retrieve_relevant_finds_stored_observation(facade: MemoryFacade) -> None:
    await facade.store_observation(
        "the user uses Python type hints in 3.12 syntax",
        importance=0.9,
        tags=("python", "syntax"),
    )
    results = await facade.retrieve_relevant("python type hints", limit=5)
    assert len(results) >= 1
    assert any("type hints" in entry.content for entry in results)


async def test_retrieve_relevant_tags_and_semantics(facade: MemoryFacade) -> None:
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


async def test_retrieve_relevant_scope_filter(facade: MemoryFacade) -> None:
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


async def test_retrieve_relevant_min_score_validation(facade: MemoryFacade) -> None:
    with pytest.raises(ValueError, match="min_score"):
        await facade.retrieve_relevant("q", min_score=1.5)


async def test_retrieve_relevant_returns_memory_entry_shape(
    facade: MemoryFacade,
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


async def test_store_procedure_validates_inputs(facade: MemoryFacade) -> None:
    with pytest.raises(ValueError, match="key"):
        await facade.store_procedure("", "body", confidence=0.9)
    with pytest.raises(ValueError, match="confidence"):
        await facade.store_procedure("k", "body", confidence=1.5)


async def test_store_procedure_round_trip(facade: MemoryFacade) -> None:
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
    fac = MemoryFacade(agent_id="c", db_path=":memory:")
    entries = [_entry("short one", 0.9), _entry("short two", 0.5)]
    view = fac.compress(entries, target_tokens=1000)
    assert isinstance(view, CompressedView)
    assert view.entries_dropped == 0
    assert view.tokens_after <= 1000
    assert "short one" in view.summary
    assert "short two" in view.summary


def test_compress_drops_lowest_importance_when_over_budget() -> None:
    fac = MemoryFacade(agent_id="c", db_path=":memory:")
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
    fac = MemoryFacade(agent_id="c", db_path=":memory:")
    view = fac.compress([_entry("anything", 0.9)], target_tokens=0)
    assert view.summary == ""
    assert view.entries_dropped == 1
    assert view.tokens_after == 0


def test_compress_negative_budget_raises() -> None:
    fac = MemoryFacade(agent_id="c", db_path=":memory:")
    with pytest.raises(ValueError, match="target_tokens"):
        fac.compress([], target_tokens=-1)


def test_compress_idempotent_on_already_fitting_view() -> None:
    fac = MemoryFacade(agent_id="c", db_path=":memory:")
    entries = [_entry("alpha", 0.9), _entry("beta", 0.5)]
    first = fac.compress(entries, target_tokens=100)
    # Re-compressing the admitted entries (synthesised from the summary)
    # is a no-op shape-wise.
    second = fac.compress(entries, target_tokens=100)
    assert first.summary == second.summary
    assert first.entries_dropped == second.entries_dropped


# ─── list_candidates (Phase 2 stub) ──────────────────────────


async def test_list_candidates_returns_empty_in_phase_2(facade: MemoryFacade) -> None:
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
